"""
Pick logging, outcome resolution, and model self-calibration.

Flow:
  1. scan_and_alert logs all analyzed markets to results/picks_log.json
  2. Next morning, resolve_picks fetches ESPN boxscores and marks win/loss
  3. update_learned_params computes per-stat bias corrections
  4. edge.py loads those corrections and applies them to future predictions

Bias correction: when we say 40% probability but only 30% actually win,
we have a +10% overestimate bias. We record this per stat type and subtract
it from future predictions for that stat.
"""
import json
from collections import defaultdict
from datetime import date, timedelta
from pathlib import Path
from typing import Optional

_ROOT       = Path(__file__).parent.parent
_PICKS_LOG  = _ROOT / "results" / "picks_log.json"
_PARAMS_FILE = _ROOT / "results" / "learned_params.json"

MIN_SAMPLES = 20   # minimum resolved picks before bias corrections kick in
BIAS_DAMPEN = 0.5  # dampen corrections to avoid overcorrection (0.5 = half weight)


# ---------------------------------------------------------------------------
# Persistence helpers
# ---------------------------------------------------------------------------

def _load_picks() -> list[dict]:
    if not _PICKS_LOG.exists():
        return []
    try:
        with open(_PICKS_LOG) as f:
            return json.load(f)
    except Exception:
        return []


def _save_picks(picks: list[dict]):
    _PICKS_LOG.parent.mkdir(parents=True, exist_ok=True)
    with open(_PICKS_LOG, "w") as f:
        json.dump(picks, f, indent=2, default=str)


def load_learned_params() -> dict:
    if not _PARAMS_FILE.exists():
        return {}
    try:
        with open(_PARAMS_FILE) as f:
            return json.load(f)
    except Exception:
        return {}


def _save_learned_params(params: dict):
    _PARAMS_FILE.parent.mkdir(parents=True, exist_ok=True)
    with open(_PARAMS_FILE, "w") as f:
        json.dump(params, f, indent=2, default=str)


# ---------------------------------------------------------------------------
# Logging
# ---------------------------------------------------------------------------

def log_picks(all_analyses: list[dict], game_date: date):
    """Append all analyzed markets (not just top picks) to the log.

    We log everything — including below-threshold markets — so calibration
    covers the full probability range, not just high-edge picks.
    """
    picks = _load_picks()
    existing = {(p["ticker"], p["game_date"]) for p in picks}
    date_str = game_date.isoformat()

    added = 0
    for a in all_analyses:
        ticker = a.get("market_ticker", "")
        if not ticker or (ticker, date_str) in existing:
            continue
        picks.append({
            "ticker":            ticker,
            "game_date":         date_str,
            "player_name":       a.get("player_name", ""),
            "stat_type":         a.get("stat_type", ""),
            "line":              a.get("line"),
            "yes_ask":           a.get("yes_ask", 50),
            "our_probability":   a.get("our_probability"),
            "implied_probability": a.get("implied_probability"),
            "edge":              a.get("edge"),
            "confidence":        a.get("confidence", ""),
            "base_rate":         a.get("base_rate"),
            "factors":           a.get("factors", {}),
            "outcome":           None,   # filled by resolve_picks
            "actual_stat":       None,
            "resolved_date":     None,
        })
        added += 1

    if added:
        _save_picks(picks)
        print(f"[calibration] Logged {added} picks for {date_str} (total: {len(picks)})")
    else:
        print(f"[calibration] No new picks to log for {date_str}")


# ---------------------------------------------------------------------------
# Resolution — fetch actual outcomes from ESPN boxscores
# ---------------------------------------------------------------------------

