"""
watchlist.py — Watchlist manager for persistent storage.
"""

from typing import List
from data.cache import SanctumDB

class WatchlistManager:
    def __init__(self, db: SanctumDB):
        self.db = db

    def add(self, ticker: str) -> None:
        self.db.add_to_watchlist(ticker)

    def remove(self, ticker: str) -> None:
        self.db.remove_from_watchlist(ticker)

    def list(self) -> List[str]:
        return self.db.get_watchlist()
