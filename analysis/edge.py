"""
Edge calculation engine.

For each Kalshi NBA player-prop market we:
  1. Fetch the player's recent game log (nba_api)
  2. Compute a weighted empirical probability of exceeding the line
  3. Apply adjustment factors: matchup quality, home/away, rest, injury
  4. Compare to Kalshi's implied probability to compute edge
  5. Assign a confidence level based on sample size and variance
"""
import math
import numpy as np
import pandas as pd
from typing import Optional

import config
from data import nba_stats, espn, news as news_mod


def analyze_market(market: dict) -> Optional[dict]:
    """
    Full edge analysis for a parsed Kalshi market dict.

    Returns an analysis dict ready to be saved to the DB, or None if we
    cannot compute a meaningful edge (e.g., player not found).
    """
    player_name = market.get("player_name", "")
    stat_type   = market.get("stat_type", "pts").lower()
    line        = market.get("line")
    yes_ask     = market.get("yes_ask", 50)
    game_date   = market.get("game_date", "")
    home_team   = market.get("home_team", "")
    away_team   = market.get("away_team", "")

    if not player_name or line is None:
        return None

    player_id = nba_stats.find_player_id(player_name)
    if not player_id:
        return None

    # -----------------------------------------------------------------------
    # Pull game log
    # -----------------------------------------------------------------------
    try:
        df = nba_stats.get_player_game_log(player_id, last_n=40)
    except Exception:
        return None

    if len(df) < 5:
        return None

    # -----------------------------------------------------------------------
    # Build stat series
    # -----------------------------------------------------------------------
    stat_series = _build_stat_series(df, stat_type)
    if stat_series is None or len(stat_series) == 0:
        return None

    # -----------------------------------------------------------------------
    # Weighted base probability (exponential decay — recent games count more)
    # -----------------------------------------------------------------------
    n = len(stat_series)
    weights = np.array([config.DECAY_FACTOR ** i for i in range(n)])
    weights /= weights.sum()

    over_mask = (stat_series >= line).astype(float).values
    base_prob = float(np.dot(weights, over_mask))
    sample_size = n

    # -----------------------------------------------------------------------
    # Adjustment factors
    # -----------------------------------------------------------------------
    factors: dict[str, float] = {}

    # 1. Matchup / opponent defensive quality
    opp_team = _resolve_opponent(player_name, home_team, away_team, df)
    if opp_team:
        match_factor = nba_stats.get_opponent_def_rating(opp_team, stat_type)
        factors["matchup"] = round(match_factor, 3)
    else:
        factors["matchup"] = 1.0

    # 2. Home / away split
    is_home = _player_is_home(player_name, home_team, df)
    if is_home is not None and "IS_HOME" in df.columns:
        split = nba_stats.get_player_home_away_split(df, _stat_cols(stat_type, df))
        home_avg = _dict_sum(split["home"])
        away_avg = _dict_sum(split["away"])
        if away_avg and home_avg:
            ratio = (home_avg / away_avg) if is_home else (away_avg / home_avg)
            # Dampen the split: don't let it swing more than ±15%
            ratio = 1.0 + (ratio - 1.0) * 0.5
            factors["home_away"] = round(max(0.85, min(1.15, ratio)), 3)
        else:
            factors["home_away"] = 1.0
    else:
        factors["home_away"] = 1.0

    # 3. Rest / fatigue
    rest_days = nba_stats.get_days_rest(df)
    if rest_days == 0:
        factors["rest"] = 0.93   # back-to-back
    elif rest_days == 1:
        factors["rest"] = 0.97
    elif rest_days >= 4:
        factors["rest"] = 1.03   # well rested
    else:
        factors["rest"] = 1.0

    # 4. Injury / news — player themselves
    inj_info = {}
    inj_status = ""
    try:
        inj_info = news_mod.get_player_injury_status(player_name)
        inj_status = inj_info.get("status", "")
        factors["injury"] = _injury_factor(inj_status)
    except Exception:
        factors["injury"] = 1.0

    # 4b. Teammate-return penalty — if a star is coming back to this player's
    #     team their usage/role will shrink (e.g. KD returning → Eason/Thompson ↓)
    teammate_note = ""
    try:
        player_team = nba_stats.get_player_team(player_name)
        if player_team:
            team_injuries = news_mod.get_injury_report(player_team)
            returning = [
                i for i in team_injuries
                if i.get("player", "").lower() != player_name.lower()
                and i.get("status", "").lower() in ("probable", "questionable", "active",
                                                     "day-to-day")
            ]
            if returning:
                # Star = appears near top of injury report (index 0-1) or is well-known
                stars_returning = returning[:2]  # top entries are usually stars
                if stars_returning:
                    teammate_note = ", ".join(
                        f"{r['player']} ({r['status']})" for r in stars_returning
                    )
                    factors["teammate_return"] = 0.90  # 10% usage reduction
    except Exception:
        pass

    # 5. Variance / consistency adjustment
    std = float(stat_series.std()) if len(stat_series) > 1 else 0.0
    mean = float(stat_series.mean())
    if mean > 0:
        cv = std / mean  # coefficient of variation
        # High variance (cv > 0.4) → shrink toward 0.5
        consistency_adj = 1.0 - max(0.0, (cv - 0.35) * 0.2)
        factors["consistency"] = round(consistency_adj, 3)
    else:
        factors["consistency"] = 1.0

    # -----------------------------------------------------------------------
    # Combine adjustments
    # -----------------------------------------------------------------------
    combined_factor = math.prod(factors.values())
    # Apply factor to shift probability toward/away from 50%
    adjusted_prob = _apply_factor(base_prob, combined_factor)
    adjusted_prob = round(max(0.03, min(0.97, adjusted_prob)), 4)

    # -----------------------------------------------------------------------
    # Implied probability from Kalshi ask price
    # -----------------------------------------------------------------------
    implied_prob = yes_ask / 100.0
    edge = round(adjusted_prob - implied_prob, 4)

    # -----------------------------------------------------------------------
    # Confidence level
    # -----------------------------------------------------------------------
    confidence = _confidence(sample_size, std, mean)

    # -----------------------------------------------------------------------
    # News summary
    # -----------------------------------------------------------------------
    try:
        news_summary = news_mod.summarize_news_for_pick(player_name, opp_team or "")
    except Exception:
        news_summary = ""

    return {
        "market_ticker":    market["ticker"],
        "our_probability":  adjusted_prob,
        "implied_probability": implied_prob,
        "edge":             edge,
        "confidence":       confidence,
        "sample_size":      sample_size,
        "base_rate":        round(base_prob, 4),
        "factors":          factors,
        "news_summary":     news_summary,
        "inj_status":       inj_status,
        "teammate_note":    teammate_note,
        "game_date":        market.get("game_date", ""),
        "away_team":        market.get("away_team", ""),
        "home_team":        market.get("home_team", ""),
    }


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