def resolve_picks(for_date: date):
    """Mark win/loss on all unresolved picks for for_date using ESPN boxscores."""
    from data import espn as espn_mod

    picks = _load_picks()
    date_str = for_date.isoformat()
    unresolved = [p for p in picks
                  if p["game_date"] == date_str and p["outcome"] is None]

    if not unresolved:
        print(f"[calibration] No unresolved picks for {date_str}")
        return

    print(f"[calibration] Resolving {len(unresolved)} picks for {date_str}...")

    # Fetch all completed games for that date
    try:
        completed = espn_mod.get_completed_games(for_date)
    except Exception as e:
        print(f"[calibration] ESPN fetch failed for {date_str}: {e}")
        return

    if not completed:
        print(f"[calibration] No completed games found for {date_str}")
        return

    # Build name → stats lookup from boxscores
    actuals: dict[str, dict] = {}
    for game in completed:
        try:
            players = espn_mod.get_game_boxscore(game["event_id"])
        except Exception:
            continue
        for p in players:
            key = p["name"].lower().strip()
            pts = float(p.get("pts") or 0)
            reb = float(p.get("reb") or 0)
            ast = float(p.get("ast") or 0)
            actuals[key] = {
                "pts": pts,
                "reb": reb,
                "ast": ast,
                "stl": float(p.get("stl") or 0),
                "blk": float(p.get("blk") or 0),
                "tov": float(p.get("tov") or 0),
                "3pm": float(p.get("fg3m") or 0),
                "pra": pts + reb + ast,
                "pr":  pts + reb,
                "pa":  pts + ast,
                "ra":  reb + ast,
            }

    resolved = 0
    no_data  = 0
    for pick in picks:
        if pick["game_date"] != date_str or pick["outcome"] is not None:
            continue

        name_key = pick["player_name"].lower().strip()
        stats = _find_player_stats(name_key, actuals)

        if stats is None:
            pick["outcome"] = "no_data"
            no_data += 1
            continue

        stat   = pick["stat_type"]
        actual = stats.get(stat, 0.0)
        line   = pick["line"] or 0

        pick["actual_stat"]   = actual
        pick["outcome"]       = "win" if actual >= line else "loss"
        pick["resolved_date"] = date.today().isoformat()
        resolved += 1

    _save_picks(picks)
    print(f"[calibration] Resolved {resolved} picks, {no_data} no-data for {date_str}")


def _find_player_stats(name_key: str, actuals: dict) -> Optional[dict]:
    """Find player stats with fuzzy name matching."""
    if name_key in actuals:
        return actuals[name_key]

    # Last-name + first-initial match
    parts = name_key.split()
    if len(parts) >= 2:
        last       = parts[-1].rstrip(".")
        first_init = parts[0][0]
        for k, v in actuals.items():
            kparts = k.split()
            if len(kparts) >= 2:
                if kparts[-1].rstrip(".") == last and kparts[0][0] == first_init:
                    return v

    # Last-name-only fallback
    if parts:
        last = parts[-1].rstrip(".")
        for k, v in actuals.items():
            kparts = k.split()
            if kparts and kparts[-1].rstrip(".") == last:
                return v

    return None


# ---------------------------------------------------------------------------
# Calibration & parameter learning
# ---------------------------------------------------------------------------

def compute_calibration() -> dict:
    """Compute accuracy stats from all resolved picks."""
    picks    = _load_picks()
    resolved = [p for p in picks if p["outcome"] in ("win", "loss")]
    n        = len(resolved)

    if n < MIN_SAMPLES:
        return {"ready": False, "n_resolved": n, "n_needed": MIN_SAMPLES}

    wins     = sum(1 for p in resolved if p["outcome"] == "win")
    hit_rate = wins / n

    # Brier score: mean squared error of probability forecast (0.25 = random)
    brier = sum(
        (p["our_probability"] - (1 if p["outcome"] == "win" else 0)) ** 2
        for p in resolved if p.get("our_probability") is not None
    ) / n

    # Per-edge-bucket calibration
    buckets: dict[str, list[int]] = {"<5%": [], "5-10%": [], "10-20%": [], "20%+": []}
    for p in resolved:
        edge    = p.get("edge", 0) or 0
        outcome = 1 if p["outcome"] == "win" else 0
        if edge < 0.05:
            buckets["<5%"].append(outcome)
        elif edge < 0.10:
            buckets["5-10%"].append(outcome)
        elif edge < 0.20:
            buckets["10-20%"].append(outcome)
        else:
            buckets["20%+"].append(outcome)

    bucket_stats = {
        k: {"n": len(v), "hit_rate": round(sum(v) / len(v), 3)}
        for k, v in buckets.items() if v
    }

    # Per-stat calibration and bias
    stat_outcomes: dict[str, list] = defaultdict(list)
    stat_probs:    dict[str, list] = defaultdict(list)
    for p in resolved:
        stat    = p.get("stat_type", "?")
        outcome = 1 if p["outcome"] == "win" else 0
        stat_outcomes[stat].append(outcome)
        if p.get("our_probability") is not None:
            stat_probs[stat].append(p["our_probability"])

    stat_summary = {}
    stat_biases  = {}
    for stat, outcomes in stat_outcomes.items():
        hr = sum(outcomes) / len(outcomes)
        stat_summary[stat] = {"n": len(outcomes), "hit_rate": round(hr, 3)}
        if len(outcomes) >= 5 and stat_probs[stat]:
            avg_pred = sum(stat_probs[stat]) / len(stat_probs[stat])
            # bias > 0 means we underestimate (actual > predicted); < 0 overestimate
            stat_biases[stat] = round(hr - avg_pred, 4)

    # Per-confidence calibration
    conf_outcomes: dict[str, list] = defaultdict(list)
    for p in resolved:
        conf_outcomes[p.get("confidence", "?")].append(1 if p["outcome"] == "win" else 0)

    conf_summary = {
        c: {"n": len(v), "hit_rate": round(sum(v) / len(v), 3)}
        for c, v in conf_outcomes.items() if v
    }

    # Global bias
    all_probs    = [p["our_probability"] for p in resolved if p.get("our_probability")]
    global_bias  = round(hit_rate - (sum(all_probs) / len(all_probs)), 4) if all_probs else 0.0

    return {
        "ready":          True,
        "n_resolved":     n,
        "overall_hit_rate": round(hit_rate, 3),
        "brier_score":    round(brier, 4),
        "global_bias":    global_bias,
        "stat_biases":    stat_biases,
        "by_edge_bucket": bucket_stats,
        "by_stat":        stat_summary,
        "by_confidence":  conf_summary,
    }


