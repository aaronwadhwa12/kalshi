"""
NBA player stats built from ESPN boxscores — no external API key, no IP blocking.

Instead of per-player game log endpoints (which require auth or block cloud IPs),
we fetch completed game boxscores from ESPN (confirmed working) going back N days
and assemble per-player histories from those.
"""
import time
from datetime import date, timedelta
from typing import Optional

import pandas as pd

from data import espn as _espn

# In-memory stores
_player_history: dict[str, list[dict]] = {}   # normalized_name → [{date, pts, reb, ...}]
_name_map: dict[str, str] = {}                # lookup_name → canonical_name in _player_history
_date_team_roster: dict[str, dict[str, set]] = {}  # date → team → {player_name} (who played)
_history_loaded: bool = False
_nba_stats_available: bool = True

HISTORY_DAYS = 185  # full 2025-26 season: Oct 2025 → Apr 2026


# ---------------------------------------------------------------------------
# Season helpers (kept for API compatibility)
# ---------------------------------------------------------------------------

def current_season() -> str:
    today = date.today()
    year = today.year if today.month >= 10 else today.year - 1
    return f"{year}-{str(year + 1)[2:]}"


def _prev_season(season: str) -> str:
    year = int(season.split("-")[0]) - 1
    return f"{year}-{str(year + 1)[2:]}"


# ---------------------------------------------------------------------------
# Build histories from ESPN boxscores
# ---------------------------------------------------------------------------

def _normalize(name: str) -> str:
    return name.lower().strip()


def _load_histories(days_back: int = HISTORY_DAYS):
    """
    Fetch ESPN boxscores for the past `days_back` days and build per-player histories.
    185 days × ~7 games/day × 300ms ≈ 6 min — within 20-min workflow timeout.
    """
    global _history_loaded, _nba_stats_available
    today = date.today()
    print(f"  [espn] Building player histories from past {days_back} days of boxscores...")
    games_loaded = 0

    for delta in range(days_back):
        game_date = today - timedelta(days=delta)
        if delta % 30 == 0 and delta > 0:
            print(f"  [espn]   ...{delta}/{days_back} days processed, {games_loaded} games so far")
        try:
            completed = _espn.get_completed_games(game_date)
        except Exception:
            continue

        for game in completed:
            home_display = game.get("home_team", "")
            try:
                time.sleep(0.05)  # gentle rate limit — ~1200 requests over 6 min
                players = _espn.get_game_boxscore(game["event_id"])
            except Exception:
                continue

            date_str = game_date.isoformat()
            for p in players:
                name_key = _normalize(p["name"])
                team_name = p.get("team", "")
                is_home = home_display and team_name == home_display
                mins = float(p.get("min") or 0)
                entry = {
                    "date":    date_str,
                    "team":    team_name,
                    "is_home": is_home,
                    "pts":    float(p.get("pts") or 0),
                    "reb":    float(p.get("reb") or 0),
                    "ast":    float(p.get("ast") or 0),
                    "stl":    float(p.get("stl") or 0),
                    "blk":    float(p.get("blk") or 0),
                    "tov":    float(p.get("tov") or 0),
                    "fg3m":   float(p.get("fg3m") or 0),
                    "min":    mins,
                }
                if name_key not in _player_history:
                    _player_history[name_key] = []
                _player_history[name_key].append(entry)
                # Track roster: only count players who actually played (min > 0)
                if mins > 0 and team_name:
                    if date_str not in _date_team_roster:
                        _date_team_roster[date_str] = {}
                    if team_name not in _date_team_roster[date_str]:
                        _date_team_roster[date_str][team_name] = set()
                    _date_team_roster[date_str][team_name].add(name_key)
            games_loaded += 1

    _history_loaded = True
    _nba_stats_available = len(_player_history) > 0
    print(f"  [espn] Loaded {games_loaded} games | {len(_player_history)} players tracked")


def _build_name_map(kalshi_names: list[str]):
    """Map Kalshi player names to the closest ESPN boxscore name."""
    global _name_map
    for name in kalshi_names:
        key = _normalize(name)
        if key in _player_history:
            _name_map[key] = key
            continue
        # Try last-name + first-initial match
        parts = key.split()
        if len(parts) >= 2:
            last = parts[-1].rstrip(".")
            first_init = parts[0][0]
            for espn_key in _player_history:
                e_parts = espn_key.split()
                if len(e_parts) >= 2:
                    e_last = e_parts[-1].rstrip(".")
                    e_first_init = e_parts[0][0]
                    if e_last == last and e_first_init == first_init:
                        _name_map[key] = espn_key
                        break
            else:
                # Last-name only fallback
                for espn_key in _player_history:
                    e_parts = espn_key.split()
                    if e_parts and e_parts[-1].rstrip(".") == last:
                        _name_map[key] = espn_key
                        break


# ---------------------------------------------------------------------------
# Public API (same signatures as original nba_stats.py)
# ---------------------------------------------------------------------------

def find_player_id(name: str) -> Optional[str]:
    """Return a lookup key for this player. Works after warm_player_cache()."""
    key = _normalize(name)
    # Check direct match and mapped match
    if key in _player_history:
        return key
    mapped = _name_map.get(key)
    if mapped and mapped in _player_history:
        return mapped
    return None


