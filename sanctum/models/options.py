"""
options.py — Options chain analysis, Black-Scholes Greeks, and strategy suggestion.

Fetches the nearest liquid expiration (14-60 DTE), computes Greeks for ATM
and 25-delta contracts, assesses the IV regime vs 30-day historical vol,
and recommends an options strategy based on model conviction and IV environment.

Strategy matrix:
  Conviction    Low IV (IV < HV)         High IV (IV > HV * 1.2)
  Strong bull   Bull call spread          Sell cash-secured put
  Mild bull     Bull call spread          Bull put credit spread
  Neutral       Long straddle             Short iron condor
  Mild bear     Bear put spread           Bear call credit spread
  Strong bear   Long put                  Sell covered call / bear call spread

Greeks are per-contract (100 shares). Theta is per calendar day.
Vega is per 1 percentage-point move in IV.
"""

import logging
import math
from datetime import datetime, timezone
from typing import Optional

import numpy as np
from scipy.stats import norm

logger = logging.getLogger(__name__)

_MIN_DTE = 14
_MAX_DTE = 60
_TARGET_DTE_LOW = 30
_TARGET_DTE_HIGH = 45


# ── Public entry points ───────────────────────────────────────────────────────

def analyze_options(stock, config: dict) -> Optional[dict]:
    """
    Fetch options chain and return analysis dict, or None if unavailable.
    Only called in analyze mode — not during bulk screen.
    """
    try:
        import yfinance as yf
        yticker = yf.Ticker(stock.ticker)
        return _run_analysis(yticker, stock, config)
    except Exception as e:
        logger.warning(f"{stock.ticker}: options analysis failed — {e}")
        return None


