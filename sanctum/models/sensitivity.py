"""
sensitivity.py — Revenue sensitivity analysis for DCF-implied price.

Computes the partial derivative dV/d(revenue) by re-running the DCF at
±revenue_delta_pct% revenue shocks, holding all other assumptions constant.

Key assumptions:
  - Sensitivity is a partial derivative of the DCF model, not the Bayesian model.
    These operate at different layers and should not be conflated.
  - Revenue shock is applied as a permanent level shift to the base revenue used in
    DCF projection. All projected years inherit the shock multiplicatively.
  - dV_dr is reported as dollar change in implied price per 1% revenue change.
    Also reported as percentage of current price for interpretability.
  - Bear and bull cases are symmetric around the base case by construction.
    Material asymmetry (|bull_upside| >> |bear_downside|) can arise from margin
    floors/ceilings or non-linear TV effects — these are flagged if detected.
"""

import logging
from typing import Optional

from sanctum.models.dcf import compute_dcf

logger = logging.getLogger(__name__)

# Threshold for flagging asymmetry (ratio of upside/downside or vice versa)
_ASYMMETRY_THRESHOLD = 1.5


def compute_sensitivity(stock, wacc_result: dict, config: dict) -> dict:
    """
    Compute revenue sensitivity of DCF-implied price.

    Parameters
    ----------
    stock : StockData
        Populated StockData object.
    wacc_result : dict
        Output of compute_wacc().
    config : dict
        Full config dict. Uses config['sensitivity'] and config['dcf'].

    Returns
    -------
    dict with keys:
        base_price          float
        bull_price          float   (revenue + delta%)
        bear_price          float   (revenue - delta%)
        base_upside_pct     float
        bull_upside_pct     float
        bear_upside_pct     float
        delta_pct           float   the revenue shock applied (e.g. 5.0)
        dV_drev             float   $ change per 1% revenue change (avg of bull/bear)
        dV_drev_pct         float   % of current price per 1% revenue change
        dV_dwacc            float   $ change per 100bps change in WACC
        dV_dwacc_pct        float   % change in value per 100bps change in WACC (Duration)
        asymmetry_ratio     float   |bull_delta| / |bear_delta| (1.0 = symmetric)
        asymmetry_flag      bool    True if asymmetry_ratio > threshold
        notes               list[str]
    """
    sens_cfg = config.get("sensitivity", {})
    delta_pct: float = float(sens_cfg.get("revenue_delta_pct", 5))

    notes: list[str] = []
    current_price = stock.current_price or float("nan")

    # ── Base case ─────────────────────────────────────────────────────────────
    try:
        base_result = compute_dcf(stock, wacc_result, config)
        base_price = base_result["implied_price"]
    except Exception as e:
        raise ValueError(f"{stock.ticker}: base DCF failed in sensitivity — {e}") from e

    # ── Revenue Sensitivity: ±delta% revenue ─────────────────────────────────
    bull_price = _run_shocked_dcf(stock, wacc_result, config, delta_pct / 100.0, notes, "bull")
    bear_price = _run_shocked_dcf(stock, wacc_result, config, -delta_pct / 100.0, notes, "bear")

    # ── Derived Revenue metrics ───────────────────────────────────────────────
    base_upside_pct = (base_price / current_price - 1.0) * 100.0 if current_price else float("nan")
    bull_upside_pct = (bull_price / current_price - 1.0) * 100.0 if current_price else float("nan")
    bear_upside_pct = (bear_price / current_price - 1.0) * 100.0 if current_price else float("nan")

    bull_delta = bull_price - base_price
    bear_delta = base_price - bear_price  # positive = base > bear (miss hurts)

    # Average dollar change per 1% revenue change
    if delta_pct > 0:
        dV_drev = ((abs(bull_delta) + abs(bear_delta)) / 2.0) / delta_pct
    else:
        dV_drev = float("nan")

    dV_drev_pct = (dV_drev / current_price) * 100.0 if current_price else float("nan")

    # ── Interest Rate Duration: ±100bps WACC ─────────────────────────────────
    # Quant Personas: "High-growth tech is essentially a long-duration bond."
    # We measure sensitivity to the discount rate itself (WACC).
    try:
        wacc_base = wacc_result["wacc"]
        
        # Bull/Bear for rates is inverted for price: rate UP = price DOWN.
        # WACC + 1% (100bps)
        wacc_up = {"wacc": wacc_base + 0.01}
        price_wacc_up = compute_dcf(stock, wacc_up, config)["implied_price"]
        
        # WACC - 1% (100bps)
        wacc_down = {"wacc": max(0.001, wacc_base - 0.01)}
        price_wacc_down = compute_dcf(stock, wacc_down, config)["implied_price"]
        
        dV_dwacc = (price_wacc_down - price_wacc_up) / 2.0 # $ change per 100bps
        dV_dwacc_pct = (dV_dwacc / base_price) * 100.0 if base_price else float("nan")
    except Exception as e:
        logger.warning(f"{stock.ticker}: Duration calculation failed — {e}")
        dV_dwacc = float("nan")
        dV_dwacc_pct = float("nan")

    # Asymmetry: |upside from beat| vs |downside from miss|
    if abs(bear_delta) > 0:
        asymmetry_ratio = abs(bull_delta) / abs(bear_delta)
    else:
        asymmetry_ratio = float("nan")

    asymmetry_flag = (
        asymmetry_ratio > _ASYMMETRY_THRESHOLD or asymmetry_ratio < (1.0 / _ASYMMETRY_THRESHOLD)
        if asymmetry_ratio and not (asymmetry_ratio != asymmetry_ratio)  # nan check
        else False
    )

    if asymmetry_flag:
        msg = (
            f"Asymmetric sensitivity detected (ratio={asymmetry_ratio:.2f}): "
            "miss/beat are not equally sized. Check margin floor/ceiling effects or TV sensitivity."
        )
        notes.append(msg)
        logger.warning(f"{stock.ticker}: {msg}")

    logger.info(
        f"{stock.ticker}: sensitivity ±{delta_pct}% revenue → "
        f"bear=${bear_price:.2f} | base=${base_price:.2f} | bull=${bull_price:.2f}  "
        f"dV/drev=${dV_drev:.2f}/1%rev  Duration={dV_dwacc_pct:.1f}%"
    )

    return {
        "base_price": base_price,
        "bull_price": bull_price,
        "bear_price": bear_price,
        "base_upside_pct": base_upside_pct,
        "bull_upside_pct": bull_upside_pct,
        "bear_upside_pct": bear_upside_pct,
        "delta_pct": delta_pct,
        "dV_drev": dV_drev,
        "dV_drev_pct": dV_drev_pct,
        "dV_dr": dV_drev, # Legacy key for tests
        "dV_dr_pct": dV_drev_pct, # Legacy key for tests
        "dV_dwacc": dV_dwacc,
        "dV_dwacc_pct": dV_dwacc_pct,
        "asymmetry_ratio": asymmetry_ratio,
        "asymmetry_flag": asymmetry_flag,
        "notes": notes,
    }


