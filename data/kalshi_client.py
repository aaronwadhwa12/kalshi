"""Kalshi REST API v2 client — market discovery, pricing, and order placement."""
import re
import time
import requests
from datetime import date, datetime
from typing import Optional

import config

_BASE = config.KALSHI_BASE_URL

_session = requests.Session()
_session.headers.update({
    "Content-Type": "application/json",
    "Accept": "application/json",
})

_token: Optional[str] = None


# ---------------------------------------------------------------------------
# Authentication
# ---------------------------------------------------------------------------

def _auth_header() -> dict:
    if not config.KALSHI_API_KEY:
        raise RuntimeError("KALSHI_API_KEY not set in .env")
    return {"Authorization": f"Bearer {config.KALSHI_API_KEY}"}


def _get(path: str, params: dict = None) -> dict:
    resp = _session.get(f"{_BASE}{path}", params=params,
                        headers=_auth_header(), timeout=15)
    resp.raise_for_status()
    return resp.json()


def _post(path: str, payload: dict) -> dict:
    resp = _session.post(f"{_BASE}{path}", json=payload,
                         headers=_auth_header(), timeout=15)
    resp.raise_for_status()
    return resp.json()


# ---------------------------------------------------------------------------
# Market discovery
# ---------------------------------------------------------------------------

_NBA_SERIES_KEYWORDS = ["NBA", "NBAPL", "NBAP"]


def get_nba_markets(game_date: Optional[date] = None,
                    limit: int = 200) -> list[dict]:
    """
    Fetch active NBA player-prop markets from Kalshi.

    Kalshi uses series tickers like NBAP (NBA Player props) and event tickers
    that embed the game date. We filter by status=open and a date range.
    """
    params = {
        "limit":       limit,
        "status":      "open",
        "series_ticker": "NBAP",
    }
    if game_date:
        params["min_close_ts"] = int(datetime.combine(game_date, datetime.min.time()).timestamp())
    try:
        data = _get("/markets", params=params)
        markets = data.get("markets", [])
    except Exception:
        # Fallback: search with broader filter
        params.pop("series_ticker", None)
        try:
            data = _get("/markets", params=params)
            markets = [m for m in data.get("markets", [])
                       if any(k in m.get("ticker", "") for k in _NBA_SERIES_KEYWORDS)]
        except Exception:
            markets = []
    return markets


def parse_market(raw: dict) -> Optional[dict]:
    """
    Parse a raw Kalshi market dict into our internal schema.

    Kalshi NBA player-prop tickers look like:
      NBAP-LEBJ-PTS-25-20241115  (player, stat, line, date)
    Title:  "Will LeBron James score 25+ points vs LAC on Nov 15?"
    """
    ticker = raw.get("ticker", "")
    title = raw.get("title", "")
    subtitle = raw.get("subtitle", "")
    full_text = f"{title} {subtitle}".lower()

    player_name, stat_type, line = _parse_title(full_text, ticker)
    if not player_name or line is None:
        return None

    game_date = _extract_date(raw)

    return {
        "ticker":        ticker,
        "event_ticker":  raw.get("event_ticker", ""),
        "series_ticker": raw.get("series_ticker", ""),
        "title":         title,
        "player_name":   player_name,
        "stat_type":     stat_type,
        "line":          line,
        "yes_ask":       raw.get("yes_ask", 50),
        "yes_bid":       raw.get("yes_bid", 50),
        "no_ask":        raw.get("no_ask", 50),
        "no_bid":        raw.get("no_bid", 50),
        "volume":        raw.get("volume", 0),
        "open_interest": raw.get("open_interest", 0),
        "close_time":    raw.get("close_time", ""),
        "game_date":     game_date,
        "home_team":     "",
        "away_team":     "",
    }