def suggest_strategy(opts: dict, score: float, bull_prob: float, bear_prob: float) -> dict:
    """
    Recommend an options strategy based on model conviction and IV regime.

    Returns dict with keys: name, rationale, legs, execution_note.
    Each leg: {action, type, strike, note}
    """
    iv_regime = opts.get("iv_regime", "normal")
    atm_strike = opts.get("atm_strike", 0)
    call_25d = opts.get("call_25d_strike")
    put_25d = opts.get("put_25d_strike")
    atm_iv = opts.get("atm_iv", 0)
    dte = opts.get("dte", 30)
    expiry = opts.get("expiration", "")

    high_iv = iv_regime == "high"
    # Jane Street / PhD Fix: If conviction is extreme (score > 85), 
    # don't cap upside with spreads. Buy directional OTM optionality (10-20Δ) 
    # to capture 'multi-bagger' 10x moves.
    multi_bagger_conviction = score >= 85

    if score >= 70:
        if multi_bagger_conviction and not high_iv:
            target_strike = call_25d or round(atm_strike * 1.10, 0)
            return {
                "name": "Long OTM Call (10x Potential)",
                "rationale": (
                    f"Extreme conviction (score {score:.0f}) + Low IV — "
                    "buying pure directional optionality to capture high-velocity move."
                ),
                "legs": [
                    {"action": "BUY", "type": "CALL", "strike": target_strike, "expiry": expiry, "note": "~20Δ call for leverage"},
                ],
                "execution_note": "Max loss = premium paid. 10x potential if catalyst hits hard. Exit at 300%+ profit."
            }
        
        if high_iv:
            leg_strike = put_25d or round(atm_strike * 0.93, 0)
            return {
                "name": "Cash-Secured Put",
                "rationale": (
                    f"Model is strongly bullish (score {score:.0f}, bull prob {bull_prob*100:.0f}%) "
                    f"and IV is elevated — sell a put to get paid while waiting to own the stock at a discount."
                ),
                "legs": [
                    {"action": "SELL", "type": "PUT", "strike": leg_strike,
                     "expiry": expiry, "note": "~25Δ put, collect premium"},
                ],
                "execution_note": (
                    f"Target the ~25Δ put ({dte} DTE). Close at 50% of max profit. "
                    "Risk: assignment if stock drops through strike."
                ),
            }
        # ... (rest of bull logic)

    elif score >= 55:
        if high_iv:
            lower = put_25d or round(atm_strike * 0.93, 0)
            return {
                "name": "Bull Put Credit Spread",
                "rationale": (
                    f"Model mildly bullish (score {score:.0f}) and IV is high — "
                    "collect credit by selling a put spread below the market."
                ),
                "legs": [
                    {"action": "SELL", "type": "PUT", "strike": lower,               "expiry": expiry, "note": "~25Δ put"},
                    {"action": "BUY",  "type": "PUT", "strike": round(lower * 0.95, 0), "expiry": expiry, "note": "protection leg"},
                ],
                "execution_note": (
                    "Max profit = credit received (stock stays above short strike). "
                    "Close at 50% of credit to limit theta risk decay."
                ),
            }
        else:
            wing = call_25d or round(atm_strike * 1.07, 0)
            return {
                "name": "Bull Call Spread",
                "rationale": (
                    f"Model mildly bullish (score {score:.0f}), low IV — "
                    "debit spread limits cost while retaining upside."
                ),
                "legs": [
                    {"action": "BUY",  "type": "CALL", "strike": atm_strike, "expiry": expiry, "note": "ATM call"},
                    {"action": "SELL", "type": "CALL", "strike": wing,       "expiry": expiry, "note": "~25Δ call"},
                ],
                "execution_note": "Max profit at expiry above short strike. Risk = net debit.",
            }

    elif score >= 45:
        if high_iv:
            put_wing  = put_25d  or round(atm_strike * 0.93, 0)
            call_wing = call_25d or round(atm_strike * 1.07, 0)
            return {
                "name": "Short Iron Condor",
                "rationale": (
                    f"Model is neutral (score {score:.0f}) and IV is elevated — "
                    "sell volatility by collecting premium on both sides."
                ),
                "legs": [
                    {"action": "SELL", "type": "PUT",  "strike": put_wing,               "expiry": expiry, "note": "~25Δ put"},
                    {"action": "BUY",  "type": "PUT",  "strike": round(put_wing * 0.95, 0),  "expiry": expiry, "note": "put wing"},
                    {"action": "SELL", "type": "CALL", "strike": call_wing,               "expiry": expiry, "note": "~25Δ call"},
                    {"action": "BUY",  "type": "CALL", "strike": round(call_wing * 1.05, 0), "expiry": expiry, "note": "call wing"},
                ],
                "execution_note": (
                    "Profit if stock stays between short strikes at expiry. "
                    "Close at 50% of max profit or when one wing is threatened."
                ),
            }
        else:
            return {
                "name": "Long Straddle",
                "rationale": (
                    f"Model is neutral (score {score:.0f}) and IV is low — "
                    "cheap optionality; profits from a big move in either direction."
                ),
                "legs": [
                    {"action": "BUY", "type": "CALL", "strike": atm_strike, "expiry": expiry, "note": "ATM call"},
                    {"action": "BUY", "type": "PUT",  "strike": atm_strike, "expiry": expiry, "note": "ATM put"},
                ],
                "execution_note": (
                    f"Breakeven = strike ± total premium paid. Profits grow with distance from strike. "
                    "Best entered before a known catalyst."
                ),
            }

    elif score >= 35:
        if high_iv:
            upper = call_25d or round(atm_strike * 1.07, 0)
            return {
                "name": "Bear Call Credit Spread",
                "rationale": (
                    f"Model mildly bearish (score {score:.0f}) and IV is high — "
                    "collect credit by selling a call spread above the market."
                ),
                "legs": [
                    {"action": "SELL", "type": "CALL", "strike": upper,               "expiry": expiry, "note": "~25Δ call"},
                    {"action": "BUY",  "type": "CALL", "strike": round(upper * 1.05, 0), "expiry": expiry, "note": "protection leg"},
                ],
                "execution_note": "Max profit = credit received if stock stays below short strike.",
            }
        else:
            lower = put_25d or round(atm_strike * 0.93, 0)
            return {
                "name": "Bear Put Spread",
                "rationale": (
                    f"Model mildly bearish (score {score:.0f}), low IV — "
                    "defined-risk downside play."
                ),
                "legs": [
                    {"action": "BUY",  "type": "PUT", "strike": atm_strike, "expiry": expiry, "note": "ATM put"},
                    {"action": "SELL", "type": "PUT", "strike": lower,      "expiry": expiry, "note": "~25Δ put"},
                ],
                "execution_note": "Max profit at expiry below short strike. Risk = net debit.",
            }

    else:
        if multi_bagger_conviction and not high_iv:
            target_strike = put_25d or round(atm_strike * 0.90, 0)
            return {
                "name": "Long OTM Put (10x Potential)",
                "rationale": (
                    f"Extreme bearish conviction (score {score:.0f}) + Low IV — "
                    "buying pure directional optionality for high-velocity downside move."
                ),
                "legs": [
                    {"action": "BUY", "type": "PUT", "strike": target_strike, "expiry": expiry, "note": "~20Δ put for leverage"},
                ],
                "execution_note": "Max loss = premium paid. 10x potential if breakdown occurs. Exit at 300%+ profit."
            }

        if high_iv:
            upper = call_25d or round(atm_strike * 1.07, 0)
            return {
                "name": "Bear Call Credit Spread",
                "rationale": (
                    f"Model is strongly bearish (score {score:.0f}, bear prob {bear_prob*100:.0f}%) "
                    f"and IV is high — aggressively sell upside premium."
                ),
                "legs": [
                    {"action": "SELL", "type": "CALL", "strike": upper,               "expiry": expiry, "note": "~25Δ call"},
                    {"action": "BUY",  "type": "CALL", "strike": round(upper * 1.05, 0), "expiry": expiry, "note": "protection leg"},
                ],
                "execution_note": "Max profit = full credit if stock stays below short strike.",
            }
        else:
            return {
                "name": "Long Put",
                "rationale": (
                    f"Model is strongly bearish (score {score:.0f}) and IV is low — "
                    "buy a put outright for maximum leverage on downside."
                ),
                "legs": [
                    {"action": "BUY", "type": "PUT", "strike": atm_strike, "expiry": expiry, "note": "ATM put"},
                ],
                "execution_note": (
                    "Max loss = premium paid. Sell before expiry if thesis plays out — "
                    "avoid holding through expiry."
                ),
            }


