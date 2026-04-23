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

ALERT_WINDOW_MIN    = int(os.getenv("ALERT_WINDOW_MIN", "90"))
ALERT_EARLIEST_MIN  = int(os.getenv("ALERT_EARLIEST_MIN", "45"))
IS_MORNING_SCAN     = os.getenv("IS_MORNING_SCAN", "0") == "1"
FULL_REPORT         = os.getenv("FULL_REPORT", "0") == "1"
CALIBRATION_REPORT  = os.getenv("CALIBRATION_REPORT", "0") == "1"
TOP_N               = int(os.getenv("TOP_N", "5"))


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


def scan_markets(game_date, limit: int = 200,
                 has_games_today: bool = False,
                 include_closed: bool = False,
                 games: list = None) -> list[dict]:
    """Fetch and parse Kalshi NBA markets for a given date.

    has_games_today: if True (ESPN confirmed games today), never fall back to
    future-date markets — Kalshi just hasn't opened props yet for today.
    include_closed: also fetch closed/settled markets (used after tip-off).
    games: ESPN game list (with away_abbr/home_abbr) — enables targeted
           event-ticker fetching so today's markets are never missed due to
           pagination sort order.
    """
    from datetime import timedelta
    try:
        if FULL_REPORT or include_closed:
            status = None   # fetch open + closed + settled
        else:
            status = "open"
        raw = kalshi_client.get_nba_markets(
            game_date=game_date, limit=limit, status=status,
            games=games,
        )
    except Exception as e:
        print(f"[kalshi] Failed to fetch markets: {e}")
        return []

    today_str = game_date.isoformat() if hasattr(game_date, "isoformat") else str(game_date)
    # _extract_date now uses ET — no UTC overflow. Keep a 1-day upper buffer
    # only for edge cases (e.g. markets whose event spans past midnight ET).
    tomorrow_str = (game_date + timedelta(days=1)).isoformat()

    print(f"[kalshi] {len(raw)} raw markets returned by API")

    parsed       = []
    parse_failed = 0
    date_dropped = 0
    failed_titles: list[str] = []
    for r in raw:
        m = kalshi_client.parse_market(r)
        if not m:
            parse_failed += 1
            title = r.get("title", "") or r.get("ticker", "?")
            if len(failed_titles) < 8:
                failed_titles.append(title)
            continue
        gdate = m.get("game_date", today_str)
        if not (today_str <= gdate <= tomorrow_str):
            date_dropped += 1
            if len(failed_titles) < 8:
                failed_titles.append(f"[date={gdate}] {m['player_name']} {m['stat_type']}")
            continue
        # Drop near-settled markets (≤2¢ = settled NO, ≥98¢ = settled YES)
        ask = m.get("yes_ask", 50)
        if ask <= 2 or ask >= 98:
            date_dropped += 1
            if len(failed_titles) < 8:
                failed_titles.append(f"[settled={ask}¢] {m['player_name']} {m['stat_type']}")
            continue
        parsed.append(m)

    if parse_failed or date_dropped:
        print(f"[kalshi] Dropped: {parse_failed} parse-fail, {date_dropped} date-filtered")
        for t in failed_titles:
            print(f"  ↳ {t}")

    # If we got raw markets but NONE parsed, dump the first few raw records
    # so we can see what Kalshi is actually returning and why parsing fails.
    if raw and not parsed and parse_failed > 0:
        print("[kalshi] DIAGNOSTIC — first 3 raw market records:")
        for r in raw[:3]:
            print(f"  ticker={r.get('ticker')} title={r.get('title')!r} "
                  f"subtitle={r.get('subtitle')!r} "
                  f"yes_ask={r.get('yes_ask')} yes_ask_dollars={r.get('yes_ask_dollars')} "
                  f"close_time={r.get('close_time')}")

    # Only fall back to future-date markets on genuine off-days.
    # If ESPN shows games today, Kalshi just hasn't opened props yet — don't
    # show next week's games as if they were tonight's picks.
    if not parsed and not has_games_today and (IS_MORNING_SCAN or FULL_REPORT):
        print(f"[kalshi] Off-day — no games today, fetching nearest upcoming open markets...")
        try:
            raw_all = kalshi_client.get_nba_markets(game_date=None, limit=limit, status="open")
            for r in raw_all:
                m = kalshi_client.parse_market(r)
                if m:
                    parsed.append(m)
        except Exception as e:
            print(f"[kalshi] Fallback fetch failed: {e}")

    # Log price distribution so we can see what Kalshi is returning
    if parsed:
        prices = [m["yes_ask"] for m in parsed]
        low    = sum(1 for p in prices if p <= 5)
        high   = sum(1 for p in prices if p >= 95)
        mid    = len(prices) - low - high
        print(f"[kalshi] Price dist: {mid} normal (6-94c), {high} high (≥95c), {low} low (≤5c)")

    # Deduplicate: keep one market per (player, stat_type) — highest volume wins
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


def build_alert_title(games_in_window: list[dict], is_morning: bool,
                      markets: list[dict] = None) -> str:
    if is_morning:
        return "NBA Kalshi Morning Scan"
    if games_in_window:
        game = games_in_window[0]
        mins = game.get("mins_until", "?")
        return (f"NBA Alert — {game['away_team']} @ {game['home_team']} "
                f"tips in ~{mins} min")
    return "NBA Kalshi Alert"


