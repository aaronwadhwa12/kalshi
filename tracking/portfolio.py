"""Portfolio tracking — record bets, resolve outcomes, compute P&L."""
import json
from datetime import date, datetime
from typing import Optional

from db import database as db
from data import kalshi_client


def add_bet(pick_id: int, contracts: int, is_live: bool = False) -> dict:
    """
    Record a bet for a selected pick.

    If is_live=True, fetches current ask price from Kalshi and places the order.
    Otherwise records a paper bet at the current ask price.
    """
    picks = db.get_top_picks(limit=500)
    pick = next((p for p in picks if p["pick_id"] == pick_id), None)
    if not pick:
        raise ValueError(f"Pick #{pick_id} not found or not in pending state.")

    ticker    = pick["ticker"]
    side      = pick.get("side", "yes")
    entry_price = pick["yes_ask"]
    kalshi_order_id = None

    if is_live:
        try:
            market_data = kalshi_client.get_market(ticker)
            market = market_data.get("market", market_data)
            entry_price = market.get("yes_ask" if side == "yes" else "no_ask", entry_price)
            result = kalshi_client.place_order(
                ticker=ticker,
                side=side,
                contracts=contracts,
                price_cents=entry_price,
            )
            kalshi_order_id = result.get("order", {}).get("order_id")
        except Exception as e:
            raise RuntimeError(f"Kalshi order failed: {e}")

    db.select_pick(pick_id)
    bet_id = db.record_bet(
        pick_id=pick_id,
        contracts=contracts,
        entry_price=entry_price,
        is_live=is_live,
        kalshi_order_id=kalshi_order_id,
    )
    cost = (contracts * entry_price) / 100.0
    return {
        "bet_id":       bet_id,
        "pick_id":      pick_id,
        "ticker":       ticker,
        "player":       pick["player_name"],
        "stat":         pick["stat_type"],
        "line":         pick["line"],
        "side":         side,
        "contracts":    contracts,
        "entry_price":  entry_price,
        "cost":         cost,
        "is_live":      is_live,
        "order_id":     kalshi_order_id,
    }


def resolve_bet(bet_id: int, actual_value: float) -> dict:
    """
    Resolve a bet given the actual observed stat value.

    Calculates P&L based on Kalshi payout (winning side pays $1 per contract).
    """
    open_bets = db.get_open_bets()
    bet = next((b for b in open_bets if b["id"] == bet_id), None)
    if not bet:
        raise ValueError(f"Bet #{bet_id} not found or already resolved.")

    line     = bet["line"]
    side     = bet["side"]
    contracts = bet["contracts"]
    entry_price = bet["entry_price"]  # cents paid per contract

    over_threshold = actual_value >= line
    won_yes = over_threshold
    outcome = "won" if ((side == "yes" and won_yes) or (side == "no" and not won_yes)) else "lost"

    # Kalshi: each contract pays $1 if you win; you paid entry_price cents
    # P&L = contracts * (payout - cost_per_contract)
    if outcome == "won":
        payout_per = 1.00           # $1.00 per winning contract
        cost_per   = entry_price / 100.0
        profit_loss = contracts * (payout_per - cost_per)
    else:
        profit_loss = -contracts * (entry_price / 100.0)

    profit_loss = round(profit_loss, 2)
    db.resolve_bet(bet_id, outcome, profit_loss)

    return {
        "bet_id":       bet_id,
        "player":       bet["player_name"],
        "stat":         bet["stat_type"],
        "line":         line,
        "actual_value": actual_value,
        "side":         side,
        "contracts":    contracts,
        "outcome":      outcome,
        "profit_loss":  profit_loss,
    }


def resolve_bet_by_outcome(bet_id: int, outcome: str, profit_loss: Optional[float] = None) -> dict:
    """Manually mark a bet as won/lost/void (for when Kalshi settles it automatically)."""
    open_bets = db.get_open_bets()
    bet = next((b for b in open_bets if b["id"] == bet_id), None)
    if not bet:
        raise ValueError(f"Bet #{bet_id} not found or already resolved.")

    if profit_loss is None:
        if outcome == "won":
            contracts   = bet["contracts"]
            entry_price = bet["entry_price"]
            profit_loss = round(contracts * (1.00 - entry_price / 100.0), 2)
        elif outcome == "lost":
            profit_loss = round(-bet["contracts"] * bet["entry_price"] / 100.0, 2)
        else:
            profit_loss = 0.0  # void

    db.resolve_bet(bet_id, outcome, profit_loss)
    return {"bet_id": bet_id, "outcome": outcome, "profit_loss": profit_loss}


def get_portfolio_summary() -> dict:
    summary = db.get_performance_summary()
    open_bets = db.get_open_bets()
    summary["open_bets"] = open_bets
    return summary


def get_open_bets() -> list[dict]:
    return db.get_open_bets()


def format_bets_table(bets: list[dict]) -> list[list]:
    """Return bets as rows for tabulate."""
    rows = []
    for b in bets:
        pnl = b.get("profit_loss")
        rows.append([
            b["id"],
            b.get("player_name", ""),
            f"{b.get('stat_type','').upper()} {b.get('line','')}+",
            b.get("side", "yes").upper(),
            b.get("contracts", ""),
            f"{b.get('entry_price', '')}c",
            b.get("outcome", "pending"),
            f"${pnl:+.2f}" if pnl is not None else "-",
        ])
    return rows
