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
    "Accept":       "application/json",
})


# ---------------------------------------------------------------------------
# Authentication
# ---------------------------------------------------------------------------

def _auth_header() -> dict:
    key = config.KALSHI_API_KEY
    if not key:
        raise RuntimeError("KALSHI_API_KEY not set in .env / GitHub Secrets")
    # Kalshi accepts the API key as a Bearer token
    return {"Authorization": f"Bearer {key}"}


def _get(path: str, params: dict = None) -> dict:
    resp = _session.get(f"{_BASE}{path}", params=params,
                        headers=_auth_header(), timeout=20)
    resp.raise_for_status()
    return resp.json()


def _post(path: str, payload: dict) -> dict:
    resp = _session.post(f"{_BASE}{path}", json=payload,
                         headers=_auth_header(), timeout=20)
    resp.raise_for_status()
    return resp.json()


# ---------------------------------------------------------------------------
# Market discovery — multi-strategy with full logging
# ---------------------------------------------------------------------------

# Kalshi rotates series tickers; try all known NBA variants
_NBA_SERIES_CANDIDATES = ["NBAP", "NBAPLAYER", "NBA", "KXNBA", "KXNBAP"]
_NBA_KEYWORDS          = ["nba", " pts", " reb", " ast", "points", "rebounds",
                          "assists", "three", "steals", "blocks"]


def get_nba_markets(game_date: Optional[date] = None,
                    limit: int = 200) -> list[dict]:
    """
    Fetch open NBA player-prop markets using multiple fallback strategies.

    Strategy 1 — known series tickers (fast, most reliable when ticker is right)
    Strategy 2 — event search (finds NBA events then gets their markets)
    Strategy 3 — keyword filter over all open markets (slowest, always works)
    """
    # ── Strategy 1: try known series tickers ────────────────────────────────
    for series in _NBA_SERIES_CANDIDATES:
        try:
            params = {"limit": limit, "status": "open", "series_ticker": series}
            if game_date:
                params["min_close_ts"] = int(
                    datetime.combine(game_date, datetime.min.time()).timestamp()
                )
            data    = _get("/markets", params=params)
            markets = data.get("markets", [])
            if markets:
                print(f"[kalshi] Strategy 1: {len(markets)} markets via series '{series}'")
                return markets
            print(f"[kalshi] Series '{series}' returned 0 markets")
        except Exception as e:
            print(f"[kalshi] Series '{series}' error: {e}")

    # ── Strategy 2: events endpoint ─────────────────────────────────────────
    print("[kalshi] Strategy 2: searching events...")
    try:
        events_data = _get("/events", params={"limit": 100, "status": "open"})
        nba_events  = [
            e for e in events_data.get("events", [])
            if "nba" in (e.get("title", "") + e.get("series_ticker", "")).lower()
        ]
        print(f"[kalshi]   Found {len(nba_events)} NBA events")
        markets = []
        for event in nba_events[:30]:
            try:
                mdata = _get("/markets", params={
                    "event_ticker": event.get("event_ticker", ""),
                    "status": "open",
                    "limit": 50,
                })
                markets.extend(mdata.get("markets", []))
            except Exception:
                pass
        if markets:
            print(f"[kalshi] Strategy 2: {len(markets)} markets from events")
            return markets
    except Exception as e:
        print(f"[kalshi] Events search error: {e}")

    # ── Strategy 3: all open markets, filter by NBA keywords ────────────────
    print("[kalshi] Strategy 3: fetching all open markets and filtering...")
    try:
        params  = {"limit": min(limit * 3, 1000), "status": "open"}
        data    = _get("/markets", params=params)
        all_mkts = data.get("markets", [])
        print(f"[kalshi]   Total open markets: {len(all_mkts)}")
        markets = [
            m for m in all_mkts
            if any(
                k in (m.get("ticker", "") + m.get("title", "") +
                      m.get("series_ticker", "")).lower()
                for k in _NBA_KEYWORDS
            )
        ]
        print(f"[kalshi] Strategy 3: {len(markets)} NBA-related markets")
        return markets
    except Exception as e:
        print(f"[kalshi] Strategy 3 error: {e}")

    return []


def debug_raw_markets(limit: int = 5):
    """Print raw API response for first N markets — use for diagnosing parser issues."""
    try:
        data = _get("/markets", params={"limit": limit, "status": "open"})
        for m in data.get("markets", []):
            print(f"  ticker:         {m.get('ticker')}")
            print(f"  series_ticker:  {m.get('series_ticker')}")
            print(f"  title:          {m.get('title')}")
            print(f"  subtitle:       {m.get('subtitle')}")
            print(f"  yes_ask:        {m.get('yes_ask')}")
            print()
    except Exception as e:
        print(f"[kalshi] debug error: {e}")


# ---------------------------------------------------------------------------
# Market parsing — handles many Kalshi title formats
# ---------------------------------------------------------------------------

