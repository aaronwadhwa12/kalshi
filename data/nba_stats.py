"""NBA player stats via ESPN unofficial API — free, no auth, cloud-friendly."""
import time
from datetime import date
from typing import Optional

import pandas as pd

from data import espn as _espn

_player_id_cache: dict[str, Optional[str]] = {}   # name → ESPN athlete ID
_player_log_cache: dict[str, pd.DataFrame] = {}   # ESPN athlete ID → game log
_nba_stats_available: bool = True


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
# Player lookup via ESPN search
# ---------------------------------------------------------------------------

def find_player_id(name: str) -> Optional[str]:
    """Return ESPN athlete ID for a player name (cached)."""
    key = name.lower().strip()
    if key in _player_id_cache:
        return _player_id_cache[key]
    try:
        result = _espn.search_player(name)
        pid = result["id"] if result else None
    except Exception as e:
        print(f"[espn] Player lookup failed for '{name}': {e}")
        pid = None
    _player_id_cache[key] = pid
    return pid


def find_team_id(name: str) -> Optional[str]:
    return None


# ---------------------------------------------------------------------------
# ESPN gamelog → DataFrame
# ---------------------------------------------------------------------------

def _gamelog_to_df(espn_stats: list[dict]) -> pd.DataFrame:
    """Convert espn.get_player_recent_stats_espn output to nba_api-style DataFrame."""
    rows = []
    for g in espn_stats:
        try:
            game_date = pd.to_datetime(g.get("date", "")[:10])
        except Exception:
            continue
        is_home = g.get("home", False)
        team = "?"
        rows.append({
            "GAME_DATE": game_date,
            "MATCHUP":   f"{team} vs. ?" if is_home else f"{team} @ ?",
            "IS_HOME":   is_home,
            "PTS":       float(g.get("pts") or 0),
            "REB":       float(g.get("reb") or 0),
            "AST":       float(g.get("ast") or 0),
            "STL":       float(g.get("stl") or 0),
            "BLK":       float(g.get("blk") or 0),
            "TOV":       float(g.get("tov") or 0),
            "FG3M":      float(g.get("fg3m") or 0),
            "MIN":       float(g.get("min") or 0),
        })
    df = pd.DataFrame(rows)
    if not df.empty:
        df = df.sort_values("GAME_DATE", ascending=False).reset_index(drop=True)
    return df


# ---------------------------------------------------------------------------
# Per-player game log
# ---------------------------------------------------------------------------

def get_player_game_log(player_id: str, season: str = None,
                        last_n: int = 30) -> pd.DataFrame:
    if not _nba_stats_available:
        return pd.DataFrame()
    if player_id in _player_log_cache:
        return _player_log_cache[player_id].head(last_n)

    try:
        stats = _espn.get_player_recent_stats_espn(str(player_id), limit=60)
        df = _gamelog_to_df(stats)
        _player_log_cache[player_id] = df
        return df.head(last_n)
    except Exception as e:
        print(f"[espn] Game log failed for player {player_id}: {e}")
        return pd.DataFrame()


# ---------------------------------------------------------------------------
# Bulk warm (fetches each player individually — ESPN is fast, no IP blocking)
# ---------------------------------------------------------------------------

def warm_player_cache(player_ids: list, season: str = None):
    """Pre-fetch game logs for all players. ~300ms per player via ESPN."""
    global _nba_stats_available
    to_fetch = [pid for pid in player_ids if pid and pid not in _player_log_cache]
    if not to_fetch:
        print("  [espn] All players already cached.")
        return

    print(f"  [espn] Fetching game logs for {len(to_fetch)} players...")
    success = 0
    for pid in to_fetch:
        try:
            stats = _espn.get_player_recent_stats_espn(str(pid), limit=60)
            _player_log_cache[pid] = _gamelog_to_df(stats)
            success += 1
        except Exception as e:
            print(f"  [espn] Failed for player {pid}: {e}")
            _player_log_cache[pid] = pd.DataFrame()

    _nba_stats_available = success > 0
    print(f"  [espn] Cached {success}/{len(to_fetch)} players.")


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
