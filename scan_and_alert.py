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

ALERT_WINDOW_MIN   = int(os.getenv("ALERT_WINDOW_MIN", "90"))
ALERT_EARLIEST_MIN = int(os.getenv("ALERT_EARLIEST_MIN", "45"))
IS_MORNING_SCAN    = os.getenv("IS_MORNING_SCAN", "0") == "1"
FULL_REPORT        = os.getenv("FULL_REPORT", "0") == "1"
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
        status = None if FULL_REPORT else "open"
        raw = kalshi_client.get_nba_markets(game_date=game_date, limit=limit, status=status)
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
    """Run edge analysis on all markets, return all results sorted by edge."""
    from data.nba_stats import warm_player_cache
    # Pass player names directly — boxscore approach uses names as keys
    player_names = list({m.get("player_name", "") for m in markets if m.get("player_name")})
    print(f"[cache] Loading ESPN boxscore history for {len(player_names)} unique players...")
    try:
        warm_player_cache(player_names)
    except Exception as e:
        import data.nba_stats as _nba
        _nba._nba_stats_available = False
        print(f"[cache] ESPN boxscore load failed ({e}); skipping analysis")

    analyses = []
    for i, market in enumerate(markets):
        try:
            result = analyze_market(market)
            if result:
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
    # Return all analyses sorted by edge (caller decides threshold)
    return sorted(analyses, key=lambda a: a.get("edge", 0), reverse=True)


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

    if not games and not IS_MORNING_SCAN and not FULL_REPORT:
        print("No games today. Exiting.")
        sys.exit(0)

    # ── 2. Check alert window ─────────────────────────────────────────────
    in_window = games_in_alert_window(games)
    if not IS_MORNING_SCAN and not in_window and not FULL_REPORT:
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
    import data.nba_stats as _nba
    all_analyses = analyze_markets(markets)
    threshold = float(os.getenv("MIN_EDGE_THRESHOLD", "0.02"))
    top_picks = [a for a in all_analyses if a.get("edge", 0) >= threshold][:TOP_N]
    print(f"[analysis] {len(all_analyses)} total | {len(top_picks)} above {threshold:.0%} threshold")

    # ── 5. Format & send ─────────────────────────────────────────────────
    title = build_alert_title(in_window, IS_MORNING_SCAN)

    if FULL_REPORT:
        # Send every analyzed market sorted by edge descending
        lines = [f"Full edge breakdown ({len(all_analyses)} markets):\n"]
        for a in all_analyses:
            edge_pct = f"{a['edge']:+.1%}"
            our = f"{a['our_probability']:.0%}"
            impl = f"{a['implied_probability']:.0%}"
            # Game context
            away = a.get("away_team", "")
            home = a.get("home_team", "")
            gdate = a.get("game_date", "")
            game_tag = f"  [{away}@{home} {gdate}]" if away and home else (f"  [{gdate}]" if gdate else "")
            # Injury / teammate context
            inj = a.get("inj_status", "")
            tm = a.get("teammate_note", "")
            inj_min_factor = a.get("factors", {}).get("inj_minutes", 1.0)
            inj_min_n = a.get("inj_minutes_sample", 0)
            notes = []
            if inj:
                notes.append(f"🏥{inj}")
            if tm:
                notes.append(f"⚠️teammate: {tm}")
            if inj_min_factor != 1.0 and inj_min_n > 0:
                direction = "↑" if inj_min_factor > 1 else "↓"
                notes.append(f"⏱min{direction}{inj_min_factor:.2f}x(n={inj_min_n})")
            note_tag = "  " + " | ".join(notes) if notes else ""
            lines.append(
                f"{a['player_name']} {a['stat_type'].upper()} {a['line']}+  "
                f"edge={edge_pct}  ours={our} kalshi={impl}  conf={a.get('confidence','?')}"
                f"{game_tag}{note_tag}"
            )
        # Split into ≤3800-char chunks for Telegram
        body_full = "\n".join(lines)
        chunks = [body_full[i:i+3800] for i in range(0, len(body_full), 3800)]
        for chunk in chunks:
            send_notification(chunk, title=title)
        print(f"\nSent full report in {len(chunks)} message(s).")
        return

    if not all_analyses and not _nba._nba_stats_available:
        top_raw = sorted(markets, key=lambda m: m.get("volume", 0), reverse=True)[:TOP_N]
        lines = ["NBA Stats API unavailable — raw Kalshi prices:\n"]
        for m in top_raw:
            ask = m.get("yes_ask", 50)
            lines.append(
                f"{m['player_name']} {m['stat_type'].upper()} {m['line']}+ "
                f"| Yes {ask}c / No {100-ask}c  vol={m.get('volume',0)}"
            )
        body = "\n".join(lines)
    elif not top_picks:
        body = f"No edges above {threshold:.0%} today. Best: "
        if all_analyses:
            best = all_analyses[0]
            body += (f"{best['player_name']} {best['stat_type'].upper()} {best['line']}+ "
                     f"edge={best['edge']:+.1%}")
        else:
            body += "none computed."
    else:
        for i, p in enumerate(top_picks, 1):
            p.setdefault("pick_id", f"#{i}")
        body = format_picks_sms(top_picks, today.isoformat())

    send_notification(body, title=title)
    print("\nDone.")


if __name__ == "__main__":
    main()
