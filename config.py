import os
from dotenv import load_dotenv

load_dotenv()

KALSHI_API_KEY = os.getenv("KALSHI_API_KEY", "")
KALSHI_BASE_URL = os.getenv("KALSHI_BASE_URL", "https://trading-api.kalshi.com/trade-api/v2")

IMESSAGE_TO = os.getenv("IMESSAGE_TO", "")

DB_PATH = os.getenv("DB_PATH", "kalshi_nba.db")

MIN_EDGE_THRESHOLD = float(os.getenv("MIN_EDGE_THRESHOLD", "0.05"))
RECENT_GAMES_WINDOW = int(os.getenv("RECENT_GAMES_WINDOW", "10"))
DECAY_FACTOR = float(os.getenv("DECAY_FACTOR", "0.92"))

MORNING_SCAN_HOUR = int(os.getenv("MORNING_SCAN_HOUR", "10"))
PREGAME_ALERT_MINUTES_BEFORE = int(os.getenv("PREGAME_ALERT_MINUTES_BEFORE", "60"))

# Stat types Kalshi uses and their nba_api column mappings
STAT_COLUMN_MAP = {
    "pts": "PTS",
    "reb": "REB",
    "ast": "AST",
    "stl": "STL",
    "blk": "BLK",
    "tov": "TOV",
    "3pm": "FG3M",
    "pra": None,   # pts+reb+ast (computed)
    "pr":  None,   # pts+reb (computed)
    "pa":  None,   # pts+ast (computed)
    "ra":  None,   # reb+ast (computed)
}

COMPOSITE_STAT_MAP = {
    "pra": ["PTS", "REB", "AST"],
    "pr":  ["PTS", "REB"],
    "pa":  ["PTS", "AST"],
    "ra":  ["REB", "AST"],
}
