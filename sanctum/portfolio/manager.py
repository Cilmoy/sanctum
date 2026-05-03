"""
manager.py — Portfolio manager for persistent storage.
"""

from typing import List, Dict
from data.cache import SanctumDB

class PortfolioManager:
    def __init__(self, db: SanctumDB):
        self.db = db

    def add(self, ticker: str, shares: float, avg_cost: float) -> None:
        self.db.add_to_portfolio(ticker, shares, avg_cost)

    def remove(self, ticker: str) -> None:
        self.db.remove_from_portfolio(ticker)

    def list(self) -> List[Dict]:
        return self.db.get_portfolio()
