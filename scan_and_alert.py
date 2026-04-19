"""
Stateless scan-and-alert script for GitHub Actions (and cron).

Runs on every invocation and decides whether to alert based on:
  - Is it a morning scan? (IS_MORNING_SCAN=1) → always scan + alert top picks
  - Is a game tipping off within ALERT_WINDOW_MIN minutes? → alert for that game
  - Otherwise → exit quietly (no games soon)

No SQLite DB is required — everything is computed fresh each run.
"""
import os
import sys
from datetime import datetime, timezone

import pytz

# ── make sure imports resolve whether run from repo root or via Actions ──
sys.path.insert(0, os.path.dirname(__file__))

from data import espn as espn_mod, kalshi_client
from analysis.edge import analyze_market, rank_picks
from notifications.notify import send_notification
from notifications.sms import (
    format_picks_sms,
    format_resolution_sms,
)
import config

ET = pytz.timezone("America/New_York")

ALERT_WINDOW_MIN   = int(os.getenv("ALERT_WINDOW_MIN", "90"))   # alert if tip-off within N min
ALERT_EARLIEST_MIN = int(os.getenv("ALERT_EARLIEST_MIN", "45")) # don't alert if < N min away
IS_MORNING_SCAN    = os.getenv("IS_MORNING_SCAN", "0") == "1"
TOP_N              = int(os.getenv("TOP_N", "5"))


def games_in_alert_window(games: list[dict]) -> list[dict]:
    """Return games whose tip-off is between ALERT_EARLIEST and ALERT_WINDOW minutes from now."""
    now = datetime.now(timezone.utc)
    in_window = []
    for game in games:
        tip_str = game.get("tip_off", "")
        if not tip_str:
            continue
        try:
            tip = datetime.fromisoformat(tip_str.replace("Z", "+00:00"))
            mins_until = (tip - now).total_seconds() / 60
            if ALERT_EARLIEST_MIN <= mins_until <= ALERT_WINDOW_MIN:
                in_window.append({**game, "mins_until": round(mins_until)})
        except Exception:
            continue
    return in_window


def scan_markets(game_date, limit: int = 200) -> list[dict]:
    """Fetch and parse Kalshi NBA markets for a given date."""
    try:
        raw = kalshi_client.get_nba_markets(game_date=game_date, limit=limit)
    except Exception as e:
        print(f"[kalshi] Failed to fetch markets: {e}")
        return []
    parsed = []
    for r in raw:
        m = kalshi_client.parse_market(r)
        if m:
            parsed.append(m)

    # Deduplicate: keep one market per (player, stat_type) — the one with
    # the highest volume (most liquid / most meaningful price)
    seen: dict[tuple, dict] = {}
    for m in parsed:
        key = (m["player_name"], m["stat_type"])
        if key not in seen or m.get("volume", 0) > seen[key].get("volume", 0):
            seen[key] = m
    deduped = list(seen.values())
    if len(deduped) < len(parsed):
        print(f"[kalshi] Deduped {len(parsed)} → {len(deduped)} markets (1 per player/stat)")
    return deduped


def analyze_markets(markets: list[dict]) -> list[dict]:
    """Run edge analysis on all markets, return top picks sorted by edge."""
    from data.nba_stats import warm_player_cache, find_player_id
    player_ids = []
    for m in markets:
        pid = find_player_id(m.get("player_name", ""))
        if pid:
            player_ids.append(pid)
    if player_ids:
        unique_ids = list(set(player_ids))
        print(f"[cache] Pre-warming {len(unique_ids)} players via bulk load...")
        try:
            warm_player_cache(unique_ids)
        except Exception as e:
            import data.nba_stats as _nba
            _nba._nba_stats_available = False
            print(f"[cache] stats.nba.com unavailable ({e}); skipping NBA stats analysis")

    analyses = []
    for i, market in enumerate(markets):
        try:
            result = analyze_market(market)
            if result:
                # attach display fields from the market
                result["player_name"] = market.get("player_name", "")
                result["stat_type"]   = market.get("stat_type", "")
                result["line"]        = market.get("line")
                result["yes_ask"]     = market.get("yes_ask", 50)
                result["close_time"]  = market.get("close_time", "")
                result["side"]        = "yes"
                analyses.append(result)
        except Exception as e:
            print(f"[analysis] {market.get('ticker','?')}: {e}")
        if (i + 1) % 10 == 0:
            print(f"  Analyzed {i+1}/{len(markets)} markets...")
    return rank_picks(analyses)


