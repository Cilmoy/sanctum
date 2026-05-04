"""
technicals.py — Technical analysis models for the Alpha Pipeline.

Calculates Bollinger Band Width (BBW) and detects Volatility Squeezes.
Focuses on 'Convexity' — identifying periods of extreme low volatility 
that often precede explosive directional moves.
"""

import logging
import numpy as np
import pandas as pd
from typing import Dict, Any, Optional

logger = logging.getLogger(__name__)

def compute_technicals(stock_data, history: pd.DataFrame) -> Dict[str, Any]:
    """
    Compute BBW and Squeeze metrics from price history.
    
    Parameters
    ----------
    stock_data : StockData
    history : pd.DataFrame
        Daily price history. Expects 'Close' column.
        Should have at least 120 days for a robust 100-day squeeze window.
        
    Returns
    -------
    dict with keys:
        bbw                 float   Current Bollinger Band Width
        is_squeeze          bool    True if in a volatility squeeze
        squeeze_percentile  float   Current BBW percentile (0-100)
    """
    res = {
        "bbw": 0.0,
        "is_squeeze": False,
        "squeeze_percentile": 50.0,
    }
    
    if history is None or history.empty or "Close" not in history.columns:
        return res
        
    closes = history["Close"].astype(float)
    if len(closes) < 20:
        return res
        
    # Bollinger Bands (Standard 20, 2)
    ma20 = closes.rolling(window=20).mean()
    std20 = closes.rolling(window=20).std()
    
    upper_band = ma20 + (2 * std20)
    lower_band = ma20 - (2 * std20)
    
    # BBW = (Upper - Lower) / Middle
    bbw_series = (upper_band - lower_band) / ma20
    bbw_series = bbw_series.dropna()
    
    if bbw_series.empty:
        return res
        
    current_bbw = float(bbw_series.iloc[-1])
    res["bbw"] = current_bbw
    
    # Squeeze Detection: BBW at lowest 10% of trailing 100-day range
    if len(bbw_series) >= 100:
        trailing_100 = bbw_series.tail(100)
        low = trailing_100.min()
        high = trailing_100.max()
        
        if high > low:
            percentile = (current_bbw - low) / (high - low)
        else:
            percentile = 0.5
            
        res["squeeze_percentile"] = float(percentile * 100.0)
        res["is_squeeze"] = percentile <= 0.10
        
    return res
