import sqlite3
import json
from contextlib import contextmanager
from config import DB_PATH


def init_db():
    with get_conn() as conn:
        conn.executescript("""
            CREATE TABLE IF NOT EXISTS markets (
                ticker TEXT PRIMARY KEY,
                event_ticker TEXT,
                series_ticker TEXT,
                title TEXT,
                player_name TEXT,
                stat_type TEXT,
                line REAL,
                yes_ask INTEGER,
                yes_bid INTEGER,
                no_ask INTEGER,
                no_bid INTEGER,
                volume INTEGER DEFAULT 0,
                open_interest INTEGER DEFAULT 0,
                close_time TEXT,
                game_date TEXT,
                home_team TEXT,
                away_team TEXT,
                fetched_at TEXT DEFAULT (datetime('now'))
            );

            CREATE TABLE IF NOT EXISTS analysis (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                market_ticker TEXT REFERENCES markets(ticker),
                our_probability REAL,
                implied_probability REAL,
                edge REAL,
                confidence TEXT,
                sample_size INTEGER,
                base_rate REAL,
                factors TEXT,
                news_summary TEXT,
                analyzed_at TEXT DEFAULT (datetime('now'))
            );

            CREATE TABLE IF NOT EXISTS picks (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                market_ticker TEXT REFERENCES markets(ticker),
                analysis_id INTEGER REFERENCES analysis(id),
                side TEXT DEFAULT 'yes',
                alerted_at TEXT,
                user_selected INTEGER DEFAULT 0,
                status TEXT DEFAULT 'pending',
                created_at TEXT DEFAULT (datetime('now'))
            );

            CREATE TABLE IF NOT EXISTS bets (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                pick_id INTEGER REFERENCES picks(id),
                contracts INTEGER,
                entry_price INTEGER,
                placed_at TEXT DEFAULT (datetime('now')),
                kalshi_order_id TEXT,
                is_live INTEGER DEFAULT 0,
                resolved_at TEXT,
                outcome TEXT,
                profit_loss REAL
            );

            CREATE INDEX IF NOT EXISTS idx_markets_game_date ON markets(game_date);
            CREATE INDEX IF NOT EXISTS idx_analysis_market ON analysis(market_ticker);
            CREATE INDEX IF NOT EXISTS idx_picks_status ON picks(status);
            CREATE INDEX IF NOT EXISTS idx_bets_pick ON bets(pick_id);
        """)


@contextmanager
def get_conn():
    conn = sqlite3.connect(DB_PATH)
    conn.row_factory = sqlite3.Row
    conn.execute("PRAGMA journal_mode=WAL")
    try:
        yield conn
        conn.commit()
    except Exception:
        conn.rollback()
        raise
    finally:
        conn.close()


def upsert_market(market: dict):
    with get_conn() as conn:
        conn.execute("""
            INSERT INTO markets (
                ticker, event_ticker, series_ticker, title, player_name,
                stat_type, line, yes_ask, yes_bid, no_ask, no_bid,
                volume, open_interest, close_time, game_date, home_team,
                away_team, fetched_at
            ) VALUES (
                :ticker, :event_ticker, :series_ticker, :title, :player_name,
                :stat_type, :line, :yes_ask, :yes_bid, :no_ask, :no_bid,
                :volume, :open_interest, :close_time, :game_date, :home_team,
                :away_team, datetime('now')
            )
            ON CONFLICT(ticker) DO UPDATE SET
                yes_ask=excluded.yes_ask,
                yes_bid=excluded.yes_bid,
                no_ask=excluded.no_ask,
                no_bid=excluded.no_bid,
                volume=excluded.volume,
                open_interest=excluded.open_interest,
                fetched_at=excluded.fetched_at
        """, market)


def save_analysis(analysis: dict) -> int:
    analysis["factors"] = json.dumps(analysis.get("factors", {}))
    with get_conn() as conn:
        cur = conn.execute("""
            INSERT INTO analysis (
                market_ticker, our_probability, implied_probability, edge,
                confidence, sample_size, base_rate, factors, news_summary
            ) VALUES (
                :market_ticker, :our_probability, :implied_probability, :edge,
                :confidence, :sample_size, :base_rate, :factors, :news_summary
            )
        """, analysis)
        return cur.lastrowid


