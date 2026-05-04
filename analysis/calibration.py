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
import re
from collections import defaultdict
from datetime import date, timedelta
from pathlib import Path
from typing import Optional

# Match the date embedded in Kalshi event tickers, e.g. "26APR21" in
# "KXNBAREB-26APR21PORSAS-..." → year=2026, month=April, day=21
_TICKER_DATE_RE = re.compile(
    r"-(\d{2})(JAN|FEB|MAR|APR|MAY|JUN|JUL|AUG|SEP|OCT|NOV|DEC)(\d{2})[A-Z]{3}"
)
_TICKER_MONTHS = {
    "JAN": 1, "FEB": 2, "MAR": 3, "APR": 4, "MAY": 5, "JUN": 6,
    "JUL": 7, "AUG": 8, "SEP": 9, "OCT": 10, "NOV": 11, "DEC": 12,
}


def _date_from_ticker(ticker: str) -> Optional[date]:
    """Extract the actual game date encoded in a Kalshi event ticker."""
    m = _TICKER_DATE_RE.search(ticker)
    if not m:
        return None
    try:
        yr  = int("20" + m.group(1))
        mon = _TICKER_MONTHS[m.group(2)]
        day = int(m.group(3))
        return date(yr, mon, day)
    except (ValueError, KeyError):
        return None

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
            "ticker":              ticker,
            "game_date":           date_str,
            "player_name":         a.get("player_name", ""),
            "stat_type":           a.get("stat_type", ""),
            "line":                a.get("line"),
            "yes_ask":             a.get("yes_ask", 50),
            "our_probability":     a.get("our_probability"),
            "implied_probability": a.get("implied_probability"),
            "edge":                a.get("edge"),
            "confidence":          a.get("confidence", ""),
            "base_rate":           a.get("base_rate"),
            "factors":             a.get("factors", {}),
            "suggested_contracts": a.get("suggested_contracts", 0),
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

# ---------------------------------------------------------------------------
# Resolution — fetch actual outcomes from ESPN boxscores
# ---------------------------------------------------------------------------

def _build_actuals_for_dates(espn_mod, dates: list[date]) -> dict[str, dict]:
    """
    Fetch ESPN boxscores for each date and build a name → stats map.
    Deduplicates: same player appearing in multiple games keeps the last entry.
    """
    actuals: dict[str, dict] = {}
    for d in dates:
        try:
            completed = espn_mod.get_completed_games(d)
        except Exception as e:
            print(f"[calibration] ESPN fetch failed for {d}: {e}")
            continue
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
    return actuals


def resolve_picks(for_date: date = None):
    """
    Mark win/loss on all unresolved picks whose game_date is in the past.

    Before the UTC→ET date fix was deployed, some picks were stored with
    game_date one day ahead of the actual game (e.g. a 10 PM ET game on
    Apr 21 got stored as Apr 22 because its close_time crossed midnight UTC).
    To handle those, we also check the ticker's embedded date and look up
    boxscores for both the stored date and the day before.

    Picks currently marked "no_data" are re-examined if their ticker reveals
    a different game date — they may simply have been looked up on the wrong
    day before.

    Pass for_date to limit resolution to a specific date (used in tests).
    """
    from data import espn as espn_mod

    picks     = _load_picks()
    today_str = date.today().isoformat()
    today_obj = date.today()

    # Re-open no_data picks where the ticker says a different game date,
    # so they get another shot at resolution on the correct date.
    reopened = 0
    for pick in picks:
        if pick.get("outcome") != "no_data":
            continue
        ticker      = pick.get("ticker", "")
        ticker_date = _date_from_ticker(ticker)
        stored_date = pick.get("game_date", "")
        if ticker_date and ticker_date.isoformat() != stored_date:
            # Stored date differs from ticker → was probably a UTC-overflow
            # mislabelling.  Reopen so we re-resolve on the correct date.
            pick["outcome"]       = None
            pick["game_date"]     = ticker_date.isoformat()
            pick["actual_stat"]   = None
            pick["resolved_date"] = None
            reopened += 1

    if reopened:
        print(f"[calibration] Reopened {reopened} no_data picks with corrected game dates")

    # Collect all dates that have unresolved picks in the past
    if for_date:
        pending_dates = [for_date.isoformat()]
    else:
        pending_dates = sorted({
            p["game_date"] for p in picks
            if p["outcome"] is None and p.get("game_date", "") < today_str
        })

    if not pending_dates:
        print("[calibration] No past unresolved picks to resolve")
        _save_picks(picks)
        return

    total_resolved = 0
    total_no_data  = 0

    for date_str in pending_dates:
        unresolved_for_date = [
            p for p in picks
            if p["game_date"] == date_str and p["outcome"] is None
        ]
        if not unresolved_for_date:
            continue

        print(f"[calibration] Resolving {len(unresolved_for_date)} picks for {date_str}...")

        game_date_obj = date.fromisoformat(date_str)
        if game_date_obj >= today_obj:
            print(f"[calibration]   {date_str} is today or future — skipping")
            continue

        # Look up boxscores on the stored date AND the day before, because
        # some games stored as "Apr 22" were really "Apr 21" (UTC midnight bug).
        dates_to_check = [game_date_obj, game_date_obj - timedelta(days=1)]
        actuals = _build_actuals_for_dates(espn_mod, dates_to_check)

        if not actuals:
            print(f"[calibration]   No boxscore data found for {date_str} (or day before)")
            # Mark as no_data so we don't keep retrying indefinitely
            for pick in picks:
                if pick["game_date"] == date_str and pick["outcome"] is None:
                    pick["outcome"] = "no_data"
                    total_no_data  += 1
            continue

        resolved = 0
        no_data  = 0
        for pick in picks:
            if pick["game_date"] != date_str or pick["outcome"] is not None:
                continue

            name_key = pick["player_name"].lower().strip()
            stats    = _find_player_stats(name_key, actuals)

            if stats is None:
                pick["outcome"] = "no_data"
                no_data += 1
                continue

            stat   = pick["stat_type"]
            actual = stats.get(stat, 0.0)
            line   = pick["line"] or 0

            pick["actual_stat"]   = actual
            pick["outcome"]       = "win" if actual >= line else "loss"
            pick["resolved_date"] = today_str
            resolved += 1

        total_resolved += resolved
        total_no_data  += no_data
        print(f"[calibration]   {date_str}: {resolved} resolved, {no_data} no-data")

    _save_picks(picks)
    print(f"[calibration] Total: {total_resolved} resolved, {total_no_data} no-data")


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


