#!/usr/bin/env python3
"""
NBA Kalshi Edge Alerts — CLI

Commands:
  scan      Scan Kalshi for NBA markets and compute edges
  picks     Show today's top picks
  alert     Send SMS with top picks
  bet       Record a bet (paper or live)
  track     Show open bets and P&L
  resolve   Resolve a bet by actual stat value
  balance   Show Kalshi account balance
"""
import sys
import json
from datetime import date

import click
from tabulate import tabulate

from db.database import init_db, get_top_picks, get_performance_summary
from data import kalshi_client, espn as espn_mod
from analysis.edge import analyze_market, rank_picks
from notifications.sms import send_picks_alert, format_daily_summary_sms, send_sms
from tracking.portfolio import (
    add_bet, resolve_bet, resolve_bet_by_outcome,
    get_open_bets, format_bets_table, get_portfolio_summary,
)
from db import database as db


@click.group()
def cli():
    """NBA Kalshi Edge Alerts — find and track the best NBA prop bets on Kalshi."""
    init_db()


# ---------------------------------------------------------------------------
# scan
# ---------------------------------------------------------------------------

@cli.command()
@click.option("--date", "game_date", default=None,
              help="Game date YYYY-MM-DD (default: today)")
@click.option("--min-edge", default=None, type=float,
              help="Minimum edge threshold (default: from .env)")
@click.option("--limit", default=20, help="Max markets to analyze (default: 20)")
@click.option("--alert/--no-alert", default=False,
              help="Send SMS alert after scanning")
def scan(game_date, min_edge, limit, alert):
    """Fetch NBA markets from Kalshi, compute edges, and save top picks."""
    target_date = date.fromisoformat(game_date) if game_date else date.today()
    click.echo(f"\nScanning Kalshi NBA markets for {target_date}...\n")

    # Enrich with today's games for home/away context
    try:
        todays_games = espn_mod.get_todays_games(target_date)
        game_map = {
            g["home_team"].lower(): g for g in todays_games
        }
        game_map.update({g["away_team"].lower(): g for g in todays_games})
        click.echo(f"Found {len(todays_games)} games today.")
    except Exception as e:
        click.echo(f"[ESPN] Could not fetch games: {e}")
        todays_games = []
        game_map = {}

    # Fetch Kalshi markets
    try:
        raw_markets = kalshi_client.get_nba_markets(game_date=target_date, limit=limit * 5)
        click.echo(f"Fetched {len(raw_markets)} raw Kalshi markets.")
    except Exception as e:
        click.echo(f"[Kalshi] Error fetching markets: {e}", err=True)
        sys.exit(1)

    parsed = []
    for raw in raw_markets:
        m = kalshi_client.parse_market(raw)
        if not m:
            continue
        # Enrich with home/away team from ESPN
        game = _match_game(m["player_name"], game_map, todays_games)
        if game:
            m["home_team"] = game["home_team"]
            m["away_team"] = game["away_team"]
        db.upsert_market(m)
        parsed.append(m)

    click.echo(f"Parsed {len(parsed)} valid player-prop markets.\n")

    if not parsed:
        click.echo("No markets to analyze. Try --limit or check your KALSHI_API_KEY.")
        return

    # Analyze each market
    analyses = []
    with click.progressbar(parsed[:limit], label="Analyzing") as bar:
        for market in bar:
            try:
                result = analyze_market(market)
                if result:
                    analyses.append((market, result))
            except Exception as e:
                pass  # skip individual failures silently

    # Save analyses and create picks
    top = rank_picks([a for _, a in analyses], min_edge=min_edge)
    pick_ids = []
    for analysis in top:
        analysis_id = db.save_analysis(analysis)
        pick_id = db.save_pick(analysis["market_ticker"], analysis_id)
        pick_ids.append(pick_id)

    click.echo(f"\nFound {len(top)} picks above edge threshold:\n")
    _print_picks_table(get_top_picks(
        date=target_date.isoformat(), min_edge=min_edge or 0.0
    ))

    if alert and top:
        picks = get_top_picks(date=target_date.isoformat(), limit=5)
        db.mark_pick_alerted(picks[0]["pick_id"]) if picks else None
        send_picks_alert(picks, target_date.isoformat())
        click.echo("\nSMS alert sent.")


# ---------------------------------------------------------------------------
# picks
# ---------------------------------------------------------------------------

