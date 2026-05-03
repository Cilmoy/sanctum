"""
wacc.py — Weighted Average Cost of Capital computation.

Computes WACC via CAPM for cost of equity and derived cost of debt from
financial statements. Capital structure weights are market-value based.

Key assumptions:
  - Cost of equity: CAPM + optional small-cap premium (Duff & Phelps).
  - Beta from yfinance; noisy and non-stationary — treat output with appropriate
    skepticism, especially for short-history or recently-restructured firms.
  - Cost of debt: interest_expense / average(total_debt[-2:]) from income
    statement and balance sheet. Falls back to config default with WARNING when
    data is unavailable or debt is zero/negative.
  - Tax shield: pre-tax Kd × (1 - marginal_tax_rate).
  - Debt market value approximated as book value (no market quotes).
  - Small-cap premium applied when market_cap < small_cap_threshold_B.
"""

import logging
from typing import Optional

logger = logging.getLogger(__name__)


def compute_wacc(stock, config: dict) -> dict:
    """
    Compute WACC for a single stock.

    Parameters
    ----------
    stock : StockData
        Populated StockData object. All fields may be None/empty.
    config : dict
        Full config dict. Uses config['wacc'] sub-section.

    Returns
    -------
    dict with keys:
        rf, beta, erp, scp, ke, kd_pretax, kd_after_tax,
        we, wd, wacc, kd_source, notes
    All rate fields are floats (e.g. 0.10 = 10%).
    """
    cfg = config.get("wacc", {})

    rf: float = float(cfg.get("risk_free_rate", 0.043))
    erp: float = float(cfg.get("equity_risk_premium", 0.055))
    scp_rate: float = float(cfg.get("small_cap_premium", 0.025))
    scp_threshold_b: float = float(cfg.get("small_cap_threshold_B", 5))
    kd_fallback: float = float(cfg.get("cost_of_debt_fallback", 0.055))
    tax_rate: float = float(cfg.get("marginal_tax_rate", 0.21))

    notes: list[str] = []

    # ── Beta ──────────────────────────────────────────────────────────────────
    beta: float
    if stock.beta is not None:
        beta = float(stock.beta)
        if beta < 0:
            notes.append(f"negative beta ({beta:.3f}) retained — valid for counter-cyclical assets")
            logger.info(f"{stock.ticker}: negative beta {beta:.3f} retained in CAPM")
        logger.debug(f"{stock.ticker}: beta = {beta:.3f}")
    else:
        beta = 1.0
        notes.append("beta unavailable; defaulted to 1.0")
        logger.warning(f"{stock.ticker}: beta unavailable, defaulting to 1.0")

    # ── Small-cap premium ─────────────────────────────────────────────────────
    scp: float = 0.0
    if stock.market_cap is not None:
        market_cap_b = stock.market_cap / 1e9
        if market_cap_b < scp_threshold_b:
            scp = scp_rate
            logger.debug(
                f"{stock.ticker}: market_cap ${market_cap_b:.2f}B < "
                f"${scp_threshold_b}B threshold; applying SCP {scp:.1%}"
            )

    # ── Cost of equity (CAPM + SCP) ───────────────────────────────────────────
    ke: float = rf + beta * erp + scp
    logger.debug(f"{stock.ticker}: Ke = {rf:.3f} + {beta:.3f}×{erp:.3f} + {scp:.3f} = {ke:.4f}")

    # ── Cost of debt ──────────────────────────────────────────────────────────
    kd_pretax: float
    kd_source: str

    interest = _get_interest_expense(stock)
    avg_debt = _get_average_debt(stock)

    if interest is not None and avg_debt is not None and avg_debt > 0:
        kd_pretax = abs(interest) / avg_debt
        # Sanity check: Kd should be between 1% and 25% for most corporates.
        if not (0.01 <= kd_pretax <= 0.25):
            logger.warning(
                f"{stock.ticker}: derived Kd {kd_pretax:.2%} outside [1%, 25%]; "
                f"falling back to config default {kd_fallback:.2%}"
            )
            kd_pretax = kd_fallback
            kd_source = "fallback (derived out of bounds)"
            notes.append(f"Kd out-of-bounds derived value; using fallback {kd_fallback:.2%}")
        else:
            kd_source = "derived (interest_expense / avg_debt)"
            logger.debug(
                f"{stock.ticker}: Kd = |{interest:.0f}| / {avg_debt:.0f} = {kd_pretax:.4f}"
            )
    else:
        kd_pretax = kd_fallback
        kd_source = f"fallback config ({kd_fallback:.2%})"
        notes.append("cost of debt unavailable from statements; using config fallback")
        logger.warning(
            f"{stock.ticker}: cannot derive Kd from statements "
            f"(interest={interest}, avg_debt={avg_debt}); "
            f"using fallback {kd_fallback:.2%}"
        )

    kd_after_tax: float = kd_pretax * (1.0 - tax_rate)

    # ── Capital structure weights (market-value based) ────────────────────────
    equity_value: float = _get_equity_value(stock)
    debt_value: float = _get_debt_value(stock)
    total_capital: float = equity_value + debt_value

    if total_capital <= 0:
        # Pathological: treat as all-equity
        we, wd = 1.0, 0.0
        notes.append("total capital <= 0; treating as all-equity")
        logger.warning(f"{stock.ticker}: total_capital <= 0, defaulting to all-equity structure")
    else:
        we = equity_value / total_capital
        wd = debt_value / total_capital

    logger.debug(
        f"{stock.ticker}: wE={we:.3f}, wD={wd:.3f} "
        f"(equity=${equity_value/1e9:.2f}B, debt=${debt_value/1e9:.2f}B)"
    )

    # ── WACC ──────────────────────────────────────────────────────────────────
    wacc: float = we * ke + wd * kd_after_tax
    logger.info(
        f"{stock.ticker}: WACC = {we:.3f}×{ke:.4f} + {wd:.3f}×{kd_after_tax:.4f} = {wacc:.4f}"
    )

    return {
        "rf": rf,
        "beta": beta,
        "erp": erp,
        "scp": scp,
        "ke": ke,
        "kd_pretax": kd_pretax,
        "kd_after_tax": kd_after_tax,
        "tax_rate": tax_rate,
        "we": we,
        "wd": wd,
        "equity_value": equity_value,
        "debt_value": debt_value,
        "wacc": wacc,
        "kd_source": kd_source,
        "notes": notes,
    }