# ── Internal helpers ──────────────────────────────────────────────────────────

class _RevenueShockedStock:
    """
    Thin wrapper around a StockData instance that overrides base_revenue
    for a DCF run. All other attributes are passed through unchanged.

    This avoids mutating the original StockData object.
    """
    def __init__(self, stock, revenue_multiplier: float):
        self._stock = stock
        self._multiplier = revenue_multiplier

    def __getattr__(self, name):
        # Scale both revenue AND fcf by the same multiplier.
        # This preserves the FCF/revenue margin ratio so the DCF projects the correct
        # FCF from the shocked revenue base. Scaling only revenue would cause the margin
        # estimator to see lower FCF/revenue, nearly cancelling the revenue effect and
        # severely understating dV/dr.
        if name == "revenue":
            rev = self._stock.revenue
            if rev:
                return [float(r) * self._multiplier for r in rev]
            return self._stock.revenue
        if name == "fcf":
            fcf = self._stock.fcf
            if fcf:
                return [float(f) * self._multiplier for f in fcf]
            return self._stock.fcf
        return getattr(self._stock, name)

    def __repr__(self):
        return f"_RevenueShockedStock({self._stock.ticker}, ×{self._multiplier:.3f})"


def _run_shocked_dcf(
    stock,
    wacc_result: dict,
    config: dict,
    revenue_shock: float,
    notes: list[str],
    label: str,
) -> float:
    """
    Run DCF with revenue multiplied by (1 + revenue_shock).

    Returns implied price. Falls back to NaN and logs on failure.
    """
    multiplier = 1.0 + revenue_shock
    shocked_stock = _RevenueShockedStock(stock, multiplier)
    try:
        result = compute_dcf(shocked_stock, wacc_result, config)
        return result["implied_price"]
    except Exception as e:
        notes.append(f"{label} DCF failed: {e}")
        logger.warning(f"{stock.ticker}: {label} sensitivity DCF failed — {e}")
        return float("nan")
