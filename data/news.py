"""News and injury aggregation — ESPN + NBA official injury report."""
import re
from datetime import date
from typing import Optional

from data.espn import get_nba_news, get_player_news, get_team_roster_injuries


def get_injury_report(team_name: Optional[str] = None) -> list[dict]:
    """Return current injury report entries, optionally filtered by team."""
    # ESPN provides real-time roster injuries
    # We use a broad team search since we don't always have the ESPN team_id
    from data.espn import _get, BASE
    try:
        data = _get(f"{BASE}/injuries")
        injuries = []
        for team in data.get("injuries", []):
            t_name = team.get("team", {}).get("displayName", "")
            if team_name and team_name.lower() not in t_name.lower():
                continue
            for inj in team.get("injuries", []):
                athlete = inj.get("athlete", {})
                injuries.append({
                    "team":           t_name,
                    "player":         athlete.get("displayName", ""),
                    "position":       athlete.get("position", {}).get("abbreviation", ""),
                    "status":         inj.get("status", ""),
                    "description":    inj.get("longComment", inj.get("shortComment", "")),
                    "date":           inj.get("date", ""),
                })
        return injuries
    except Exception:
        return []


def get_player_injury_status(player_name: str) -> dict:
    """
    Return a dict with injury status and description for a player.

    Searches ESPN news and injury data.
    """
    news = get_player_news(player_name, limit=3)
    injury_keywords = ["injur", "out", "doubtful", "questionable", "probable",
                       "miss", "scratch", "day-to-day", "surgery", "rest"]

    status_text = ""
    description = ""
    for art in news:
        text = (art["headline"] + " " + art["description"]).lower()
        if any(kw in text for kw in injury_keywords):
            status_text = _extract_status_from_text(text)
            description = art["headline"]
            break

    return {
        "player":      player_name,
        "status":      status_text,
        "description": description,
        "news_items":  [a["headline"] for a in news],
    }


def _extract_status_from_text(text: str) -> str:
    if "ruled out" in text or " out " in text:
        return "Out"
    if "doubtful" in text:
        return "Doubtful"
    if "questionable" in text:
        return "Questionable"
    if "probable" in text:
        return "Probable"
    if "day-to-day" in text or "day to day" in text:
        return "Day-To-Day"
    if "rest" in text:
        return "Rest"
    return ""


def summarize_news_for_pick(player_name: str, opp_team: str) -> str:
    """Return a short news summary relevant to a betting pick."""
    player_news = get_player_news(player_name, limit=3)
    lines = []
    for art in player_news:
        headline = art["headline"]
        if len(headline) > 100:
            headline = headline[:97] + "..."
        lines.append(f"• {headline}")
    if not lines:
        lines.append("No recent news found.")
    return "\n".join(lines)


def get_team_news(team_name: str, limit: int = 5) -> list[dict]:
    """Return recent news items mentioning a team."""
    all_news = get_nba_news(limit=50)
    team_lower = team_name.lower()
    relevant = []
    for art in all_news:
        text = (art["headline"] + " " + art["description"]).lower()
        if team_lower in text:
            relevant.append(art)
        if len(relevant) >= limit:
            break
    return relevant
