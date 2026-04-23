"""ESPN unofficial API client for player stats, injuries, news, and scoreboard."""
import re
import time
import requests
from datetime import date
from typing import Optional

BASE = "https://site.api.espn.com/apis/site/v2/sports/basketball/nba"
CORE = "https://sports.core.api.espn.com/v2/sports/basketball/leagues/nba"
WEB  = "https://site.web.api.espn.com/apis/common/v3"

_session = requests.Session()
_session.headers.update({"User-Agent": "Mozilla/5.0"})

_cache: dict = {}


def _get(url: str, params: dict = None, cache_ttl: int = 300) -> dict:
    key = (url, str(params))
    if key in _cache:
        data, ts = _cache[key]
        if time.time() - ts < cache_ttl:
            return data
    resp = _session.get(url, params=params, timeout=15)
    resp.raise_for_status()
    data = resp.json()
    _cache[key] = (data, time.time())
    return data


def get_todays_games(game_date: Optional[date] = None) -> list[dict]:
    """Return list of today's NBA games with team names and tip-off times."""
    date_str = (game_date or date.today()).strftime("%Y%m%d")
    data = _get(f"{BASE}/scoreboard", params={"dates": date_str})
    games = []
    for event in data.get("events", []):
        comp = event["competitions"][0]
        teams = {t["homeAway"]: t["team"] for t in comp["competitors"]}
        games.append({
            "event_id":   event["id"],
            "home_team":  teams.get("home", {}).get("displayName", ""),
            "away_team":  teams.get("away", {}).get("displayName", ""),
            "home_abbr":  teams.get("home", {}).get("abbreviation", ""),
            "away_abbr":  teams.get("away", {}).get("abbreviation", ""),
            "tip_off":    comp.get("date", ""),
            "venue":      comp.get("venue", {}).get("fullName", ""),
            "status":     event["status"]["type"]["name"],
        })
    return games


def search_player(name: str) -> Optional[dict]:
    """Search for a player by name and return basic info."""
    data = _get(f"{WEB}/search", params={"query": name, "sport": "basketball", "league": "nba"})
    for hit in data.get("results", []):
        for item in hit.get("contents", []):
            if item.get("type") == "athlete":
                return {
                    "id":     item["id"],
                    "name":   item.get("displayName", name),
                    "team":   item.get("teamName", ""),
                    "team_id": item.get("teamId", ""),
                }
    return None


def get_player_info(player_id: str) -> dict:
    """Return player info including current injury status."""
    data = _get(f"{BASE}/athletes/{player_id}")
    athlete = data.get("athlete", {})
    injuries = athlete.get("injuries", [])
    injury_status = None
    injury_desc = None
    if injuries:
        latest = injuries[0]
        injury_status = latest.get("status", "")
        injury_desc = latest.get("longComment", latest.get("shortComment", ""))
    return {
        "id":             athlete.get("id", player_id),
        "name":           athlete.get("fullName", ""),
        "position":       athlete.get("position", {}).get("abbreviation", ""),
        "team_id":        athlete.get("team", {}).get("id", ""),
        "team_name":      athlete.get("team", {}).get("displayName", ""),
        "injury_status":  injury_status,
        "injury_desc":    injury_desc,
    }


def get_team_roster_injuries(team_id: str) -> list[dict]:
    """Return all injured players on a team roster."""
    data = _get(f"{BASE}/teams/{team_id}/roster")
    injured = []
    for athlete in data.get("athletes", []):
        for player in athlete.get("items", []):
            inj = player.get("injuries", [])
            if inj:
                injured.append({
                    "name":           player.get("fullName", ""),
                    "position":       player.get("position", {}).get("abbreviation", ""),
                    "injury_status":  inj[0].get("status", ""),
                    "injury_desc":    inj[0].get("longComment", ""),
                })
    return injured


def get_nba_news(limit: int = 20) -> list[dict]:
    """Return recent NBA news headlines."""
    data = _get(f"{BASE}/news", params={"limit": limit}, cache_ttl=600)
    articles = []
    for art in data.get("articles", []):
        articles.append({
            "headline":    art.get("headline", ""),
            "description": art.get("description", ""),
            "published":   art.get("published", ""),
            "categories":  [c.get("description", "") for c in art.get("categories", [])],
            "athletes":    [a.get("description", "") for a in art.get("categories", [])
                            if a.get("type") == "athlete"],
        })
    return articles