def save_pick(market_ticker: str, analysis_id: int, side: str = "yes") -> int:
    with get_conn() as conn:
        cur = conn.execute("""
            INSERT INTO picks (market_ticker, analysis_id, side)
            VALUES (?, ?, ?)
        """, (market_ticker, analysis_id, side))
        return cur.lastrowid


def mark_pick_alerted(pick_id: int):
    with get_conn() as conn:
        conn.execute(
            "UPDATE picks SET alerted_at=datetime('now') WHERE id=?", (pick_id,)
        )


def select_pick(pick_id: int):
    with get_conn() as conn:
        conn.execute(
            "UPDATE picks SET user_selected=1 WHERE id=?", (pick_id,)
        )


def record_bet(pick_id: int, contracts: int, entry_price: int,
               is_live: bool = False, kalshi_order_id: str = None) -> int:
    with get_conn() as conn:
        cur = conn.execute("""
            INSERT INTO bets (pick_id, contracts, entry_price, is_live, kalshi_order_id)
            VALUES (?, ?, ?, ?, ?)
        """, (pick_id, contracts, entry_price, int(is_live), kalshi_order_id))
        return cur.lastrowid


def resolve_bet(bet_id: int, outcome: str, profit_loss: float):
    with get_conn() as conn:
        conn.execute("""
            UPDATE bets SET outcome=?, profit_loss=?, resolved_at=datetime('now')
            WHERE id=?
        """, (outcome, profit_loss, bet_id))
        conn.execute("""
            UPDATE picks SET status=? WHERE id=(SELECT pick_id FROM bets WHERE id=?)
        """, (outcome, bet_id))


def get_open_bets() -> list:
    with get_conn() as conn:
        rows = conn.execute("""
            SELECT b.*, p.market_ticker, p.side, m.title, m.line, m.stat_type,
                   m.player_name, m.game_date, m.close_time
            FROM bets b
            JOIN picks p ON b.pick_id = p.id
            JOIN markets m ON p.market_ticker = m.ticker
            WHERE b.outcome IS NULL
            ORDER BY m.game_date
        """).fetchall()
        return [dict(r) for r in rows]


def get_top_picks(date: str = None, min_edge: float = 0.0, limit: int = 10) -> list:
    with get_conn() as conn:
        query = """
            SELECT p.id as pick_id, p.side, m.ticker, m.title, m.player_name,
                   m.stat_type, m.line, m.yes_ask, m.game_date, m.close_time,
                   a.our_probability, a.implied_probability, a.edge,
                   a.confidence, a.sample_size, a.factors, a.news_summary
            FROM picks p
            JOIN markets m ON p.market_ticker = m.ticker
            JOIN analysis a ON p.analysis_id = a.id
            WHERE a.edge >= ?
              AND p.status = 'pending'
        """
        params = [min_edge]
        if date:
            query += " AND m.game_date = ?"
            params.append(date)
        query += " ORDER BY a.edge DESC LIMIT ?"
        params.append(limit)
        rows = conn.execute(query, params).fetchall()
        return [dict(r) for r in rows]


def get_performance_summary() -> dict:
    with get_conn() as conn:
        row = conn.execute("""
            SELECT
                COUNT(*) as total_bets,
                SUM(CASE WHEN outcome='won' THEN 1 ELSE 0 END) as wins,
                SUM(CASE WHEN outcome='lost' THEN 1 ELSE 0 END) as losses,
                SUM(CASE WHEN outcome IS NULL THEN 1 ELSE 0 END) as pending,
                ROUND(SUM(COALESCE(profit_loss, 0)), 2) as total_pnl,
                ROUND(AVG(CASE WHEN outcome IS NOT NULL THEN profit_loss END), 2) as avg_pnl
            FROM bets
        """).fetchone()
        return dict(row) if row else {}
