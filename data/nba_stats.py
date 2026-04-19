"""NBA player stats via BallDontLie API — reliable from cloud/GitHub Actions."""
import time
from collections import defaultdict
from datetime import date
from typing import Optional

import requests
import pandas as pd

import config

_BDL_BASE = "https://api.balldontlie.io/v1"
_REQUEST_DELAY = 0.3  # BDL has generous rate limits vs stats.nba.com

_player_id_cache: dict[str, Optional[int]] = {}  # name → BDL player ID
_player_log_cache: dict[int, pd.DataFrame] = {}  # BDL player ID → game log
_nba_stats_available: bool = True


def _headers() -> dict:
    key = config.BALLDONTLIE_API_KEY
    if not key:
        raise RuntimeError("BALLDONTLIE_API_KEY not set in .env / GitHub Secrets")
    return {"Authorization": key}


def _get(path: str, params=None) -> dict:
    time.sleep(_REQUEST_DELAY)
    resp = requests.get(f"{_BDL_BASE}{path}", params=params,
                        headers=_headers(), timeout=15)
    resp.raise_for_status()
    return resp.json()


# ---------------------------------------------------------------------------
# Season helpers
# ---------------------------------------------------------------------------

def current_season() -> str:
    today = date.today()
    year = today.year if today.month >= 10 else today.year - 1
    return f"{year}-{str(year + 1)[2:]}"


def _season_year() -> int:
    today = date.today()
    return today.year if today.month >= 10 else today.year - 1


def _prev_season(season: str) -> str:
    year = int(season.split("-")[0]) - 1
    return f"{year}-{str(year + 1)[2:]}"


# ---------------------------------------------------------------------------
# Player lookup
# ---------------------------------------------------------------------------

def find_player_id(name: str) -> Optional[int]:
    """Search BDL for a player by name, return their BDL player ID."""
    name_lower = name.lower().strip()
    if name_lower in _player_id_cache:
        return _player_id_cache[name_lower]

    try:
        data = _get("/players", [("search", name), ("per_page", 5)])
        players = data.get("data", [])

        for p in players:
            full = f"{p['first_name']} {p['last_name']}".lower()
            if full == name_lower:
                _player_id_cache[name_lower] = p["id"]
                return p["id"]

        last = name_lower.split()[-1]
        for p in players:
            full = f"{p['first_name']} {p['last_name']}".lower()
            if last in full:
                _player_id_cache[name_lower] = p["id"]
                return p["id"]
    except Exception as e:
        print(f"[bdl] Player lookup failed for '{name}': {e}")

    _player_id_cache[name_lower] = None
    return None


def find_team_id(name: str) -> Optional[int]:
    return None  # not needed for live scan


# ---------------------------------------------------------------------------
# Stats → DataFrame conversion
# ---------------------------------------------------------------------------

def _stats_to_df(stats: list[dict]) -> pd.DataFrame:
    """Convert BDL /stats rows to a DataFrame matching nba_api column names."""
    rows = []
    for s in stats:
        g = s.get("game", {})
        t = s.get("team", {})
        is_home = g.get("home_team_id") == t.get("id")
        team_abbr = t.get("abbreviation", "?")
        rows.append({
            "GAME_DATE": pd.to_datetime(g.get("date", "")[:10]) if g.get("date") else pd.NaT,
            "MATCHUP":   f"{team_abbr} vs. ?" if is_home else f"{team_abbr} @ ?",
            "IS_HOME":   is_home,
            "PTS":       float(s.get("pts") or 0),
            "REB":       float(s.get("reb") or 0),
            "AST":       float(s.get("ast") or 0),
            "STL":       float(s.get("stl") or 0),
            "BLK":       float(s.get("blk") or 0),
            "TOV":       float(s.get("turnover") or 0),
            "FG3M":      float(s.get("fg3m") or 0),
            "MIN":       s.get("min", ""),
        })
    df = pd.DataFrame(rows)
    if not df.empty:
        df = df.dropna(subset=["GAME_DATE"])
        df = df.sort_values("GAME_DATE", ascending=False).reset_index(drop=True)
    return df