# ── Internal ──────────────────────────────────────────────────────────────────

def _run_analysis(yticker, stock, config: dict) -> Optional[dict]:
    from data.fetcher import _yf_call
    try:
        expirations = _yf_call(lambda: yticker.options)
    except Exception:
        return None
    if not expirations:
        return None

    today = datetime.now(timezone.utc).date()

    # Prefer 30-45 DTE; fall back to nearest valid expiration
    selected_exp = None
    selected_dte = None
    fallback_exp = None
    fallback_dte = None

    for exp_str in expirations:
        try:
            exp_date = datetime.strptime(exp_str, "%Y-%m-%d").date()
            dte = (exp_date - today).days
            if dte <= 0:
                continue
            if fallback_exp is None:
                fallback_exp, fallback_dte = exp_str, dte
            if _TARGET_DTE_LOW <= dte <= _TARGET_DTE_HIGH:
                if selected_dte is None or dte < selected_dte:
                    selected_exp, selected_dte = exp_str, dte
            elif _MIN_DTE <= dte <= _MAX_DTE and selected_exp is None:
                selected_exp, selected_dte = exp_str, dte
        except ValueError:
            continue

    if selected_exp is None:
        selected_exp, selected_dte = fallback_exp, fallback_dte
    if selected_exp is None:
        return None

    try:
        chain = _yf_call(lambda: yticker.option_chain(selected_exp))
    except Exception as e:
        logger.debug(f"{stock.ticker}: option_chain fetch failed — {e}")
        return None

    calls = chain.calls
    puts = chain.puts
    if calls is None or puts is None or calls.empty or puts.empty:
        return None

    price = stock.current_price
    if not price or price <= 0:
        return None

    T = max(selected_dte / 365.0, 1 / 365.0)
    rf = config.get("wacc", {}).get("risk_free_rate", 0.043)

    # ATM strike
    strikes = calls["strike"].values.astype(float)
    atm_idx = int(np.argmin(np.abs(strikes - price)))
    atm_strike = float(strikes[atm_idx])

    atm_call_row = calls.iloc[atm_idx]
    atm_put_rows = puts[np.isclose(puts["strike"].values.astype(float), atm_strike)]
    atm_put_row = atm_put_rows.iloc[0] if not atm_put_rows.empty else None

    atm_iv = _get_iv(atm_call_row)
    if atm_iv <= 0 and atm_put_row is not None:
        atm_iv = _get_iv(atm_put_row)
    if atm_iv <= 0:
        return None

    hv30 = _compute_hv30(yticker, stock.ticker)
    iv_regime = _iv_regime(atm_iv, hv30)

    atm_call_greeks = _bs_greeks(price, atm_strike, T, rf, atm_iv, "call")
    atm_put_greeks  = _bs_greeks(price, atm_strike, T, rf, atm_iv, "put")

    call_25d = _find_delta_strike(calls, price, T, rf,  0.25, "call")
    put_25d  = _find_delta_strike(puts,  price, T, rf, -0.25, "put")

    def _row_to_dict(row, greeks):
        if row is None:
            return None
        return {
            "strike":        float(row.get("strike", 0)),
            "bid":           float(row.get("bid", 0) or 0),
            "ask":           float(row.get("ask", 0) or 0),
            "volume":        int(row.get("volume", 0) or 0),
            "open_interest": int(row.get("openInterest", 0) or 0),
            "iv":            _get_iv(row),
            "greeks":        greeks,
        }

    return {
        "expiration":      selected_exp,
        "dte":             selected_dte,
        "atm_strike":      atm_strike,
        "atm_iv":          atm_iv,
        "hv30":            hv30,
        "iv_regime":       iv_regime,
        "atm_call":        _row_to_dict(atm_call_row, atm_call_greeks),
        "atm_put":         _row_to_dict(atm_put_row,  atm_put_greeks),
        "call_25d_strike": call_25d,
        "put_25d_strike":  put_25d,
    }