_STAT_PATTERNS = [
    # "over 25.5 points" / "25.5+ points" / "25+ pts"
    (r"over\s+(\d+\.?\d*)\s*(?:\+)?\s*(?:point|pts)", "pts"),
    (r"(\d+\.?\d*)\+?\s*point", "pts"),
    (r"(\d+\.?\d*)\+?\s*pts\b", "pts"),
    # rebounds
    (r"over\s+(\d+\.?\d*)\s*(?:\+)?\s*(?:rebound|reb)", "reb"),
    (r"(\d+\.?\d*)\+?\s*rebound", "reb"),
    (r"(\d+\.?\d*)\+?\s*reb\b", "reb"),
    # assists
    (r"over\s+(\d+\.?\d*)\s*(?:\+)?\s*(?:assist|ast)", "ast"),
    (r"(\d+\.?\d*)\+?\s*assist", "ast"),
    (r"(\d+\.?\d*)\+?\s*ast\b", "ast"),
    # threes
    (r"over\s+(\d+\.?\d*)\s*(?:\+)?\s*(?:three|3-pointer|3pm|threes)", "3pm"),
    (r"(\d+\.?\d*)\+?\s*(?:three|3-pointer|threes)", "3pm"),
    # steals / blocks / turnovers
    (r"(\d+\.?\d*)\+?\s*steal", "stl"),
    (r"(\d+\.?\d*)\+?\s*block", "blk"),
    (r"(\d+\.?\d*)\+?\s*turnover", "tov"),
    # combos
    (r"(\d+\.?\d*)\+?\s*(?:pts\+reb\+ast|pra|p\+r\+a)", "pra"),
    (r"(\d+\.?\d*)\+?\s*(?:pts\+reb|pr)\b", "pr"),
    (r"(\d+\.?\d*)\+?\s*(?:pts\+ast|pa)\b", "pa"),
    (r"(\d+\.?\d*)\+?\s*(?:reb\+ast|ra)\b", "ra"),
]

# "Will LeBron James score ..."
_WILL_RE = re.compile(
    r"will\s+([a-z][a-z\s\-'\.]{2,30}?)\s+"
    r"(?:score|have|record|make|grab|dish|tally|log|post|get|total|hit)",
    re.I,
)
# "LeBron James: Over 25.5 Points"  or  "LeBron James - points - over 25.5"
_NAME_COLON_RE = re.compile(
    r"^([A-Z][a-z]+(?:\s+[A-Z][a-z'\.]+){1,3})\s*[:\-]",
)
# "LeBron James total points over 25.5"
_NAME_TOTAL_RE = re.compile(
    r"^([A-Z][a-z]+(?:\s+[A-Z][a-z'\.]+){1,3})\s+(?:total|over|to\s+have|to\s+score)",
)


def parse_market(raw: dict) -> Optional[dict]:
    ticker   = raw.get("ticker", "")
    title    = raw.get("title", "")
    subtitle = raw.get("subtitle", "")
    full_lc  = f"{title} {subtitle}".lower()

    stat_type, line = _extract_stat(full_lc)
    if line is None:
        return None

    player_name = _extract_player(title, subtitle, ticker)
    if not player_name:
        return None

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
        "game_date":     _extract_date(raw),
        "home_team":     "",
        "away_team":     "",
    }


def _extract_stat(text: str) -> tuple[str, Optional[float]]:
    for pattern, stype in _STAT_PATTERNS:
        m = re.search(pattern, text, re.I)
        if m:
            try:
                return stype, float(m.group(1))
            except ValueError:
                continue
    return "pts", None


def _extract_player(title: str, subtitle: str, ticker: str) -> Optional[str]:
    # 1. "Will [Player] score/have/..."
    m = _WILL_RE.search(title)
    if m:
        return _clean_name(m.group(1))

    # 2. "Player Name: ..." or "Player Name - ..."
    m = _NAME_COLON_RE.match(title)
    if m:
        return _clean_name(m.group(1))

    # 3. "Player Name total/over ..."
    m = _NAME_TOTAL_RE.match(title)
    if m:
        return _clean_name(m.group(1))

    # 4. Same patterns on subtitle
    for text in (subtitle,):
        m = _WILL_RE.search(text)
        if m:
            return _clean_name(m.group(1))
        m = _NAME_COLON_RE.match(text)
        if m:
            return _clean_name(m.group(1))

    return None


def _clean_name(raw: str) -> str:
    name = raw.strip().title()
    # Remove trailing noise words
    for noise in (" To ", " The ", " A ", " An ", " In "):
        if noise in name:
            name = name[:name.index(noise)].strip()
    return name


def _extract_date(raw: dict) -> str:
    for field in ("close_time", "expiration_time"):
        ts = raw.get(field, "")
        if ts:
            try:
                dt = datetime.fromisoformat(ts.replace("Z", "+00:00"))
                return dt.strftime("%Y-%m-%d")
            except ValueError:
                pass
    return date.today().isoformat()


# ---------------------------------------------------------------------------
# Portfolio & Ordering
# ---------------------------------------------------------------------------

def get_balance() -> float:
    data = _get("/portfolio/balance")
    return data.get("balance", 0) / 100.0


def get_positions() -> list[dict]:
    data = _get("/portfolio/positions")
    return data.get("market_positions", [])


def place_order(ticker: str, side: str, contracts: int,
                price_cents: int, order_type: str = "limit") -> dict:
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
    return _get(f"/markets/{ticker}")


def get_market_history(ticker: str) -> list[dict]:
    data = _get(f"/markets/{ticker}/history")
    return data.get("history", [])


def cancel_order(order_id: str) -> dict:
    resp = _session.delete(f"{_BASE}/portfolio/orders/{order_id}",
                           headers=_auth_header(), timeout=15)
    resp.raise_for_status()
    return resp.json()
