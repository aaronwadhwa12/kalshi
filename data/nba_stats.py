"""NBA API wrapper for historical player game logs, team defense, and standings."""
import time
import functools
import pandas as pd
from datetime import date
from typing import Optional

from nba_api.stats.endpoints import (
    playergamelog,
    leaguegamelog,
    commonplayerinfo,
    leaguedashteamstats,
    leaguedashplayerstats,
    teamgamelog,
)
from nba_api.stats.static import players as static_players, teams as static_teams
from nba_api.stats.library.parameters import SeasonAll


_player_cache: dict = {}
_team_def_cache: dict = {}
_bulk_log_cache: dict = {}  # season → full DataFrame of all player-games

_REQUEST_DELAY = 0.6  # seconds between NBA API requests (rate limiting)


def _nba_request(fn, *args, **kwargs):
    time.sleep(_REQUEST_DELAY)
    return fn(*args, **kwargs)


def find_player_id(name: str) -> Optional[int]:
    """Fuzzy-match a player name and return their NBA API player ID."""
    name_lower = name.lower().strip()
    all_players = static_players.get_players()
    for p in all_players:
        if p["full_name"].lower() == name_lower:
            return p["id"]
    # partial match
    matches = [p for p in all_players if name_lower in p["full_name"].lower()]
    if len(matches) == 1:
        return matches[0]["id"]
    if len(matches) > 1:
        # prefer active players
        active = [p for p in matches if p.get("is_active")]
        if active:
            return active[0]["id"]
        return matches[0]["id"]
    return None


def current_season() -> str:
    """Return the current NBA season string (e.g. '2025-26') based on today's date."""
    today = date.today()
    year = today.year if today.month >= 10 else today.year - 1
    return f"{year}-{str(year + 1)[2:]}"


def find_team_id(name: str) -> Optional[int]:
    name_lower = name.lower()
    for t in static_teams.get_teams():
        if (name_lower in t["full_name"].lower()
                or name_lower in t["nickname"].lower()
                or name_lower in t["abbreviation"].lower()):
            return t["id"]
    return None


def load_season_game_logs(season: str = None) -> pd.DataFrame:
    """
    Fetch every player-game for a full season in ONE API call.

    This is the fast path used by warm_player_cache — replaces ~50 individual
    PlayerGameLog requests with 2 LeagueGameLog requests (current + prev season).
    """
    if season is None:
        season = current_season()
    if season in _bulk_log_cache:
        return _bulk_log_cache[season]
    print(f"  [nba_api] Bulk-loading {season} game logs (1 request)...")
    log = _nba_request(
        leaguegamelog.LeagueGameLog,
        season=season,
        player_or_team_abbreviation="P",
        season_type_all_star="Regular Season",
    )
    df = log.get_data_frames()[0]
    df["GAME_DATE"] = pd.to_datetime(df["GAME_DATE"])
    df["IS_HOME"]   = ~df["MATCHUP"].str.contains("@")
    _bulk_log_cache[season] = df
    return df


def warm_player_cache(player_ids: list, season: str = None):
    """
    Pre-populate the per-player cache for a list of player IDs using bulk data.

    Call this once at the start of a backtest to avoid N individual API calls.
    Fetches current + previous season in 2 requests total regardless of player count.
    """
    if season is None:
        season = current_season()

    bulk    = load_season_game_logs(season)
    prev_df = None

    for pid in player_ids:
        if pid is None or (pid, season) in _player_cache:
            continue

        pdf = bulk[bulk["PLAYER_ID"] == pid].sort_values(
            "GAME_DATE", ascending=False
        ).reset_index(drop=True)

        if len(pdf) < 5:
            if prev_df is None:
                prev_df = load_season_game_logs(_prev_season(season))
            prev = prev_df[prev_df["PLAYER_ID"] == pid].sort_values(
                "GAME_DATE", ascending=False
            ).reset_index(drop=True)
            pdf = pd.concat([pdf, prev], ignore_index=True)

        _player_cache[(pid, season)] = pdf

    print(f"  [cache] Warmed {len(player_ids)} players from bulk data.")


