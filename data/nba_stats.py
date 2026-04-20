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
_history_loaded: bool = False
_nba_stats_available: bool = True

HISTORY_DAYS = 90   # covers full regular season + playoffs


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
    Each boxscore call takes ~300ms; ~3 games/day × 21 days = ~63 calls ≈ 20s total.
    """
    global _history_loaded, _nba_stats_available
    today = date.today()
    print(f"  [espn] Building player histories from past {days_back} days of boxscores...")
    games_loaded = 0

    for delta in range(days_back):
        game_date = today - timedelta(days=delta)
        try:
            completed = _espn.get_completed_games(game_date)
        except Exception:
            continue

        for game in completed:
            # get_todays_games returns home_team / away_team display names
            home_display = game.get("home_team", "")
            try:
                players = _espn.get_game_boxscore(game["event_id"])
            except Exception:
                continue

            for p in players:
                name_key = _normalize(p["name"])
                is_home = home_display and p.get("team", "") == home_display
                entry = {
                    "date":    game_date.isoformat(),
                    "team":    p.get("team", ""),
                    "is_home": is_home,
                    "pts":    float(p.get("pts") or 0),
                    "reb":    float(p.get("reb") or 0),
                    "ast":    float(p.get("ast") or 0),
                    "stl":    float(p.get("stl") or 0),
                    "blk":    float(p.get("blk") or 0),
                    "tov":    float(p.get("tov") or 0),
                    "fg3m":   float(p.get("fg3m") or 0),
                    "min":    float(p.get("min") or 0),
                }
                if name_key not in _player_history:
                    _player_history[name_key] = []
                _player_history[name_key].append(entry)
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
