"""
Backtest — run the edge model against completed games and score its picks.

Usage:
    python backtest.py                   # today's completed games
    python backtest.py --date 2025-04-18 # specific past date
    python backtest.py --min-edge 0.05   # only show picks with edge above threshold
    python backtest.py --top 10          # only analyse top N players per game

How it works:
  1. Fetches completed game box scores from ESPN
  2. For each player with meaningful minutes, pulls their historical game log
     (filtered to games BEFORE the target date so we don't cheat)
  3. Runs the same edge model used for live picks
  4. Tests each stat (PTS, REB, AST, 3PM) at lines near the player's recent average
  5. Compares model predictions to actual box score values
  6. Prints a results table and win-rate summary
"""
import sys
import os
import argparse
from datetime import date, datetime, timedelta

import pandas as pd
from tabulate import tabulate

sys.path.insert(0, os.path.dirname(__file__))

from data import espn as espn_mod
from data import nba_stats
from analysis.edge import _build_stat_series, _apply_factor, _confidence
from db.database import init_db
import config
import numpy as np


STAT_TYPES   = ["pts", "reb", "ast", "3pm"]
STAT_TO_BOX  = {"pts": "pts", "reb": "reb", "ast": "ast", "3pm": "fg3m"}


def lines_to_test(avg: float, stat_type: str) -> list[float]:
    """Generate candidate lines around a player's recent average."""
    if avg < 1:
        return []
    step = 1.0 if avg < 10 else 1.5 if avg < 20 else 2.5
    lines = [round(avg - step, 1), round(avg, 1), round(avg + step, 1)]
    # snap to .5 increments (common Kalshi format)
    snapped = []
    for l in lines:
        snapped.append(round(l * 2) / 2)
    return sorted(set(snapped))


def run_model(player_name: str, stat_type: str, line: float,
              game_date: date, season: str) -> dict | None:
    """Run the pre-game edge model for one player/stat/line. Returns analysis or None."""
    player_id = nba_stats.find_player_id(player_name)
    if not player_id:
        return None
    try:
        df_full = nba_stats.get_player_game_log(player_id, season=season, last_n=60)
    except Exception:
        return None

    # ── CRITICAL: exclude the game being tested (no lookahead bias) ──────────
    df = df_full[df_full["GAME_DATE"].dt.date < game_date].head(40)
    if len(df) < 5:
        return None

    stat_series = _build_stat_series(df, stat_type)
    if stat_series is None or len(stat_series) == 0:
        return None

    n = len(stat_series)
    weights = np.array([config.DECAY_FACTOR ** i for i in range(n)])
    weights /= weights.sum()
    over_mask = (stat_series >= line).astype(float).values
    base_prob = float(np.dot(weights, over_mask))

    # Assume 50c implied probability (neutral Kalshi line) for backtesting
    implied_prob = 0.50
    edge = round(base_prob - implied_prob, 4)
    confidence = _confidence(n, float(stat_series.std()), float(stat_series.mean()))

    return {
        "our_prob":   round(base_prob, 3),
        "implied":    implied_prob,
        "edge":       edge,
        "confidence": confidence,
        "sample_n":   n,
    }


