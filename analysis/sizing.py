"""
Position sizing via quarter-Kelly criterion.

For each pick we compute the Kelly-optimal fraction of bankroll to risk,
then scale it down (quarter-Kelly) and by confidence level to keep
individual bets conservative.

Kelly fraction:
    f* = (p*b - q) / b
    where b = net-odds per dollar risked = (1 - cost) / cost
          p = our probability of YES
          q = 1 - p

Then: fraction = f* × 0.25 × confidence_scale
      max_risk  = bankroll × fraction  (hard cap: 15% per bet)
      contracts = floor(max_risk / cost_per_contract)
"""

_CONF_SCALE = {"high": 1.0, "medium": 0.75, "low": 0.5}
_KELLY_FRAC = 0.25   # quarter-Kelly
_MAX_FRAC   = 0.15   # never risk more than 15% of bankroll on one bet


def suggest_contracts(our_probability: float,
                      yes_ask_cents: int,
                      confidence: str,
                      bankroll_dollars: float) -> int:
    """Return suggested number of YES contracts to buy."""
    cost = yes_ask_cents / 100.0
    if cost <= 0 or cost >= 1 or bankroll_dollars <= 0:
        return 0

    b = (1.0 - cost) / cost      # net odds
    p = our_probability
    q = 1.0 - p
    kelly = (p * b - q) / b
    if kelly <= 0:
        return 0

    conf_scale = _CONF_SCALE.get((confidence or "").lower(), 0.5)
    fraction   = min(kelly * _KELLY_FRAC * conf_scale, _MAX_FRAC)

    max_risk  = bankroll_dollars * fraction
    contracts = int(max_risk / cost)
    return max(1, contracts)


def pick_pnl(contracts: int, yes_ask_cents: int, outcome: str) -> float:
    """Return P&L in dollars for a resolved pick."""
    cost = yes_ask_cents / 100.0
    if outcome == "win":
        return contracts * (1.0 - cost)   # paid cost, received $1.00
    elif outcome == "loss":
        return -contracts * cost
    return 0.0