def build_alert_title(games_in_window: list[dict], is_morning: bool) -> str:
    if is_morning:
        return "NBA Kalshi Morning Scan"
    if games_in_window:
        game = games_in_window[0]
        mins = game.get("mins_until", "?")
        return (f"NBA Alert — {game['away_team']} @ {game['home_team']} "
                f"tips in ~{mins} min")
    return "NBA Kalshi Alert"


def main():
    now_et = datetime.now(ET)
    today  = now_et.date()
    print(f"[scan_and_alert] {now_et.strftime('%Y-%m-%d %H:%M ET')} | "
          f"morning={IS_MORNING_SCAN}")

    # ── 1. Get today's games ──────────────────────────────────────────────
    try:
        games = espn_mod.get_todays_games(today)
        print(f"[ESPN] {len(games)} games today")
    except Exception as e:
        print(f"[ESPN] {e}")
        games = []

    if not games and not IS_MORNING_SCAN:
        print("No games today. Exiting.")
        sys.exit(0)

    # ── 2. Check alert window ─────────────────────────────────────────────
    in_window = games_in_alert_window(games)
    if not IS_MORNING_SCAN and not in_window:
        print(f"No games tipping off in next {ALERT_EARLIEST_MIN}–{ALERT_WINDOW_MIN} min. "
              "Exiting.")
        sys.exit(0)

    if in_window:
        for g in in_window:
            print(f"  GAME IN WINDOW: {g['away_team']} @ {g['home_team']} "
                  f"({g['mins_until']} min away)")

    # ── 3. Scan Kalshi ────────────────────────────────────────────────────
    print(f"\nScanning Kalshi markets for {today}...")
    markets = scan_markets(today)
    print(f"[kalshi] {len(markets)} valid markets")

    if not markets:
        send_notification(
            "Kalshi markets unavailable or no NBA props found today.",
            title="NBA Kalshi — No Markets",
        )
        sys.exit(0)

    # ── 4. Analyze ────────────────────────────────────────────────────────
    print("Running edge analysis...")
    top_picks = analyze_markets(markets)[:TOP_N]
    print(f"[analysis] {len(top_picks)} picks above threshold")

    # ── 5. Format & send ─────────────────────────────────────────────────
    title = build_alert_title(in_window, IS_MORNING_SCAN)
    import data.nba_stats as _nba
    if not top_picks and not _nba._nba_stats_available:
        # stats.nba.com is down — send top markets by volume as a raw price alert
        top_raw = sorted(markets, key=lambda m: m.get("volume", 0), reverse=True)[:TOP_N]
        lines = [f"⚠️ NBA Stats API unavailable — raw Kalshi prices:\n"]
        for m in top_raw:
            ask = m.get("yes_ask", 50)
            lines.append(
                f"• {m['player_name']} {m['stat_type'].upper()} {m['line']}+ "
                f"| Yes {ask}¢ / No {100-ask}¢  vol={m.get('volume',0)}"
            )
        body = "\n".join(lines)
    elif not top_picks:
        body = "No strong edges found today (all picks below threshold)."
    else:
        for i, p in enumerate(top_picks, 1):
            p.setdefault("pick_id", f"#{i}")
        body = format_picks_sms(top_picks, today.isoformat())

    send_notification(body, title=title)
    print("\nDone.")


if __name__ == "__main__":
    main()