def get_player_news(player_name: str, limit: int = 5) -> list[dict]:
    """Filter news for a specific player by name."""
    all_news = get_nba_news(limit=50)
    name_lower = player_name.lower()
    relevant = []
    for art in all_news:
        text = (art["headline"] + " " + art["description"]).lower()
        if name_lower in text or any(name_lower in a.lower() for a in art["athletes"]):
            relevant.append(art)
        if len(relevant) >= limit:
            break
    return relevant


def get_player_recent_stats_espn(player_id: str, limit: int = 10) -> list[dict]:
    """Return recent game stats for a player from ESPN."""
    data = _get(f"{BASE}/athletes/{player_id}/gamelog")
    games = []
    for season in data.get("seasonTypes", []):
        for cat in season.get("categories", []):
            if cat.get("type") == "event":
                for event in cat.get("events", []):
                    stats = {s["name"]: s["value"] for s in event.get("stats", [])}
                    games.append({
                        "date":   event.get("gameDate", ""),
                        "opp":    event.get("opponent", {}).get("displayName", ""),
                        "home":   event.get("homeAway", "") == "home",
                        "pts":    stats.get("points", 0),
                        "reb":    stats.get("rebounds", 0),
                        "ast":    stats.get("assists", 0),
                        "stl":    stats.get("steals", 0),
                        "blk":    stats.get("blocks", 0),
                        "tov":    stats.get("turnovers", 0),
                        "fg3m":   stats.get("threePointFieldGoalsMade", 0),
                        "min":    stats.get("minutes", 0),
                    })
    games.sort(key=lambda g: g["date"], reverse=True)
    return games[:limit]


def get_game_boxscore(event_id: str) -> list[dict]:
    """
    Return player stats for a completed game.

    Each entry: {name, team, min, pts, reb, ast, stl, blk, tov, fg3m}
    """
    data = _get(f"{BASE}/summary", params={"event": event_id}, cache_ttl=60)
    players = []
    for team_block in data.get("boxscore", {}).get("players", []):
        team_name = team_block.get("team", {}).get("displayName", "")
        for stat_block in team_block.get("statistics", []):
            labels = [l.upper() for l in stat_block.get("labels", [])]
            for athlete_entry in stat_block.get("athletes", []):
                athlete = athlete_entry.get("athlete", {})
                raw_stats = athlete_entry.get("stats", [])
                if not raw_stats:
                    continue
                stat_map = dict(zip(labels, raw_stats))
                def _f(key, default=0.0):
                    try:
                        return float(stat_map.get(key, default))
                    except (ValueError, TypeError):
                        return default
                min_played = _f("MIN")
                if min_played < 10:
                    continue
                players.append({
                    "name":  athlete.get("displayName", ""),
                    "id":    athlete.get("id", ""),
                    "team":  team_name,
                    "min":   min_played,
                    "pts":   _f("PTS"),
                    "reb":   _f("REB"),
                    "ast":   _f("AST"),
                    "stl":   _f("STL"),
                    "blk":   _f("BLK"),
                    "tov":   _f("TO"),
                    "fg3m":  _f("3PT") or _f("FG3M"),
                    "pra":   _f("PTS") + _f("REB") + _f("AST"),
                })
    return players


def get_completed_games(game_date: Optional[date] = None) -> list[dict]:
    """Return completed games for a given date with event IDs."""
    games = get_todays_games(game_date)
    return [g for g in games if g["status"] in (
        "STATUS_FINAL", "STATUS_FINAL_OT", "Final", "final"
    )]


def parse_injury_impact(player_info: dict) -> float:
    """Return a probability multiplier based on injury status (1.0 = no injury)."""
    status = (player_info.get("injury_status") or "").lower()
    if "out" in status:
        return 0.0
    if "doubtful" in status:
        return 0.4
    if "questionable" in status:
        return 0.8
    if "probable" in status:
        return 0.95
    if "day-to-day" in status:
        return 0.85
    return 1.0