def backtest_date(target_date: date, min_edge: float = 0.0, top_n: int = 999):
    season = _season_for_date(target_date)
    print(f"\nBacktest — {target_date}  (season {season})\n")

    # ── 1. Fetch completed games ─────────────────────────────────────────────
    all_games = espn_mod.get_todays_games(target_date)
    completed  = [g for g in all_games
                  if "final" in g["status"].lower() or g["status"].startswith("STATUS_FINAL")]

    if not completed:
        # ESPN may still say "STATUS_IN_PROGRESS" — try anyway
        completed = all_games

    if not completed:
        print(f"No completed games found for {target_date}.")
        print("ESPN may not have data for this date, or no games were scheduled.")
        return

    print(f"Found {len(completed)} completed game(s):\n")
    for g in completed:
        print(f"  {g['away_team']} @ {g['home_team']}")
    print()

    all_results = []

    for game in completed:
        event_id = game["event_id"]
        home     = game["home_team"]
        away     = game["away_team"]
        header   = f"{away} @ {home}"
        print(f"{'─'*60}")
        print(f"{header}")
        print(f"{'─'*60}")

        # ── 2. Get box score ─────────────────────────────────────────────────
        try:
            players = espn_mod.get_game_boxscore(event_id)
        except Exception as e:
            print(f"  Box score unavailable: {e}\n")
            continue

        if not players:
            print("  No box score data.\n")
            continue

        # Sort by minutes played, take top_n
        players = sorted(players, key=lambda p: p["min"], reverse=True)[:top_n]
        game_rows = []

        for player in players:
            name = player["name"]
            for stat_type in STAT_TYPES:
                actual_val = player.get(STAT_TO_BOX[stat_type], 0)

                # Build lines to test
                recent_avg = _recent_avg(name, stat_type, target_date, season)
                if recent_avg is None or recent_avg < 0.5:
                    continue
                for line in lines_to_test(recent_avg, stat_type):
                    result = run_model(name, stat_type, line, target_date, season)
                    if result is None:
                        continue
                    if abs(result["edge"]) < min_edge:
                        continue

                    over_hit = actual_val >= line
                    # Did following the model (bet YES if edge>0, NO if edge<0) pay off?
                    model_says_yes = result["edge"] > 0
                    correct = (model_says_yes and over_hit) or (not model_says_yes and not over_hit)

                    game_rows.append({
                        "player":   name,
                        "stat":     stat_type.upper(),
                        "line":     line,
                        "actual":   actual_val,
                        "over_hit": over_hit,
                        "our_prob": result["our_prob"],
                        "edge":     result["edge"],
                        "conf":     result["confidence"],
                        "correct":  correct,
                        "game":     header,
                    })

        if not game_rows:
            print("  No picks met the edge threshold.\n")
            continue

        # Print table for this game
        table_rows = []
        for r in sorted(game_rows, key=lambda x: abs(x["edge"]), reverse=True):
            result_str = "WIN" if r["correct"] else "LOSS"
            over_str   = f"{r['actual']} (OVER)" if r["over_hit"] else f"{r['actual']} (UNDER)"
            table_rows.append([
                r["player"],
                r["stat"],
                r["line"],
                over_str,
                f"{r['our_prob']*100:.0f}%",
                f"{r['edge']*100:+.1f}%",
                r["conf"],
                result_str,
            ])

        print(tabulate(table_rows, headers=[
            "Player", "Stat", "Line", "Actual", "OurProb", "Edge", "Conf", "Result"
        ], tablefmt="simple"))
        print()
        all_results.extend(game_rows)

    # ── 3. Overall summary ───────────────────────────────────────────────────
    if not all_results:
        print("No results to summarise.")
        return

    total   = len(all_results)
    correct = sum(1 for r in all_results if r["correct"])
    high_conf = [r for r in all_results if r["conf"] == "high"]
    high_correct = sum(1 for r in high_conf if r["correct"])

    # Edge-weighted: only picks where model saw >5% edge
    strong = [r for r in all_results if abs(r["edge"]) >= 0.05]
    strong_correct = sum(1 for r in strong if r["correct"])

    print("=" * 60)
    print("BACKTEST SUMMARY")
    print("=" * 60)
    print(f"  All picks:          {correct}/{total}  ({_pct(correct, total)}%)")
    if strong:
        print(f"  Edge >= 5%:         {strong_correct}/{len(strong)}  ({_pct(strong_correct, len(strong))}%)")
    if high_conf:
        print(f"  High confidence:    {high_correct}/{len(high_conf)}  ({_pct(high_correct, len(high_conf))}%)")
    print()

    # Show the biggest wins and misses
    hits   = sorted([r for r in all_results if r["correct"]],
                    key=lambda x: abs(x["edge"]), reverse=True)[:3]
    misses = sorted([r for r in all_results if not r["correct"]],
                    key=lambda x: abs(x["edge"]), reverse=True)[:3]

    if hits:
        print("  Best calls:")
        for r in hits:
            over_str = "OVER" if r["over_hit"] else "UNDER"
            print(f"    {r['player']} {r['stat']} {r['line']}+ → "
                  f"actual {r['actual']} ({over_str}) | edge {r['edge']*100:+.1f}%")
    if misses:
        print("  Missed on:")
        for r in misses:
            over_str = "OVER" if r["over_hit"] else "UNDER"
            print(f"    {r['player']} {r['stat']} {r['line']}+ → "
                  f"actual {r['actual']} ({over_str}) | edge {r['edge']*100:+.1f}%")
    print()


def _recent_avg(player_name: str, stat_type: str,
                before_date: date, season: str) -> float | None:
    """Get a player's pre-game average for a stat (excluding the game being tested)."""
    player_id = nba_stats.find_player_id(player_name)
    if not player_id:
        return None
    try:
        df = nba_stats.get_player_game_log(player_id, season=season, last_n=60)
        df = df[df["GAME_DATE"].dt.date < before_date].head(10)
        if len(df) < 3:
            return None
        series = _build_stat_series(df, stat_type)
        return float(series.mean()) if series is not None and len(series) > 0 else None
    except Exception:
        return None


def _season_for_date(d: date) -> str:
    year = d.year if d.month >= 10 else d.year - 1
    return f"{year}-{str(year + 1)[2:]}"


def _pct(num: int, denom: int) -> str:
    return f"{num/denom*100:.1f}" if denom else "—"


def _send_via_telegram(text: str, title: str):
    """Chunk and send backtest output to Telegram (4096 char limit per message)."""
    from notifications.notify import send_telegram
    chunk_size = 3800
    chunks = [text[i:i+chunk_size] for i in range(0, len(text), chunk_size)]
    for i, chunk in enumerate(chunks):
        t = title if i == 0 else f"{title} (cont.)"
        ok = send_telegram(chunk, title=t)
        if not ok:
            print(f"[telegram] Failed to send chunk {i+1}/{len(chunks)}")


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description="Backtest NBA Kalshi edge model")
    parser.add_argument("--date", default=None,
                        help="Date to backtest (YYYY-MM-DD, default: today)")
    parser.add_argument("--min-edge", type=float, default=0.0,
                        help="Minimum |edge| to include a pick (default: 0.0)")
    parser.add_argument("--top", type=int, default=10,
                        help="Max players to analyse per game (default: 10)")
    parser.add_argument("--notify", action="store_true",
                        help="Send results to Telegram when done")
    args = parser.parse_args()

    target = date.fromisoformat(args.date) if args.date else date.today()
    init_db()

    if args.notify:
        import io
        buf = io.StringIO()
        _real_stdout = sys.stdout
        sys.stdout = buf

    backtest_date(target, min_edge=args.min_edge, top_n=args.top)

    if args.notify:
        sys.stdout = _real_stdout
        output = buf.getvalue()
        print(output)   # still print to Actions log
        _send_via_telegram(output, title=f"Backtest {target}")