@cli.command()
@click.option("--date", "game_date", default=None,
              help="Game date YYYY-MM-DD (default: today)")
@click.option("--min-edge", default=0.0, type=float,
              help="Filter by minimum edge")
@click.option("--limit", default=10, help="Number of picks to show")
@click.option("--detail/--no-detail", default=False,
              help="Show full analysis detail")
def picks(game_date, min_edge, limit, detail):
    """Show top picks for today (or a given date)."""
    today = game_date or date.today().isoformat()
    rows = get_top_picks(date=today, min_edge=min_edge, limit=limit)
    if not rows:
        click.echo(f"No picks found for {today}. Run: python main.py scan")
        return
    click.echo(f"\nTop picks for {today}:\n")
    _print_picks_table(rows, detail=detail)


# ---------------------------------------------------------------------------
# alert
# ---------------------------------------------------------------------------

@cli.command()
@click.option("--date", "game_date", default=None)
@click.option("--limit", default=5)
def alert(game_date, limit):
    """Send an SMS alert with today's top picks."""
    today = game_date or date.today().isoformat()
    rows = get_top_picks(date=today, limit=limit)
    if not rows:
        click.echo("No picks to alert. Run scan first.")
        return
    sid = send_picks_alert(rows, today)
    if sid:
        click.echo(f"SMS sent. SID: {sid}")
    else:
        click.echo("SMS not sent (no Twilio config). Picks printed above.")


# ---------------------------------------------------------------------------
# bet
# ---------------------------------------------------------------------------

@cli.command()
@click.argument("pick_id", type=int)
@click.argument("contracts", type=int)
@click.option("--live/--paper", default=False,
              help="Place real order on Kalshi (requires API key + funds)")
def bet(pick_id, contracts, live):
    """
    Record a bet for a pick.

    \b
    PICK_ID   The pick ID shown in `picks` output
    CONTRACTS Number of contracts to buy
    """
    mode = "LIVE" if live else "PAPER"
    click.echo(f"\nRecording {mode} bet: pick #{pick_id}, {contracts} contracts")

    if live:
        click.confirm(
            "This will place a REAL order on Kalshi. Continue?", abort=True
        )

    try:
        result = add_bet(pick_id, contracts, is_live=live)
    except (ValueError, RuntimeError) as e:
        click.echo(f"Error: {e}", err=True)
        sys.exit(1)

    cost = result["cost"]
    click.echo(f"\n✓ Bet #{result['bet_id']} recorded")
    click.echo(f"  {result['player']} {result['stat'].upper()} {result['line']}+")
    click.echo(f"  Side: {result['side'].upper()} @ {result['entry_price']}c/contract")
    click.echo(f"  Contracts: {contracts}  |  Cost: ${cost:.2f}")
    if result.get("order_id"):
        click.echo(f"  Kalshi order ID: {result['order_id']}")


# ---------------------------------------------------------------------------
# track
# ---------------------------------------------------------------------------

@cli.command()
@click.option("--summary/--no-summary", default=True)
def track(summary):
    """Show open bets and portfolio P&L."""
    open_bets = get_open_bets()
    click.echo("\n--- Open Bets ---\n")
    if open_bets:
        rows = format_bets_table(open_bets)
        click.echo(tabulate(rows, headers=[
            "ID", "Player", "Prop", "Side", "Qty", "Price", "Outcome", "P&L"
        ], tablefmt="simple"))
    else:
        click.echo("No open bets.")

    if summary:
        perf = get_performance_summary()
        click.echo("\n--- Portfolio Summary ---\n")
        wins    = perf.get("wins", 0)
        losses  = perf.get("losses", 0)
        total   = perf.get("total_bets", 0)
        pnl     = perf.get("total_pnl", 0.0) or 0.0
        avg_pnl = perf.get("avg_pnl", 0.0) or 0.0
        win_pct = round(wins / max(wins + losses, 1) * 100, 1)
        click.echo(f"  Total bets: {total}")
        click.echo(f"  Record:     {wins}W - {losses}L ({win_pct}%)")
        click.echo(f"  Pending:    {perf.get('pending', 0)}")
        click.echo(f"  Net P&L:    ${pnl:+.2f}")
        click.echo(f"  Avg/bet:    ${avg_pnl:+.2f}")
        click.echo()


# ---------------------------------------------------------------------------
# resolve
# ---------------------------------------------------------------------------