def format_daily_recap(for_date: date) -> Optional[str]:
    """
    Format a recap of all picks logged for for_date with their outcomes.
    Returns None if no picks were logged for that date.
    """
    picks    = _load_picks()
    date_str = for_date.isoformat()

    day_picks = [p for p in picks if p["game_date"] == date_str]
    if not day_picks:
        return None

    resolved = [p for p in day_picks if p["outcome"] in ("win", "loss")]
    no_data  = [p for p in day_picks if p["outcome"] == "no_data"]
    pending  = [p for p in day_picks if p["outcome"] is None]

    wins = sum(1 for p in resolved if p["outcome"] == "win")

    lines = [f"NBA Results — {date_str}", ""]

    if resolved:
        from analysis.sizing import pick_pnl
        total_pnl = 0.0
        # Sort: wins first, then losses
        for p in sorted(resolved, key=lambda x: x["outcome"]):
            icon       = "WIN" if p["outcome"] == "win" else "LOSS"
            actual     = p.get("actual_stat")
            actual_s   = f"{actual:.1f}" if actual is not None else "?"
            our_pct    = f"{p['our_probability']:.0%}" if p.get("our_probability") is not None else "?"
            impl_pct   = f"{p['implied_probability']:.0%}" if p.get("implied_probability") is not None else "?"
            edge_pct   = f"{p['edge']:+.1%}" if p.get("edge") is not None else "?"
            stat       = p.get("stat_type", "?").upper()
            line_val   = p.get("line", "?")
            player     = p.get("player_name", "?")
            contracts  = p.get("suggested_contracts", 0)
            yes_ask    = p.get("yes_ask", 50)
            pnl        = pick_pnl(contracts, yes_ask, p["outcome"]) if contracts else None
            if pnl is not None:
                total_pnl += pnl
                pnl_s = f"  {pnl:+.2f}"
            else:
                pnl_s = ""
            contracts_s = f" × {contracts}c" if contracts else ""
            lines.append(f"[{icon}] {player} {stat} {line_val}+  actual={actual_s}{contracts_s}{pnl_s}")
            lines.append(f"       edge={edge_pct}  ours={our_pct}  kalshi={impl_pct}")

        lines.append("")
        lines.append(f"Hit rate: {wins}/{len(resolved)} ({wins/len(resolved):.1%})")
        if any(p.get("suggested_contracts", 0) for p in resolved):
            lines.append(f"P&L: ${total_pnl:+.2f}")
    else:
        lines.append("No resolved picks for this date.")

    if no_data:
        lines.append(f"No boxscore data found for {len(no_data)} pick(s).")
    if pending:
        lines.append(f"{len(pending)} pick(s) still pending resolution.")

    # Running total across all resolved picks
    all_resolved = [p for p in picks if p["outcome"] in ("win", "loss")]
    if len(all_resolved) > len(resolved):
        total_wins = sum(1 for p in all_resolved if p["outcome"] == "win")
        lines.append(
            f"\nAll-time: {total_wins}/{len(all_resolved)} "
            f"({total_wins/len(all_resolved):.1%}) across "
            f"{len({p['game_date'] for p in all_resolved})} day(s)"
        )

    return "\n".join(lines)


