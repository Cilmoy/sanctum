"""
finviz_scraper.py — Scrapes Finviz quote pages for technical and insider data.

Provides a keyless fallback/supplement for data points like RVOL, RSI, 
and insider buying that can be spotty in yfinance.
"""

import logging
import requests
from bs4 import BeautifulSoup
import re
from typing import Dict, Any, Optional

logger = logging.getLogger(__name__)

HEADERS = {
    "User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/120.0.0.0 Safari/537.36"
}

def fetch_finviz_data(ticker: str) -> Dict[str, Any]:
    """
    Fetch RVOL, RSI, Volatility, Short Float, and Insider Trading from Finviz.
    
    Returns a dict with extracted values or defaults on failure.
    """
    url = f"https://finviz.com/quote.ashx?t={ticker}"
    
    defaults = {
        "relative_volume": None,
        "rsi_14": None,
        "volatility_week": None,
        "volatility_month": None,
        "short_pct_float": None,
        "insider_buys_60d": 0,
        "insider_buy_value_60d": 0.0,
    }
    
    try:
        response = requests.get(url, headers=HEADERS, timeout=10)
        if response.status_code != 200:
            logger.warning(f"Finviz fetch failed for {ticker} (Status {response.status_code})")
            return defaults
        
        soup = BeautifulSoup(response.text, "html.parser")
        
        # 1. Extract from the main stats table (snapshot-table2)
        data = _parse_snapshot_table(soup)
        
        # 2. Extract insider trading (insider-table)
        insider_data = _parse_insider_table(soup)
        
        # Merge results
        result = defaults.copy()
        result.update(data)
        result["insider_buys_60d"] = insider_data["buys_60d"]
        result["insider_buy_value_60d"] = insider_data["buy_value_60d"]
        
        return result
        
    except Exception as e:
        logger.error(f"Error scraping Finviz for {ticker}: {e}")
        return defaults

def _parse_snapshot_table(soup: BeautifulSoup) -> Dict[str, Any]:
    """Parses the main snapshot-table2 for technical metrics."""
    res = {}
    
    # Table rows and cells
    table = soup.find("table", class_="snapshot-table2")
    if not table:
        return res
    
    cells = table.find_all("td")
    # Finviz layout: Label | Value | Label | Value ...
    for i in range(0, len(cells) - 1, 2):
        label = cells[i].text.strip()
        value = cells[i+1].text.strip()
        
        if label == "Rel Volume":
            res["relative_volume"] = _to_float(value)
        elif label == "RSI (14)":
            res["rsi_14"] = _to_float(value)
        elif label == "Volatility":
            # Format: "Week % Month %"
            parts = value.split()
            if len(parts) >= 1:
                res["volatility_week"] = _to_float(parts[0]) / 100.0
            if len(parts) >= 2:
                res["volatility_month"] = _to_float(parts[1]) / 100.0
        elif label == "Short Float":
            res["short_pct_float"] = _to_float(value) / 100.0
            
    return res

def _parse_insider_table(soup: BeautifulSoup) -> Dict[str, float]:
    """Parses the insider trading table for BUYs."""
    res = {"buys_60d": 0, "buy_value_60d": 0.0}
    
    # Insider table is usually within a div or table with specific classes
    # It has rows like: Insider | Relationship | Date | Transaction | Cost | #Shares | Value ($) | #Shares Total | SEC Form 4
    table = soup.find("table", class_="insider-table")
    if not table:
        return res
    
    import datetime
    cutoff = datetime.date.today() - datetime.timedelta(days=60)
    
    # Skip header
    rows = table.find_all("tr")[1:]
    
    unique_insiders = set()
    total_val = 0.0
    
    for row in rows:
        cols = row.find_all("td")
        if len(cols) < 7:
            continue
        
        insider = cols[0].text.strip()
        date_str = cols[2].text.strip() # e.g. "Oct 23"
        trans = cols[3].text.strip()
        cost = _to_float(cols[4].text.strip())
        shares = _to_float(cols[5].text.strip())
        value = _to_float(cols[6].text.strip())
        
        # Only focus on BUY
        if "Buy" not in trans:
            continue
        # Filter out Option Exercise if possible (Finviz usually labels as "Option Exercise")
        if "Option" in trans or "Exercise" in trans:
            continue
            
        try:
            # Finviz date format "Month Day" or "Month Day Year"
            # If it's just "Oct 23", it's the current year.
            parts = date_str.split()
            if len(parts) == 2:
                # Add current year
                date_obj = datetime.datetime.strptime(f"{date_str} {datetime.date.today().year}", "%b %d %Y").date()
                # If the date is in the future, it was probably from last year
                if date_obj > datetime.date.today():
                    date_obj = date_obj.replace(year=date_obj.year - 1)
            elif len(parts) == 3:
                date_obj = datetime.datetime.strptime(date_str, "%b %d %Y").date()
            else:
                continue
                
            if date_obj >= cutoff:
                unique_insiders.add(insider)
                total_val += value
                
        except Exception:
            continue
            
    res["buys_60d"] = len(unique_insiders)
    res["buy_value_60d"] = total_val
    return res

def _to_float(val: str) -> float:
    """Helper to convert finviz strings (e.g. '1.23', '10%', '1.5M') to float."""
    if not val or val == "-":
        return 0.0
    
    # Remove %, commas
    clean = val.replace("%", "").replace(",", "")
    
    # Handle multipliers
    multiplier = 1.0
    if clean.endswith("K"):
        multiplier = 1e3
        clean = clean[:-1]
    elif clean.endswith("M"):
        multiplier = 1e6
        clean = clean[:-1]
    elif clean.endswith("B"):
        multiplier = 1e9
        clean = clean[:-1]
        
    try:
        return float(clean) * multiplier
    except ValueError:
        return 0.0
