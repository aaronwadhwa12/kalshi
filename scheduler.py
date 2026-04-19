"""
Automated scheduler for daily NBA Kalshi edge alerts.

Runs two jobs:
  1. Morning scan (default 10 AM ET) — full market scan + SMS alert
  2. Pre-game refresh (1 hour before each tip-off) — refresh prices + re-alert

Usage:
    python scheduler.py
"""
import logging
import sys
from datetime import datetime, timedelta

import pytz
from apscheduler.schedulers.blocking import BlockingScheduler
from apscheduler.triggers.cron import CronTrigger
from apscheduler.triggers.date import DateTrigger

import config
from db.database import init_db, get_top_picks, mark_pick_alerted
from data import kalshi_client, espn as espn_mod
from data.kalshi_client import get_nba_markets, parse_market
from analysis.edge import analyze_market, rank_picks
from notifications.sms import send_picks_alert
from db import database as db

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s [%(levelname)s] %(message)s",
    handlers=[logging.StreamHandler(sys.stdout)],
)
log = logging.getLogger(__name__)

ET = pytz.timezone("America/New_York")


def morning_scan():
    """Full market scan and SMS alert for today's games."""
    today = datetime.now(ET).date()
    log.info(f"Morning scan starting for {today}")
    init_db()

    # ESPN: get today's games for context
    try:
        games = espn_mod.get_todays_games(today)
        log.info(f"  {len(games)} games today")
    except Exception as e:
        log.warning(f"  ESPN games fetch failed: {e}")
        games = []

    if not games:
        log.info("  No games today — skipping scan.")
        return

    # Kalshi: fetch NBA markets
    try:
        raw_markets = kalshi_client.get_nba_markets(game_date=today, limit=500)
        log.info(f"  {len(raw_markets)} Kalshi markets fetched")
    except Exception as e:
        log.error(f"  Kalshi fetch failed: {e}")
        return

    parsed = []
    for raw in raw_markets:
        m = parse_market(raw)
        if m:
            db.upsert_market(m)
            parsed.append(m)
    log.info(f"  {len(parsed)} markets parsed")

    analyses = []
    for market in parsed:
        try:
            result = analyze_market(market)
            if result:
                analyses.append(result)
        except Exception as e:
            log.debug(f"  Analysis failed for {market.get('ticker')}: {e}")

    top = rank_picks(analyses)
    log.info(f"  {len(top)} picks above threshold")

    for analysis in top:
        analysis_id = db.save_analysis(analysis)
        db.save_pick(analysis["market_ticker"], analysis_id)

    picks = get_top_picks(date=today.isoformat(), limit=5)
    if picks:
        send_picks_alert(picks, today.isoformat())
        log.info("  SMS alert sent")
        for p in picks:
            mark_pick_alerted(p["pick_id"])
    else:
        log.info("  No strong picks to alert")

    # Schedule pre-game refreshes
    _schedule_pregame_refreshes(games, scheduler)


def pregame_refresh(home_team: str, away_team: str, game_date: str):
    """Refresh prices and re-alert 1 hour before a specific game."""
    log.info(f"Pre-game refresh: {away_team} @ {home_team} on {game_date}")
    picks = get_top_picks(date=game_date, limit=5)
    if picks:
        send_picks_alert(picks, game_date)
        log.info("  Pre-game SMS sent")


def _schedule_pregame_refreshes(games: list, sched):
    """Add one-shot pre-game refresh jobs for each game today."""
    minutes_before = config.PREGAME_ALERT_MINUTES_BEFORE
    now = datetime.now(ET)
    for game in games:
        tip_off_str = game.get("tip_off", "")
        if not tip_off_str:
            continue
        try:
            tip_off = datetime.fromisoformat(tip_off_str.replace("Z", "+00:00"))
            tip_off_et = tip_off.astimezone(ET)
            refresh_time = tip_off_et - timedelta(minutes=minutes_before)
            if refresh_time > now:
                sched.add_job(
                    pregame_refresh,
                    trigger=DateTrigger(run_date=refresh_time),
                    args=[game["home_team"], game["away_team"],
                          datetime.now(ET).date().isoformat()],
                    id=f"pregame_{game['event_id']}",
                    replace_existing=True,
                )
                log.info(
                    f"  Pre-game refresh scheduled for {game['away_team']} @ "
                    f"{game['home_team']} at {refresh_time.strftime('%I:%M %p ET')}"
                )
        except Exception as e:
            log.warning(f"  Could not schedule pre-game refresh: {e}")


scheduler = BlockingScheduler(timezone=ET)


def main():
    init_db()
    log.info("NBA Kalshi Edge Alert Scheduler starting...")
    log.info(f"  Morning scan:    {config.MORNING_SCAN_HOUR}:00 AM ET daily")
    log.info(f"  Pre-game alert:  {config.PREGAME_ALERT_MINUTES_BEFORE} min before tip-off")

    scheduler.add_job(
        morning_scan,
        trigger=CronTrigger(hour=config.MORNING_SCAN_HOUR, minute=0, timezone=ET),
        id="morning_scan",
        replace_existing=True,
    )

    # Run immediately on startup as well
    log.info("Running initial scan now...")
    morning_scan()

    try:
        scheduler.start()
    except (KeyboardInterrupt, SystemExit):
        log.info("Scheduler stopped.")


if __name__ == "__main__":
    main()
