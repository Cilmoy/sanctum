"""
cache.py — SQLite-backed local cache for StockData objects and persistent state.

Uses JSON serialization (not pickle) to avoid arbitrary code execution
on a tampered cache file. TTL is configurable.
"""

import json
import logging
import sqlite3
import threading
import time
from pathlib import Path
from typing import Optional, List, Dict

logger = logging.getLogger(__name__)


class SanctumDB:
    """JSON-serialized StockData objects and persistent portfolio/watchlist stored in SQLite."""

    def __init__(self, config: dict):
        cache_cfg = config.get("cache", {})
        self.ttl_seconds = cache_cfg.get("ttl_hours", 24) * 3600
        db_path = Path(cache_cfg.get("db_path", ".cache/sanctum.db"))
        db_path.parent.mkdir(parents=True, exist_ok=True)
        self._lock = threading.Lock()
        self.conn = sqlite3.connect(str(db_path), check_same_thread=False)
        self._init_db()

    def _init_db(self) -> None:
        with self._lock:
            # Stock data cache
            self.conn.execute(
                """
                CREATE TABLE IF NOT EXISTS stock_cache (
                    ticker TEXT PRIMARY KEY,
                    data   TEXT NOT NULL,
                    ts     REAL NOT NULL
                )
                """
            )
            # Portfolio table
            self.conn.execute(
                """
                CREATE TABLE IF NOT EXISTS portfolio (
                    ticker TEXT PRIMARY KEY,
                    shares REAL NOT NULL,
                    avg_cost REAL NOT NULL,
                    ts     REAL NOT NULL
                )
                """
            )
            # Watchlist table
            self.conn.execute(
                """
                CREATE TABLE IF NOT EXISTS watchlist (
                    ticker TEXT PRIMARY KEY,
                    ts     REAL NOT NULL
                )
                """
            )
            self.conn.commit()

    # --- Stock Cache (for DataFetcher) ---

    def get(self, ticker: str) -> Optional[object]:
        """Return cached StockData if present and not expired, else None."""
        from data.fetcher import StockData

        with self._lock:
            row = self.conn.execute(
                "SELECT data, ts FROM stock_cache WHERE ticker = ?", (ticker,)
            ).fetchone()

        if row is None:
            return None

        data_text, ts = row
        if time.time() - ts > self.ttl_seconds:
            logger.debug(f"{ticker}: cache expired")
            with self._lock:
                self.conn.execute("DELETE FROM stock_cache WHERE ticker = ?", (ticker,))
                self.conn.commit()
            return None

        try:
            d = json.loads(data_text)
            return StockData.from_dict(d)
        except Exception as e:
            logger.warning(f"{ticker}: cache deserialization error — {e}")
            return None

    def set(self, ticker: str, data: object) -> None:
        """Store a StockData object in the cache."""
        try:
            text = json.dumps(data.to_dict())
        except Exception as e:
            logger.warning(f"{ticker}: cache serialization error — {e}")
            return

        with self._lock:
            self.conn.execute(
                "INSERT OR REPLACE INTO stock_cache (ticker, data, ts) VALUES (?, ?, ?)",
                (ticker, text, time.time()),
            )
            self.conn.commit()

    def invalidate(self, ticker: str) -> None:
        with self._lock:
            self.conn.execute("DELETE FROM stock_cache WHERE ticker = ?", (ticker,))
            self.conn.commit()

    def clear_cache(self) -> None:
        with self._lock:
            self.conn.execute("DELETE FROM stock_cache")
            self.conn.commit()
        logger.info("Cache cleared.")

    # --- Portfolio Management ---

    def add_to_portfolio(self, ticker: str, shares: float, avg_cost: float) -> None:
        with self._lock:
            self.conn.execute(
                "INSERT OR REPLACE INTO portfolio (ticker, shares, avg_cost, ts) VALUES (?, ?, ?, ?)",
                (ticker.upper(), shares, avg_cost, time.time()),
            )
            self.conn.commit()

    def remove_from_portfolio(self, ticker: str) -> None:
        with self._lock:
            self.conn.execute("DELETE FROM portfolio WHERE ticker = ?", (ticker.upper(),))
            self.conn.commit()

    def get_portfolio(self) -> List[Dict]:
        with self._lock:
            cursor = self.conn.execute("SELECT ticker, shares, avg_cost, ts FROM portfolio ORDER BY ticker")
            return [
                {"ticker": row[0], "shares": row[1], "avg_cost": row[2], "ts": row[3]}
                for row in cursor.fetchall()
            ]

    # --- Watchlist Management ---

    def add_to_watchlist(self, ticker: str) -> None:
        with self._lock:
            self.conn.execute(
                "INSERT OR REPLACE INTO watchlist (ticker, ts) VALUES (?, ?)",
                (ticker.upper(), time.time()),
            )
            self.conn.commit()

    def remove_from_watchlist(self, ticker: str) -> None:
        with self._lock:
            self.conn.execute("DELETE FROM watchlist WHERE ticker = ?", (ticker.upper(),))
            self.conn.commit()

    def get_watchlist(self) -> List[str]:
        with self._lock:
            cursor = self.conn.execute("SELECT ticker FROM watchlist ORDER BY ticker")
            return [row[0] for row in cursor.fetchall()]

    def close(self) -> None:
        with self._lock:
            self.conn.close()