# ---------------------------------------------------------------------------
# Bulk warm (used by scan_and_alert + backtest)
# ---------------------------------------------------------------------------

def warm_player_cache(player_ids: list, season: str = None):
    """
    Fetch game logs for all player IDs in batched BDL requests.
    Much faster and more reliable than N individual stats.nba.com calls.
    """
    global _nba_stats_available
    year = _season_year()
    to_fetch = [pid for pid in player_ids if pid and pid not in _player_log_cache]
    if not to_fetch:
        print("  [bdl] All players already cached.")
        return

    print(f"  [bdl] Fetching stats for {len(to_fetch)} players (seasons {year-1},{year})...")
    try:
        param_list = [
            ("seasons[]", year),
            ("seasons[]", year - 1),
            ("per_page", 100),
        ]
        for pid in to_fetch:
            param_list.append(("player_ids[]", pid))

        all_stats = []
        cursor = None
        pages = 0
        while True:
            p = list(param_list)
            if cursor:
                p.append(("cursor", cursor))
            time.sleep(_REQUEST_DELAY)
            resp = requests.get(f"{_BDL_BASE}/stats", params=p,
                                headers=_headers(), timeout=20)
            resp.raise_for_status()
            data = resp.json()
            all_stats.extend(data.get("data", []))
            cursor = data.get("meta", {}).get("next_cursor")
            pages += 1
            if not cursor:
                break

        print(f"  [bdl] Got {len(all_stats)} stat rows across {pages} page(s)")

        by_player: dict[int, list] = defaultdict(list)
        for s in all_stats:
            pid = s.get("player", {}).get("id")
            if pid:
                by_player[pid].append(s)

        for pid, stats in by_player.items():
            _player_log_cache[pid] = _stats_to_df(stats)

        _nba_stats_available = True
        print(f"  [bdl] Cached {len(by_player)} players.")
    except Exception as e:
        _nba_stats_available = False
        print(f"  [bdl] Bulk fetch failed: {e}")
        raise


# ---------------------------------------------------------------------------
# Per-player game log (uses cache populated by warm_player_cache)
# ---------------------------------------------------------------------------

def get_player_game_log(player_id: int, season: str = None,
                        last_n: int = 30) -> pd.DataFrame:
    if not _nba_stats_available:
        return pd.DataFrame()
    if player_id in _player_log_cache:
        return _player_log_cache[player_id].head(last_n)

    # Not in cache — fetch individually (fallback for backtest single-player calls)
    year = _season_year()
    try:
        param_list = [
            ("player_ids[]", player_id),
            ("seasons[]", year),
            ("seasons[]", year - 1),
            ("per_page", 100),
        ]
        all_stats = []
        cursor = None
        while True:
            p = list(param_list)
            if cursor:
                p.append(("cursor", cursor))
            time.sleep(_REQUEST_DELAY)
            resp = requests.get(f"{_BDL_BASE}/stats", params=p,
                                headers=_headers(), timeout=15)
            resp.raise_for_status()
            data = resp.json()
            all_stats.extend(data.get("data", []))
            cursor = data.get("meta", {}).get("next_cursor")
            if not cursor:
                break

        df = _stats_to_df(all_stats)
        _player_log_cache[player_id] = df
        return df.head(last_n)
    except Exception as e:
        print(f"[bdl] Individual fetch failed for player {player_id}: {e}")
        return pd.DataFrame()


# ---------------------------------------------------------------------------
# Stubs kept for API compatibility (team defense not on BDL free tier)
# ---------------------------------------------------------------------------

def load_season_game_logs(season: str = None) -> pd.DataFrame:
    return pd.DataFrame()


def get_team_defensive_ratings(season: str = None) -> pd.DataFrame:
    return pd.DataFrame()


def get_opponent_def_rating(opp_team_name: str, stat_type: str,
                            season: str = None) -> float:
    return 1.0  # neutral — advanced defensive stats not on BDL free tier


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
