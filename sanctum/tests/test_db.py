import os
import time
import pytest
from pathlib import Path
from sanctum.data.cache import SanctumDB

class MockStockData:
    def __init__(self, ticker, price):
        self.ticker = ticker
        self.current_price = price

    def to_dict(self):
        return {"ticker": self.ticker, "current_price": self.current_price}

    @classmethod
    def from_dict(cls, d):
        return cls(d["ticker"], d["current_price"])

@pytest.fixture
def temp_db_path(tmp_path):
    db_file = tmp_path / "test_sanctum.db"
    return str(db_file)

@pytest.fixture
def db(temp_db_path):
    config = {
        "cache": {
            "db_path": temp_db_path,
            "ttl_hours": 1
        }
    }
    db_instance = SanctumDB(config)
    yield db_instance
    db_instance.close()

def test_db_initialization(temp_db_path):
    config = {"cache": {"db_path": temp_db_path}}
    db = SanctumDB(config)
    assert os.path.exists(temp_db_path)
    db.close()

def test_portfolio_operations(db):
    # Add
    db.add_to_portfolio("AAPL", 10, 150.0)
    db.add_to_portfolio("MSFT", 5, 300.0)
    
    portfolio = db.get_portfolio()
    assert len(portfolio) == 2
    assert portfolio[0]["ticker"] == "AAPL"
    assert portfolio[0]["shares"] == 10
    assert portfolio[0]["avg_cost"] == 150.0
    
    # Update (Replace)
    db.add_to_portfolio("AAPL", 20, 160.0)
    portfolio = db.get_portfolio()
    assert len(portfolio) == 2
    assert portfolio[0]["shares"] == 20
    
    # Remove
    db.remove_from_portfolio("AAPL")
    portfolio = db.get_portfolio()
    assert len(portfolio) == 1
    assert portfolio[0]["ticker"] == "MSFT"

def test_watchlist_operations(db):
    # Add
    db.add_to_watchlist("TSLA")
    db.add_to_watchlist("GOOGL")
    
    watchlist = db.get_watchlist()
    assert len(watchlist) == 2
    assert "TSLA" in watchlist
    assert "GOOGL" in watchlist
    
    # Remove
    db.remove_from_watchlist("TSLA")
    watchlist = db.get_watchlist()
    assert len(watchlist) == 1
    assert "GOOGL" in watchlist
    assert "TSLA" not in watchlist

def test_stock_cache_operations(db, monkeypatch):
    # We need to mock StockData in data.fetcher for db.get to work
    import data.fetcher
    monkeypatch.setattr(data.fetcher, "StockData", MockStockData)
    
    ticker = "NVDA"
    data = MockStockData(ticker, 500.0)
    
    # Set
    db.set(ticker, data)
    
    # Get
    cached_data = db.get(ticker)
    assert cached_data is not None
    assert cached_data.ticker == ticker
    assert cached_data.current_price == 500.0
    
    # Invalidate
    db.invalidate(ticker)
    assert db.get(ticker) is None

def test_stock_cache_ttl(db, monkeypatch):
    import data.fetcher
    monkeypatch.setattr(data.fetcher, "StockData", MockStockData)
    
    ticker = "AMD"
    data = MockStockData(ticker, 100.0)
    
    # Mock time to be in the past
    original_time = time.time()
    
    # Set TTL to 1 second for this test
    db.ttl_seconds = 1
    
    db.set(ticker, data)
    assert db.get(ticker) is not None
    
    # Fast forward time
    monkeypatch.setattr(time, "time", lambda: original_time + 10)
    
    # Should be expired
    assert db.get(ticker) is None

def test_clear_cache(db, monkeypatch):
    import data.fetcher
    monkeypatch.setattr(data.fetcher, "StockData", MockStockData)
    
    db.set("AAPL", MockStockData("AAPL", 150))
    db.set("MSFT", MockStockData("MSFT", 300))
    
    assert db.get("AAPL") is not None
    assert db.get("MSFT") is not None
    
    db.clear_cache()
    
    assert db.get("AAPL") is None
    assert db.get("MSFT") is None