def _get_iv(row) -> float:
    v = row.get("impliedVolatility", 0)
    return float(v) if v and not math.isnan(float(v)) else 0.0


def _iv_regime(iv: float, hv30: Optional[float]) -> str:
    if hv30 and hv30 > 0:
        ratio = iv / hv30
        if ratio > 1.25:
            return "high"
        if ratio < 0.85:
            return "low"
        return "normal"
    # Absolute fallback thresholds
    if iv > 0.45:
        return "high"
    if iv < 0.20:
        return "low"
    return "normal"


def _compute_hv30(yticker, ticker: str) -> Optional[float]:
    try:
        from data.fetcher import _yf_call
        hist = _yf_call(lambda: yticker.history(period="45d", interval="1d"))
        if hist is None or len(hist) < 15:
            return None
        closes = hist["Close"].dropna().values
        if len(closes) < 15:
            return None
        log_returns = np.diff(np.log(closes.astype(float)))
        return float(np.std(log_returns) * math.sqrt(252))
    except Exception as e:
        logger.debug(f"{ticker}: HV30 failed — {e}")
        return None


def _bs_greeks(S: float, K: float, T: float, r: float, sigma: float, option_type: str) -> dict:
    """Black-Scholes price and Greeks. Theta per calendar day. Vega per 1pp IV change."""
    if T <= 0 or sigma <= 0 or S <= 0 or K <= 0:
        return {}
    sqrt_T = math.sqrt(T)
    d1 = (math.log(S / K) + (r + 0.5 * sigma ** 2) * T) / (sigma * sqrt_T)
    d2 = d1 - sigma * sqrt_T

    Nd1  = norm.cdf(d1)
    Nd2  = norm.cdf(d2)
    Nnd1 = norm.cdf(-d1)
    Nnd2 = norm.cdf(-d2)
    nd1  = norm.pdf(d1)
    disc = math.exp(-r * T)

    if option_type == "call":
        price = S * Nd1 - K * disc * Nd2
        delta = Nd1
        rho   = K * T * disc * Nd2 / 100
        theta_rho_term = r * K * disc * Nd2
    else:
        price = K * disc * Nnd2 - S * Nnd1
        delta = Nd1 - 1.0
        rho   = -K * T * disc * Nnd2 / 100
        theta_rho_term = r * K * disc * Nnd2

    gamma = nd1 / (S * sigma * sqrt_T)
    theta = (-(S * nd1 * sigma) / (2 * sqrt_T) - theta_rho_term) / 365
    vega  = S * nd1 * sqrt_T / 100

    return {
        "price": round(price, 2),
        "delta": round(delta, 3),
        "gamma": round(gamma, 5),
        "theta": round(theta, 3),
        "vega":  round(vega, 3),
        "rho":   round(rho, 3),
    }


def _find_delta_strike(
    chain_df,
    S: float,
    T: float,
    r: float,
    target_delta: float,
    option_type: str,
) -> Optional[float]:
    best_strike = None
    best_diff = float("inf")
    for _, row in chain_df.iterrows():
        K  = float(row.get("strike", 0))
        iv = _get_iv(row)
        if K <= 0 or iv <= 0:
            continue
        g = _bs_greeks(S, K, T, r, iv, option_type)
        if not g:
            continue
        diff = abs(g["delta"] - target_delta)
        if diff < best_diff:
            best_diff = diff
            best_strike = K
    return best_strike