# ── Helpers ───────────────────────────────────────────────────────────────────

def _get_interest_expense(stock) -> Optional[float]:
    """Return most recent annual interest expense, or None if unavailable."""
    if stock.interest_expense and len(stock.interest_expense) > 0:
        val = stock.interest_expense[0]
        if val is not None:
            return float(val)
    return None


def _get_average_debt(stock) -> Optional[float]:
    """Return average of last 2 years' total debt, or None if unavailable."""
    if not stock.total_debt:
        return None
    valid = [float(d) for d in stock.total_debt[:2] if d is not None and d > 0]
    if not valid:
        return None
    return sum(valid) / len(valid)


def _get_equity_value(stock) -> float:
    """Return market cap in dollars. Falls back to 0.0 if unavailable."""
    if stock.market_cap is not None and stock.market_cap > 0:
        return float(stock.market_cap)
    # Last-resort approximation: price × shares
    if stock.current_price and stock.shares_outstanding:
        approx = float(stock.current_price) * float(stock.shares_outstanding)
        if approx > 0:
            return approx
    return 0.0


def _get_debt_value(stock) -> float:
    """Return most recent total debt (book value proxy for market value)."""
    if stock.total_debt and stock.total_debt[0] is not None:
        val = float(stock.total_debt[0])
        return max(val, 0.0)
    return 0.0