def main():
    from datetime import timedelta
    from analysis.calibration import (
        resolve_picks, log_picks, update_learned_params,
        format_calibration_report, format_daily_recap,
    )

    now_et = datetime.now(ET)
    today  = now_et.date()
    print(f"[scan_and_alert] {now_et.strftime('%Y-%m-%d %H:%M ET')} | "
          f"morning={IS_MORNING_SCAN}")

    # ── 0. Morning resolution & recap ─────────────────────────────────────
    if IS_MORNING_SCAN:
        try:
            resolve_picks()               # mark win/loss for all past game dates
            update_learned_params()       # recompute bias corrections
        except Exception as e:
            print(f"[calibration] Resolution failed (non-fatal): {e}")

        # Send yesterday's results before today's picks
        yesterday = today - timedelta(days=1)
        try:
            recap = format_daily_recap(yesterday)
            if recap:
                send_notification(recap, title=f"NBA Results — {yesterday}")
                print(f"[calibration] Sent recap for {yesterday}")
            else:
                print(f"[calibration] No logged picks for {yesterday} — recap skipped"
                      " (picks log was empty or scan didn't run yesterday)")
        except Exception as e:
            print(f"[calibration] Recap send failed (non-fatal): {e}")

        # Regenerate Excel report so it stays current after each morning resolution
        try:
            from analysis.report import generate_report
            generate_report()
        except Exception as e:
            print(f"[report] Excel generation failed (non-fatal): {e}")

    # ── 0b. Calibration report (short-circuit) ────────────────────────────
    if CALIBRATION_REPORT:
        try:
            report = format_calibration_report()
            send_notification(report, title="NBA Model Calibration Report")
            print(report)
        except Exception as e:
            print(f"[calibration] Report failed: {e}")
        return

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
    has_games_today = len(games) > 0
    markets = scan_markets(today, has_games_today=has_games_today, games=games)
    print(f"[kalshi] {len(markets)} valid markets")

    if not markets:
        if has_games_today:
            # Distinguish "not open yet" from "already closed" based on ET hour
            if now_et.hour >= 18:
                # After 6 PM ET — markets likely closed at tip-off; try fetching
                # closed markets for today so we can still run analysis
                print("[kalshi] After 6 PM ET — trying closed markets for today's games...")
                markets = scan_markets(today, has_games_today=True, include_closed=True)
                if markets:
                    print(f"[kalshi] Found {len(markets)} closed markets — running analysis for reference")
                else:
                    msg = (f"Today's Kalshi markets have closed — {len(games)} game(s) "
                           f"are underway for {today}. Results tomorrow morning.")
                    print(f"[kalshi] {msg}")
                    send_notification(msg, title="NBA Kalshi — Games Underway")
                    sys.exit(0)
            else:
                msg = (f"{len(games)} NBA game(s) today but Kalshi hasn't opened "
                       f"player prop markets yet for {today}. Check back closer to tip-off.")
                print(f"[kalshi] {msg}")
                if IS_MORNING_SCAN or FULL_REPORT:
                    send_notification(msg, title="NBA Kalshi — No Markets")
                sys.exit(0)
        else:
            msg = f"No NBA games today ({today}) and no upcoming Kalshi markets found."
            print(f"[kalshi] {msg}")
            if IS_MORNING_SCAN or FULL_REPORT:
                send_notification(msg, title="NBA Kalshi — No Markets")
            sys.exit(0)

    # Warn if any markets are for a future date (off-day fallback triggered)
    today_str = today.isoformat()
    future_markets = [m for m in markets if m.get("game_date", today_str) > today_str]
    if future_markets and not has_games_today:
        future_dates = sorted({m["game_date"] for m in future_markets})
        print(f"[kalshi] Off-day: showing markets for upcoming games on {', '.join(future_dates)}")

    # ── 4. Analyze ────────────────────────────────────────────────────────
    print("Running edge analysis...")
    import data.nba_stats as _nba
    all_analyses = analyze_markets(markets)
    threshold = float(os.getenv("MIN_EDGE_THRESHOLD", "0.02"))
    top_picks = [a for a in all_analyses if a.get("edge", 0) >= threshold][:TOP_N]
    print(f"[analysis] {len(all_analyses)} total | {len(top_picks)} above {threshold:.0%} threshold")

    # ── 4b. Position sizing ───────────────────────────────────────────────
    from analysis.sizing import suggest_contracts
    try:
        bankroll = kalshi_client.get_balance()
        print(f"[sizing] Kalshi balance: ${bankroll:.2f}")
    except Exception:
        bankroll = float(os.getenv("BANKROLL", "100"))
        print(f"[sizing] Balance fetch failed, using default ${bankroll:.2f}")
    for pick in top_picks:
        pick["suggested_contracts"] = suggest_contracts(
            pick.get("our_probability", 0.5),
            pick.get("yes_ask", 50),
            pick.get("confidence", "low"),
            bankroll,
        )

    # ── 4c. Log picks for calibration ─────────────────────────────────────
    try:
        log_picks(all_analyses, today)
    except Exception as e:
        print(f"[calibration] Logging failed (non-fatal): {e}")

    # ── 5. Format & send ─────────────────────────────────────────────────
    title = build_alert_title(in_window, IS_MORNING_SCAN, markets=all_analyses or markets)

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