def update_learned_params() -> Optional[dict]:
    """Recompute bias corrections from calibration data and persist them."""
    cal = compute_calibration()

    if not cal.get("ready"):
        n = cal.get("n_resolved", 0)
        need = cal.get("n_needed", MIN_SAMPLES)
        print(f"[calibration] {n}/{need} resolved picks — not enough data yet")
        return None

    # Dampen corrections so we don't overcorrect from small samples
    global_bias  = round(cal["global_bias"]  * BIAS_DAMPEN, 4)
    stat_biases  = {
        stat: round(bias * BIAS_DAMPEN, 4)
        for stat, bias in cal["stat_biases"].items()
    }

    params = {
        "global_bias":      global_bias,
        "stat_biases":      stat_biases,
        "n_resolved":       cal["n_resolved"],
        "overall_hit_rate": cal["overall_hit_rate"],
        "brier_score":      cal["brier_score"],
        "last_updated":     date.today().isoformat(),
    }
    _save_learned_params(params)
    print(
        f"[calibration] Params saved — global_bias={global_bias:+.3f}, "
        f"n={cal['n_resolved']}, hit_rate={cal['overall_hit_rate']:.1%}, "
        f"brier={cal['brier_score']:.4f}"
    )
    return params


# ---------------------------------------------------------------------------
# Report formatting
# ---------------------------------------------------------------------------

def format_calibration_report() -> str:
    cal = compute_calibration()

    if not cal.get("ready"):
        n    = cal.get("n_resolved", 0)
        need = cal.get("n_needed", MIN_SAMPLES)
        return (f"Model Calibration\n"
                f"Collecting data: {n}/{need} resolved picks so far.\n"
                f"Bias corrections activate at {need} picks.")

    lines = [
        f"Model Calibration ({cal['n_resolved']} resolved picks)",
        f"Hit rate:    {cal['overall_hit_rate']:.1%}",
        f"Brier score: {cal['brier_score']:.4f}  (0.25 = random, lower = better)",
        f"Global bias: {cal['global_bias']:+.3f}  (+ = underestimating, - = overestimating)",
        "",
        "By edge bucket (predicted edge → actual hit rate):",
    ]
    for bucket, s in cal.get("by_edge_bucket", {}).items():
        lines.append(f"  {bucket:8s}: {s['hit_rate']:.1%}  (n={s['n']})")

    lines += ["", "By stat type:"]
    for stat, s in sorted(cal.get("by_stat", {}).items()):
        bias = cal.get("stat_biases", {}).get(stat, 0)
        sign = "+" if bias >= 0 else ""
        lines.append(f"  {stat.upper():5s}: {s['hit_rate']:.1%}  n={s['n']}  bias={sign}{bias:.3f}")

    lines += ["", "By confidence level:"]
    for conf, s in cal.get("by_confidence", {}).items():
        lines.append(f"  {conf.upper():6s}: {s['hit_rate']:.1%}  (n={s['n']})")

    return "\n".join(lines)