@cli.command()
@click.argument("bet_id", type=int)
@click.argument("actual_value", type=float)
def resolve(bet_id, actual_value):
    """
    Resolve a bet by entering the actual stat value.

    \b
    BET_ID        The bet ID shown in `track` output
    ACTUAL_VALUE  The player's actual stat (e.g., 27.0 for 27 points)
    """
    try:
        result = resolve_bet(bet_id, actual_value)
    except ValueError as e:
        click.echo(f"Error: {e}", err=True)
        sys.exit(1)

    icon = "WIN" if result["outcome"] == "won" else "LOSS"
    pnl  = result["profit_loss"]
    click.echo(
        f"\n{icon}  Bet #{bet_id}: {result['player']} "
        f"{result['stat'].upper()} {result['line']}+ "
        f"(actual: {actual_value})  P&L: ${pnl:+.2f}"
    )


# ---------------------------------------------------------------------------
# resolve-manual  (for when Kalshi has already settled the market)
# ---------------------------------------------------------------------------

@cli.command("resolve-manual")
@click.argument("bet_id", type=int)
@click.argument("outcome", type=click.Choice(["won", "lost", "void"]))
@click.option("--pnl", type=float, default=None,
              help="Override P&L (auto-calculated if omitted)")
def resolve_manual(bet_id, outcome, pnl):
    """Manually mark a bet as won, lost, or void."""
    try:
        result = resolve_bet_by_outcome(bet_id, outcome, pnl)
    except ValueError as e:
        click.echo(f"Error: {e}", err=True)
        sys.exit(1)
    click.echo(f"\nBet #{bet_id} resolved: {outcome}  P&L: ${result['profit_loss']:+.2f}")


# ---------------------------------------------------------------------------
# balance
# ---------------------------------------------------------------------------

@cli.command()
def balance():
    """Show your Kalshi account balance."""
    try:
        bal = kalshi_client.get_balance()
        click.echo(f"\nKalshi balance: ${bal:.2f}")
    except Exception as e:
        click.echo(f"Error: {e}", err=True)


# ---------------------------------------------------------------------------
# summary-sms
# ---------------------------------------------------------------------------

@cli.command("summary-sms")
def summary_sms():
    """Send a portfolio performance summary via SMS."""
    perf = get_performance_summary()
    body = format_daily_summary_sms(perf)
    click.echo(body)
    try:
        sid = send_sms(body)
        click.echo(f"\nSMS sent. SID: {sid}")
    except RuntimeError as e:
        click.echo(f"SMS not sent: {e}")


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

def _print_picks_table(picks: list[dict], detail: bool = False):
    if not picks:
        click.echo("  (none)")
        return

    rows = []
    for p in picks:
        edge_pct = f"+{p['edge']*100:.1f}%"
        our_pct  = f"{p['our_probability']*100:.1f}%"
        imp_pct  = f"{p['implied_probability']*100:.1f}%"
        rows.append([
            p["pick_id"],
            p.get("player_name", ""),
            f"{p.get('stat_type','').upper()} {p.get('line','')}+",
            p.get("side", "yes").upper(),
            f"{p.get('yes_ask','')}c",
            our_pct,
            imp_pct,
            edge_pct,
            p.get("confidence", ""),
            p.get("game_date", ""),
        ])

    click.echo(tabulate(rows, headers=[
        "ID", "Player", "Prop", "Side", "Ask", "OurProb", "Implied",
        "Edge", "Conf", "Date"
    ], tablefmt="simple"))

    if detail:
        click.echo()
        for p in picks:
            click.echo(f"\n  #{p['pick_id']} {p.get('player_name')} — {p.get('stat_type','').upper()} {p.get('line','')}+")
            try:
                factors = json.loads(p.get("factors") or "{}")
            except Exception:
                factors = {}
            for k, v in factors.items():
                click.echo(f"    {k:15s}: {v:.3f}")
            news = p.get("news_summary", "")
            if news:
                click.echo(f"\n  Recent news:")
                for line in news.split("\n"):
                    click.echo(f"    {line}")


def _match_game(player_name: str, game_map: dict, games: list) -> dict | None:
    """Best-effort: find today's game relevant to a player (by team name match)."""
    # Without a player→team lookup we can't do this accurately here;
    # the edge engine handles it via nba_api commonplayerinfo.
    return None


if __name__ == "__main__":
    cli()