def get_player_game_log(player_id: int, season: str = None,
                        last_n: int = 30) -> pd.DataFrame:
    """Return the last N regular-season games for a player as a DataFrame."""
    if season is None:
        season = current_season()
    cache_key = (player_id, season)
    if cache_key in _player_cache:
        return _player_cache[cache_key].head(last_n)

    log = _nba_request(
        playergamelog.PlayerGameLog,
        player_id=player_id,
        season=season,
        season_type_all_star="Regular Season",
    )
    df = log.get_data_frames()[0]

    if len(df) < 5:
        prev_season = _prev_season(season)
        log2 = _nba_request(
            playergamelog.PlayerGameLog,
            player_id=player_id,
            season=prev_season,
            season_type_all_star="Regular Season",
        )
        df2 = log2.get_data_frames()[0]
        df = pd.concat([df, df2], ignore_index=True)

    df["GAME_DATE"] = pd.to_datetime(df["GAME_DATE"])
    df = df.sort_values("GAME_DATE", ascending=False).reset_index(drop=True)
    df["IS_HOME"] = ~df["MATCHUP"].str.contains("@")

    _player_cache[cache_key] = df
    return df.head(last_n)


def get_playoff_game_log(player_id: int, season: str = None) -> pd.DataFrame:
    """Return playoff game log for a player."""
    if season is None:
        season = current_season()
    log = _nba_request(
        playergamelog.PlayerGameLog,
        player_id=player_id,
        season=season,
        season_type_all_star="Playoffs",
    )
    df = log.get_data_frames()[0]
    df["GAME_DATE"] = pd.to_datetime(df["GAME_DATE"])
    df = df.sort_values("GAME_DATE", ascending=False).reset_index(drop=True)
    return df


def get_team_defensive_ratings(season: str = None) -> pd.DataFrame:
    """Return per-team defensive rating and opponent stats (cached)."""
    if season is None:
        season = current_season()
    if season in _team_def_cache:
        return _team_def_cache[season]

    stats = _nba_request(
        leaguedashteamstats.LeagueDashTeamStats,
        season=season,
        measure_type_detailed_defense="Defense",
        per_mode_simple="PerGame",
    )
    df = stats.get_data_frames()[0]
    _team_def_cache[season] = df
    return df


def get_opponent_def_rating(opp_team_name: str, stat_type: str,
                            season: str = "2024-25") -> float:
    """
    Return the opponent's defensive rank multiplier for a given stat type.

    Returns a float where > 1.0 means the opponent allows more than league average
    (easier matchup) and < 1.0 means tougher matchup.
    """
    df = get_team_defensive_ratings(season)

    opp_lower = opp_team_name.lower()
    row = df[df["TEAM_NAME"].str.lower().str.contains(opp_lower, na=False)]
    if row.empty:
        return 1.0  # neutral if not found

    row = row.iloc[0]

    stat_col_map = {
        "pts":  "OPP_PTS",
        "reb":  "OPP_REB",
        "ast":  "OPP_AST",
        "3pm":  "OPP_FG3M",
        "blk":  "OPP_BLK",
        "stl":  "OPP_STL",
        "pra":  "OPP_PTS",
        "pr":   "OPP_PTS",
        "pa":   "OPP_PTS",
        "ra":   "OPP_REB",
    }
    col = stat_col_map.get(stat_type)
    if not col or col not in df.columns:
        return 1.0

    league_avg = df[col].mean()
    opp_val = row[col]
    if league_avg == 0:
        return 1.0
    return float(opp_val / league_avg)


def get_player_home_away_split(df: pd.DataFrame, stat_cols: list[str]) -> dict:
    """Return average stat values split by home/away."""
    home = df[df["IS_HOME"] == True][stat_cols].mean()
    away = df[df["IS_HOME"] == False][stat_cols].mean()
    return {"home": home.to_dict(), "away": away.to_dict()}


def get_days_rest(df: pd.DataFrame, upcoming_date: Optional[pd.Timestamp] = None) -> int:
    """Estimate days of rest before the next game from the most recent logged game."""
    if df.empty:
        return 2
    most_recent = df["GAME_DATE"].iloc[0]
    ref = upcoming_date or pd.Timestamp.today()
    return max(0, (ref - most_recent).days)


def _prev_season(season: str) -> str:
    parts = season.split("-")
    year = int(parts[0]) - 1
    return f"{year}-{str(year + 1)[2:]}"


def compute_composite_stat(df: pd.DataFrame, stat_type: str) -> pd.Series:
    """Compute composite stats like PRA, PR, PA, RA."""
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
