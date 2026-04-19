"""iMessage notifications via macOS osascript (Messages app)."""
import subprocess
import sys
from datetime import datetime
from typing import Optional

import config


def _escape_applescript(text: str) -> str:
    """Escape a string for embedding inside an AppleScript double-quoted string."""
    text = text.replace("\\", "\\\\").replace('"', '\\"')
    # AppleScript doesn't understand \n — join with the return character keyword
    parts = text.split("\n")
    return '" & return & "'.join(parts)


def send_imessage(body: str, to: str = None) -> bool:
    """
    Send an iMessage via the macOS Messages app using osascript.

    Returns True on success, False on failure.
    Requires the script to run on macOS with Messages signed in to iMessage.
    """
    recipient = to or config.IMESSAGE_TO
    if not recipient:
        raise RuntimeError("IMESSAGE_TO not set in .env")

    escaped = _escape_applescript(body)
    script = f'''
tell application "Messages"
    set targetService to 1st service whose service type = iMessage
    set targetBuddy to buddy "{recipient}" of targetService
    send "{escaped}" to targetBuddy
end tell
'''
    result = subprocess.run(
        ["osascript", "-e", script],
        capture_output=True,
        text=True,
    )
    if result.returncode != 0:
        raise RuntimeError(
            f"osascript failed (rc={result.returncode}): {result.stderr.strip()}"
        )
    return True


# ---------------------------------------------------------------------------
# Message formatters (shared with SMS alias for backward compat)
# ---------------------------------------------------------------------------

def format_picks_sms(picks: list[dict], game_date: str = None) -> str:
    date_str = game_date or datetime.today().strftime("%b %d, %Y")
    lines = [f"NBA Kalshi Edge Alerts — {date_str}", ""]

    if not picks:
        lines.append("No strong edges found today.")
        lines.append("Check again closer to tip-off.")
        return "\n".join(lines)

    lines.append(f"TOP {len(picks)} PICK(S):")
    lines.append("")

    for i, pick in enumerate(picks, 1):
        edge_pct = round(pick["edge"] * 100, 1)
        our_pct  = round(pick["our_probability"] * 100, 1)
        imp_pct  = round(pick["implied_probability"] * 100, 1)
        side     = pick.get("side", "yes").upper()
        conf     = pick.get("confidence", "?").upper()
        line_val = pick.get("line", "?")
        stat     = pick.get("stat_type", "?").upper()
        player   = pick.get("player_name", "?")
        pick_id  = pick.get("pick_id", "?")
        tip_off  = _format_tipoff(pick.get("close_time", ""))

        lines.append(f"{i}. [#{pick_id}] {player}")
        lines.append(f"   {stat} {side} {line_val}+ @ {imp_pct}c")
        lines.append(f"   Our prob: {our_pct}% | Edge: +{edge_pct}%")
        lines.append(f"   Confidence: {conf} | Tip: {tip_off}")
        lines.append("")

    lines.append("To track: python main.py bet <pick_id> <contracts>")
    return "\n".join(lines)


def format_resolution_sms(resolved_bets: list[dict]) -> str:
    if not resolved_bets:
        return "No bets resolved recently."

    lines = ["NBA Kalshi Results", ""]
    total_pnl = 0.0
    for bet in resolved_bets:
        pnl     = bet.get("profit_loss", 0) or 0
        total_pnl += pnl
        outcome = (bet.get("outcome") or "?").upper()
        player  = bet.get("player_name", "?")
        stat    = bet.get("stat_type", "?").upper()
        line    = bet.get("line", "?")
        icon    = "WIN" if outcome == "WON" else ("LOSS" if outcome == "LOST" else "?")
        lines.append(f"{icon}  {player} {stat} {line}+ -> {pnl:+.2f}")

    lines.append("")
    lines.append(f"Net P&L: ${total_pnl:+.2f}")
    return "\n".join(lines)


def format_daily_summary_sms(summary: dict) -> str:
    total   = summary.get("total_bets", 0)
    wins    = summary.get("wins", 0)
    losses  = summary.get("losses", 0)
    pending = summary.get("pending", 0)
    pnl     = summary.get("total_pnl", 0.0) or 0.0
    avg     = summary.get("avg_pnl", 0.0) or 0.0
    win_pct = round(wins / max(wins + losses, 1) * 100, 1)

    return "\n".join([
        "NBA Kalshi Portfolio Summary",
        "",
        f"Total bets: {total}",
        f"Record:     {wins}W-{losses}L ({win_pct}%)",
        f"Pending:    {pending}",
        f"Net P&L:    ${pnl:+.2f}",
        f"Avg/bet:    ${avg:+.2f}",
    ])


def _format_tipoff(close_time: str) -> str:
    if not close_time:
        return "TBD"
    try:
        dt = datetime.fromisoformat(close_time.replace("Z", "+00:00"))
        import pytz
        dt_et = dt.astimezone(pytz.timezone("America/New_York"))
        return dt_et.strftime("%-I:%M %p ET")
    except Exception:
        return close_time[:10]


def send_picks_alert(picks: list[dict], game_date: str = None) -> Optional[bool]:
    """Format and send an iMessage with top picks. Always prints to console too."""
    body = format_picks_sms(picks, game_date)
    print(body)
    try:
        return send_imessage(body)
    except RuntimeError as e:
        print(f"[iMessage] {e}")
        return None


# Alias so callers using the old send_sms name still work
def send_sms(body: str, to: str = None) -> bool:
    return send_imessage(body, to)