_STAT_PATTERNS = [
    (r"(\d+\.?\d*)\+?\s*point", "pts"),
    (r"(\d+\.?\d*)\+?\s*rebound", "reb"),
    (r"(\d+\.?\d*)\+?\s*assist", "ast"),
    (r"(\d+\.?\d*)\+?\s*steal", "stl"),
    (r"(\d+\.?\d*)\+?\s*block", "blk"),
    (r"(\d+\.?\d*)\+?\s*turnover", "tov"),
    (r"(\d+\.?\d*)\+?\s*three", "3pm"),
    (r"(\d+\.?\d*)\+?\s*3-pointer", "3pm"),
    (r"(\d+\.?\d*)\+?\s*(?:pts\+reb\+ast|pra)", "pra"),
    (r"(\d+\.?\d*)\+?\s*(?:pts\+reb|pr)\b", "pr"),
    (r"(\d+\.?\d*)\+?\s*(?:pts\+ast|pa)\b", "pa"),
    (r"(\d+\.?\d*)\+?\s*(?:reb\+ast|ra)\b", "ra"),
]

# Common NBA player name patterns to extract from titles
_WILL_PLAYER_RE = re.compile(
    r"will\s+([a-z\s\-'\.]+?)\s+(?:score|have|record|make|grab|dish|tally|log|post)",
    re.I,
)


def _parse_title(text: str, ticker: str) -> tuple[Optional[str], str, Optional[float]]:
    """Extract (player_name, stat_type, line) from market title text."""
    stat_type = "pts"
    line = None

    for pattern, stype in _STAT_PATTERNS:
        m = re.search(pattern, text, re.I)
        if m:
            try:
                line = float(m.group(1))
                stat_type = stype
                break
            except ValueError:
                continue

    m = _WILL_PLAYER_RE.search(text)
    player_name = None
    if m:
        player_name = m.group(1).strip().title()

    if not player_name:
        # Try extracting from ticker: NBAP-LEBJ-PTS-25-20241115
        parts = ticker.split("-")
        if len(parts) >= 2:
            # Parts[1] is typically a player code — not reliable, skip
            pass

    return player_name, stat_type, line


def _extract_date(raw: dict) -> str:
    close_time = raw.get("close_time", "")
    if close_time:
        try:
            dt = datetime.fromisoformat(close_time.replace("Z", "+00:00"))
            return dt.strftime("%Y-%m-%d")
        except ValueError:
            pass
    return date.today().isoformat()


# ---------------------------------------------------------------------------
# Portfolio & Ordering
# ---------------------------------------------------------------------------

def get_balance() -> float:
    """Return current Kalshi account balance in dollars."""
    data = _get("/portfolio/balance")
    return data.get("balance", 0) / 100.0  # Kalshi returns cents


def get_positions() -> list[dict]:
    """Return all open market positions."""
    data = _get("/portfolio/positions")
    return data.get("market_positions", [])


def place_order(ticker: str, side: str, contracts: int,
                price_cents: int, order_type: str = "limit") -> dict:
    """
    Place a Kalshi order.

    Args:
        ticker:      Market ticker (e.g. "NBAP-LEBJ-PTS-25-20241115")
        side:        "yes" or "no"
        contracts:   Number of contracts (each costs price_cents cents)
        price_cents: Price per contract in cents (0-100)
        order_type:  "limit" or "market"
    """
    payload = {
        "ticker":       ticker,
        "side":         side,
        "count":        contracts,
        "type":         order_type,
        "buy_max_cost": contracts * price_cents,
    }
    if order_type == "limit":
        payload["yes_price" if side == "yes" else "no_price"] = price_cents
    return _post("/portfolio/orders", payload)


def get_market(ticker: str) -> dict:
    """Fetch a single market by ticker."""
    return _get(f"/markets/{ticker}")


def get_market_history(ticker: str) -> list[dict]:
    """Fetch price history for a market."""
    data = _get(f"/markets/{ticker}/history")
    return data.get("history", [])


def cancel_order(order_id: str) -> dict:
    resp = _session.delete(f"{_BASE}/portfolio/orders/{order_id}",
                           headers=_auth_header(), timeout=15)
    resp.raise_for_status()
    return resp.json()