def get_player_game_log(player_id: str, season: str = None,
                        last_n: int = 30) -> pd.DataFrame:
    if not _nba_stats_available:
        return pd.DataFrame()
    games = _player_history.get(str(player_id), [])
    if not games:
        return pd.DataFrame()

    games_sorted = sorted(games, key=lambda g: g["date"], reverse=True)[:last_n]
    rows = [{
        "GAME_DATE": pd.to_datetime(g["date"]),
        "MATCHUP":   "? vs. ?" if g["is_home"] else "? @ ?",
        "IS_HOME":   g["is_home"],
        "PTS":       g["pts"],
        "REB":       g["reb"],
        "AST":       g["ast"],
        "STL":       g["stl"],
        "BLK":       g["blk"],
        "TOV":       g["tov"],
        "FG3M":      g["fg3m"],
        "MIN":       g["min"],
    } for g in games_sorted]
    return pd.DataFrame(rows)


def warm_player_cache(player_ids: list, season: str = None):
    """Load all player histories from ESPN boxscores (fetches all at once)."""
    global _history_loaded
    if not _history_loaded:
        _load_histories()
    # player_ids here are Kalshi player names; build the name map
    _build_name_map([str(p) for p in player_ids if p])
    found = sum(1 for p in player_ids if find_player_id(str(p)))
    print(f"  [espn] Name map built: {found}/{len(player_ids)} players matched")


# ---------------------------------------------------------------------------
# Stubs for API compatibility
# ---------------------------------------------------------------------------

def load_season_game_logs(season: str = None) -> pd.DataFrame:
    return pd.DataFrame()


def get_team_defensive_ratings(season: str = None) -> pd.DataFrame:
    return pd.DataFrame()


def get_opponent_def_rating(opp_team_name: str, stat_type: str,
                            season: str = None) -> float:
    return 1.0


def get_player_home_away_split(df: pd.DataFrame, stat_cols: list[str]) -> dict:
    if df.empty or "IS_HOME" not in df.columns:
        return {"home": {c: 0.0 for c in stat_cols},
                "away": {c: 0.0 for c in stat_cols}}
    home = df[df["IS_HOME"] == True][stat_cols].mean()
    away = df[df["IS_HOME"] == False][stat_cols].mean()
    return {"home": home.to_dict(), "away": away.to_dict()}


def get_days_rest(df: pd.DataFrame, upcoming_date=None) -> int:
    if df.empty or "GAME_DATE" not in df.columns:
        return 2
    most_recent = df["GAME_DATE"].iloc[0]
    ref = upcoming_date or pd.Timestamp.today()
    return max(0, (ref - most_recent).days)


def get_player_team(name: str) -> str:
    """Return the most recent team name for a player from their history."""
    key = _normalize(name)
    mapped = _name_map.get(key, key)
    games = _player_history.get(mapped, [])
    if not games:
        return ""
    recent = sorted(games, key=lambda g: g["date"], reverse=True)
    return recent[0].get("team", "")


def get_minutes_impact(player_name: str, absent_player_name: str) -> tuple[float, int]:
    """
    Compare player's average minutes in games where absent_player_name was out
    vs. games when they played together.

    Returns (factor, n_absent_games) where factor > 1 means the player plays
    more minutes when that teammate is absent.  Returns (1.0, 0) if not enough
    data to compute a meaningful estimate.
    """
    p_key = _normalize(player_name)
    p_key = _name_map.get(p_key, p_key)
    absent_key = _normalize(absent_player_name)
    absent_key = _name_map.get(absent_key, absent_key)

    games = _player_history.get(p_key, [])
    if not games:
        return 1.0, 0

    mins_with: list[float] = []
    mins_without: list[float] = []

    for g in games:
        mins = g["min"]
        if mins < 1:  # DNP — skip
            continue
        date_str = g["date"]
        team = g["team"]
        roster = _date_team_roster.get(date_str, {}).get(team, set())
        if absent_key in roster:
            mins_with.append(mins)
        else:
            mins_without.append(mins)

    n_with    = len(mins_with)
    n_without = len(mins_without)

    if n_with < 3 or n_without < 3:
        return 1.0, n_without

    avg_with    = sum(mins_with)    / n_with
    avg_without = sum(mins_without) / n_without

    if avg_with < 1:
        return 1.0, n_without

    factor = avg_without / avg_with
    factor = max(0.75, min(1.40, factor))  # cap at ±25-40%
    return round(factor, 3), n_without


def find_team_id(name: str) -> Optional[str]:
    return None


def compute_composite_stat(df: pd.DataFrame, stat_type: str) -> pd.Series:
    combos = {
        "pra": ["PTS", "REB", "AST"],
        "pr":  ["PTS", "REB"],
        "pa":  ["PTS", "AST"],
        "ra":  ["REB", "AST"],
    }
    cols = combos.get(stat_type, [])
    if not cols:
        raise ValueError(f"Unknown composite stat: {stat_type}")
    available = [c for c in cols if c in df.columns]
    return df[available].sum(axis=1)
