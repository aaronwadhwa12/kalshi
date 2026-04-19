"""Twilio SMS notifications for edge alerts."""
import json
from datetime import datetime
from typing import Optional

import config

try:
    from twilio.rest import Client as TwilioClient
    _twilio_available = True
except ImportError:
    _twilio_available = False


def _client() -> "TwilioClient":
    if not _twilio_available:
        raise RuntimeError("twilio package not installed. Run: pip install twilio")
    if not config.TWILIO_ACCOUNT_SID or not config.TWILIO_AUTH_TOKEN:
        raise RuntimeError("TWILIO_ACCOUNT_SID and TWILIO_AUTH_TOKEN must be set in .env")
    return TwilioClient(config.TWILIO_ACCOUNT_SID, config.TWILIO_AUTH_TOKEN)


def send_sms(body: str, to: str = None) -> str:
    """Send an SMS and return the message SID."""
    to = to or config.TWILIO_TO_NUMBER
    if not to:
        raise RuntimeError("TWILIO_TO_NUMBER not set in .env")
    msg = _client().messages.create(
        body=body,
        from_=config.TWILIO_FROM_NUMBER,
        to=to,
    )
    return msg.sid


def format_picks_sms(picks: list[dict], game_date: str = None) -> str:
    """Format a list of top picks into an SMS-friendly message."""
    date_str = game_date or datetime.today().strftime("%b %d, %Y")
    lines = [f"NBA Kalshi Edge Alerts — {date_str}", ""]

    if not picks:
        lines.append("No strong edges found today.")
        lines.append("Check again closer to tip-off.")
        return "\n".join(lines)

    lines.append(f"TOP {len(picks)} PICK(S):")
    lines.append("")

    for i, pick in enumerate(picks, 1):
        edge_pct  = round(pick["edge"] * 100, 1)
        our_pct   = round(pick["our_probability"] * 100, 1)
        imp_pct   = round(pick["implied_probability"] * 100, 1)
        side      = pick.get("side", "yes").upper()
        conf      = pick.get("confidence", "?").upper()
        line_val  = pick.get("line", "?")
        stat      = pick.get("stat_type", "?").upper()
        player    = pick.get("player_name", "?")
        pick_id   = pick.get("pick_id", "?")
        tip_off   = _format_tipoff(pick.get("close_time", ""))

        lines.append(f"{i}. [#{pick_id}] {player}")
        lines.append(f"   {stat} {side} {line_val}+ @ {imp_pct}c")
        lines.append(f"   Our prob: {our_pct}% | Edge: +{edge_pct}%")
        lines.append(f"   Confidence: {conf} | Tip: {tip_off}")
        lines.append("")

    lines.append("To track: python main.py bet <pick_id> <contracts>")
    return "\n".join(lines)


def format_resolution_sms(resolved_bets: list[dict]) -> str:
    """Format resolved bet outcomes into an SMS."""
    if not resolved_bets:
        return "No bets resolved recently."

    lines = ["NBA Kalshi Results", ""]
    total_pnl = 0.0
    for bet in resolved_bets:
        pnl = bet.get("profit_loss", 0) or 0
        total_pnl += pnl
        outcome = (bet.get("outcome") or "?").upper()
        player  = bet.get("player_name", "?")
        stat    = bet.get("stat_type", "?").upper()
        line    = bet.get("line", "?")
        icon    = "WIN" if outcome == "WON" else ("LOSS" if outcome == "LOST" else "?")
        lines.append(f"{icon}  {player} {stat} {line}+ → {pnl:+.2f}")

    lines.append("")
    lines.append(f"Net P&L: ${total_pnl:+.2f}")
    return "\n".join(lines)


def format_daily_summary_sms(summary: dict) -> str:
    """Format portfolio P&L summary into an SMS."""
    total   = summary.get("total_bets", 0)
    wins    = summary.get("wins", 0)
    losses  = summary.get("losses", 0)
    pending = summary.get("pending", 0)
    pnl     = summary.get("total_pnl", 0.0) or 0.0
    avg     = summary.get("avg_pnl", 0.0) or 0.0
    win_pct = round(wins / max(wins + losses, 1) * 100, 1)

    lines = [
        "NBA Kalshi Portfolio Summary",
        "",
        f"Total bets: {total}",
        f"Record:     {wins}W-{losses}L ({win_pct}%)",
        f"Pending:    {pending}",
        f"Net P&L:    ${pnl:+.2f}",
        f"Avg/bet:    ${avg:+.2f}",
    ]
    return "\n".join(lines)


def _format_tipoff(close_time: str) -> str:
    if not close_time:
        return "TBD"
    try:
        from datetime import timezone
        dt = datetime.fromisoformat(close_time.replace("Z", "+00:00"))
        import pytz
        et = pytz.timezone("America/New_York")
        dt_et = dt.astimezone(et)
        return dt_et.strftime("%-I:%M %p ET")
    except Exception:
        return close_time[:10]


def send_picks_alert(picks: list[dict], game_date: str = None) -> Optional[str]:
    """Format and send an SMS with top picks. Returns message SID or None."""
    body = format_picks_sms(picks, game_date)
    print(body)  # always print regardless of Twilio config
    try:
        return send_sms(body)
    except RuntimeError as e:
        print(f"[SMS] Skipped: {e}")
        return None