def format_pnl_report(from_date: date = None, to_date: date = None,
                      confidence_levels: tuple = ("high", "medium"),
                      sent_only: bool = True) -> str:
    """
    Build a P&L summary for all logged picks in a date range.

    from_date / to_date: inclusive, defaults to all dates in the log.
    confidence_levels: filter by confidence (default high+medium).
    sent_only: if True, only include picks where suggested_contracts > 0
               (i.e. actually alerted to the user).
    """
    from analysis.sizing import pick_pnl

    picks = _load_picks()

    # Apply filters
    subset = []
    for p in picks:
        if confidence_levels and p.get("confidence") not in confidence_levels:
            continue
        if sent_only and not p.get("suggested_contracts", 0):
            continue
        gd = p.get("game_date", "")
        if from_date and gd < from_date.isoformat():
            continue
        if to_date and gd > to_date.isoformat():
            continue
        subset.append(p)

    if not subset:
        return "No picks match the specified filters."

    resolved = [p for p in subset if p["outcome"] in ("win", "loss")]
    no_data  = [p for p in subset if p["outcome"] == "no_data"]
    pending  = [p for p in subset if p["outcome"] is None]

    wins       = sum(1 for p in resolved if p["outcome"] == "win")
    total_pnl  = 0.0
    total_risk = 0.0

    lines = ["=" * 50]
    header = "NBA Picks P&L Report"
    if from_date or to_date:
        fd = from_date.isoformat() if from_date else "?"
        td = to_date.isoformat() if to_date else "?"
        header += f"  ({fd} → {td})"
    lines.append(header)
    lines.append(f"Filter: confidence={'/'.join(confidence_levels)}, sent_only={sent_only}")
    lines.append("=" * 50)
    lines.append("")

    if resolved:
        lines.append("── Resolved picks ──────────────────────────────")
        for p in sorted(resolved, key=lambda x: (x["game_date"], x["outcome"])):
            icon      = "WIN " if p["outcome"] == "win" else "LOSS"
            contracts = p.get("suggested_contracts", 0)
            ask       = p.get("yes_ask", 50)
            pnl       = pick_pnl(contracts, ask, p["outcome"])
            risk      = contracts * ask / 100
            total_pnl  += pnl
            total_risk += risk
            actual_s = (f"{p['actual_stat']:.1f}" if p.get("actual_stat") is not None else "?")
            lines.append(
                f"[{icon}] {p['game_date']}  {p['player_name']}  "
                f"{p.get('stat_type','?').upper()} {p.get('line','?')}+"
                f"  actual={actual_s}  {contracts}x@{ask}c  {pnl:+.2f}"
            )
        lines.append("")

    if no_data:
        lines.append("── No boxscore data (unresolvable) ─────────────")
        for p in sorted(no_data, key=lambda x: x["game_date"]):
            contracts = p.get("suggested_contracts", 0)
            ask       = p.get("yes_ask", 50)
            risk      = contracts * ask / 100
            total_risk += risk
            lines.append(
                f"[????] {p['game_date']}  {p['player_name']}  "
                f"{p.get('stat_type','?').upper()} {p.get('line','?')}+"
                f"  {contracts}x@{ask}c  risk=${risk:.2f}"
            )
        lines.append("")

    if pending:
        lines.append("── Pending resolution ──────────────────────────")
        for p in sorted(pending, key=lambda x: x["game_date"]):
            contracts = p.get("suggested_contracts", 0)
            ask       = p.get("yes_ask", 50)
            risk      = contracts * ask / 100
            total_risk += risk
            lines.append(
                f"[PEND] {p['game_date']}  {p['player_name']}  "
                f"{p.get('stat_type','?').upper()} {p.get('line','?')}+"
                f"  {contracts}x@{ask}c  risk=${risk:.2f}"
            )
        lines.append("")

    # Summary
    lines.append("─" * 50)
    lines.append(f"Total picks sent:      {len(subset)}")
    if resolved:
        lines.append(
            f"  Resolved:            {len(resolved)}  "
            f"({wins} W / {len(resolved)-wins} L  "
            f"{wins/len(resolved):.1%} hit rate)"
        )
    else:
        lines.append(f"  Resolved:            0")
    lines.append(f"  No data:             {len(no_data)}")
    lines.append(f"  Pending:             {len(pending)}")
    lines.append(f"Total capital at risk: ${total_risk:.2f}")
    if resolved:
        lines.append(f"Resolved P&L:          ${total_pnl:+.2f}")
    unresolved_risk = sum(
        p.get("suggested_contracts", 0) * p.get("yes_ask", 50) / 100
        for p in no_data + pending
    )
    if unresolved_risk:
        lines.append(f"Unresolved risk:       ${unresolved_risk:.2f}  (outcome unknown)")

    return "\n".join(lines)