def _build_stat_series(df: pd.DataFrame, stat_type: str) -> Optional[pd.Series]:
    composites = {"pra", "pr", "pa", "ra"}
    if stat_type in composites:
        try:
            return nba_stats.compute_composite_stat(df, stat_type)
        except Exception:
            return None

    col_map = {
        "pts": "PTS", "reb": "REB", "ast": "AST",
        "stl": "STL", "blk": "BLK", "tov": "TOV", "3pm": "FG3M",
    }
    col = col_map.get(stat_type)
    if not col or col not in df.columns:
        return None
    return df[col].astype(float)


def _stat_cols(stat_type: str, df: pd.DataFrame) -> list[str]:
    col_map = {
        "pts": ["PTS"], "reb": ["REB"], "ast": ["AST"],
        "stl": ["STL"], "blk": ["BLK"], "tov": ["TOV"], "3pm": ["FG3M"],
        "pra": ["PTS", "REB", "AST"], "pr": ["PTS", "REB"],
        "pa": ["PTS", "AST"], "ra": ["REB", "AST"],
    }
    return [c for c in col_map.get(stat_type, []) if c in df.columns]


def _dict_sum(d: dict) -> float:
    return sum(v for v in d.values() if isinstance(v, (int, float)) and not math.isnan(v))


def _apply_factor(prob: float, factor: float) -> float:
    """Shift probability multiplicatively toward or away from 0.5."""
    if factor >= 1.0:
        return prob + (1.0 - prob) * (factor - 1.0) * 0.5
    else:
        return prob * factor


def _injury_factor(status: str) -> float:
    s = status.lower()
    if "out" in s:
        return 0.0
    if "doubtful" in s:
        return 0.5
    if "questionable" in s:
        return 0.85
    if "day-to-day" in s or "day to day" in s:
        return 0.90
    if "probable" in s:
        return 0.97
    if "rest" in s:
        return 0.80
    return 1.0


def _confidence(sample_size: int, std: float, mean: float) -> str:
    cv = (std / mean) if mean > 0 else 1.0
    if sample_size >= 25 and cv < 0.35:
        return "high"
    if sample_size >= 12 and cv < 0.55:
        return "medium"
    return "low"


def _resolve_opponent(player_name: str, home_team: str,
                      away_team: str, df: pd.DataFrame) -> Optional[str]:
    """Determine the opponent team for this game from context."""
    if home_team and away_team:
        return home_team  # placeholder; enriched by caller when team is known
    if not df.empty and "MATCHUP" in df.columns:
        last_matchup = df["MATCHUP"].iloc[0]
        if "vs." in last_matchup:
            return last_matchup.split("vs.")[-1].strip()
        if "@" in last_matchup:
            return last_matchup.split("@")[-1].strip()
    return None


def _player_is_home(player_name: str, home_team: str,
                    df: pd.DataFrame = None) -> Optional[bool]:
    """Return True if the player's team is the home team.

    Derives the player's team abbreviation from the most recent MATCHUP row
    (e.g. 'SAS vs. POR' → team=SAS) rather than making an extra API call.
    """
    if not home_team or df is None or df.empty or "MATCHUP" not in df.columns:
        return None
    try:
        player_team = df["MATCHUP"].iloc[0].split()[0].upper()
        return player_team == home_team.upper()
    except Exception:
        return None


def rank_picks(analyses: list[dict], min_edge: float = None) -> list[dict]:
    """Sort and filter analyses by edge, returning best opportunities first."""
    threshold = min_edge if min_edge is not None else config.MIN_EDGE_THRESHOLD
    filtered = [a for a in analyses if a and a.get("edge", 0) >= threshold]
    return sorted(filtered, key=lambda a: a["edge"], reverse=True)
