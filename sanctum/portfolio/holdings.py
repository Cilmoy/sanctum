"""
holdings.py — Load current portfolio state from CSV or YAML.

CSV format: ticker,shares
  GOOG,10
  NVDA,5
"""

import csv
import logging
from pathlib import Path

import yaml

logger = logging.getLogger(__name__)


def load_holdings(path: str) -> dict[str, float]:
    """
    Load holdings from a CSV or YAML file.

    Returns dict of {ticker: shares}.
    """
    p = Path(path)
    if not p.exists():
        raise FileNotFoundError(f"Holdings file not found: {path}")

    if p.suffix in (".yaml", ".yml"):
        with open(p) as f:
            data = yaml.safe_load(f)
        return {k.upper(): float(v) for k, v in data.items()}

    # Default: CSV
    holdings = {}
    with open(p) as f:
        reader = csv.DictReader(f)
        for row in reader:
            ticker = row.get("ticker", row.get("Ticker", "")).strip().upper()
            shares = float(row.get("shares", row.get("Shares", 0)))
            if ticker:
                holdings[ticker] = shares

    logger.info(f"Loaded {len(holdings)} holdings from {path}")
    return holdings
