"""
dcf.py — Discounted Cash Flow model.

Computes intrinsic equity value via unlevered FCF → Enterprise Value → equity bridge.

Key assumptions:
  - Revenue growth is blended from: (a) 3-year historical CAGR, (b) sector medians,
    and (c) a 3% terminal growth floor.
  - FCF margin is averaged from last 5 years, with sector-specific floors/caps
    to prevent extreme outliers in sparse data cases.
  - Discounting uses WACC (Weighted Average Cost of Capital).
  - Terminal Value uses Gordon Growth (perpetuity) model.
"""

import logging
import math
from typing import Optional

logger = logging.getLogger(__name__)

# Sector-specific FCF margin medians (fallback when company data is missing or extreme)
_SECTOR_FCF_MARGIN_MEDIANS = {
    "Technology": 0.18,
    "Healthcare": 0.15,
    "Financial Services": 0.20,
    "Consumer Cyclical": 0.08,
    "Industrials": 0.09,
    "Communication Services": 0.14,
    "Consumer Defensive": 0.07,
    "Energy": 0.12,
    "Real Estate": 0.25,
    "Utilities": 0.10,
    "Basic Materials": 0.08,
    "default": 0.10,
}

# Sector-specific revenue growth medians (trailing 3Y CAGR)
SECTOR_GROWTH_MEDIANS = {
    "Technology": 0.12,
    "Healthcare": 0.08,
    "Financial Services": 0.06,
    "Consumer Cyclical": 0.07,
    "Industrials": 0.05,
    "Communication Services": 0.10,
    "Consumer Defensive": 0.04,
    "Energy": 0.03,
    "Real Estate": 0.05,
    "Utilities": 0.03,
    "Basic Materials": 0.04,
    "default": 0.05,
}


def compute_dcf(stock, wacc_result: dict, config: dict) -> dict:
# ... (rest of compute_dcf)
    """
    Compute intrinsic price for a single stock using 2-stage Unlevered FCF model.
    """
    cfg = config.get("dcf", {})
    # Jane Street / PhD Math Fix: Structural winners (Tech/Comm) deserve a longer 
    # projection period to capture their durable moats. Extend to 10y if not overridden.
    default_n = 10 if stock.sector in ["Technology", "Communication Services"] else 7
    n_years: int = int(cfg.get("projection_years", default_n))

    terminal_g: float = float(cfg.get("terminal_growth_rate", 0.03))
    growth_override: dict = cfg.get("growth_override", {}) or {}
    margin_override: dict = cfg.get("margin_override", {}) or {}

    wacc: float = float(wacc_result["wacc"])
    notes: list[str] = []

    if wacc <= terminal_g:
        raise ValueError(
            f"{stock.ticker}: WACC ({wacc:.4f}) <= terminal_growth_rate ({terminal_g:.4f}). "
            "Gordon Growth model undefined. Increase WACC or reduce terminal growth."
        )

    # ── Base revenue ──────────────────────────────────────────────────────────
    base_revenue = _get_base_revenue(stock)
    if base_revenue is None or base_revenue <= 0:
        raise ValueError(f"{stock.ticker}: no usable revenue data for DCF.")

    # ── Growth rates ──────────────────────────────────────────────────────────
    if stock.ticker in growth_override and growth_override[stock.ticker]:
        blended_rates = [float(g) for g in growth_override[stock.ticker]]
        # Pad or trim to n_years
        if len(blended_rates) < n_years:
            blended_rates += [terminal_g] * (n_years - len(blended_rates))
        blended_rates = blended_rates[:n_years]
        notes.append(f"growth_override applied for {stock.ticker}")
        logger.info(f"{stock.ticker}: using growth override: {blended_rates}")
    else:
        blended_rates = _build_growth_schedule(stock, n_years, terminal_g, config, notes)

    # ── FCF margin ────────────────────────────────────────────────────────────
    if stock.ticker in margin_override and margin_override[stock.ticker]:
        fcf_margin = float(margin_override[stock.ticker])
        notes.append(f"margin_override applied: {fcf_margin:.2%}")
        logger.info(f"{stock.ticker}: using margin override {fcf_margin:.2%}")
    else:
        fcf_margin = _estimate_fcf_margin(stock, notes)

    logger.debug(f"{stock.ticker}: FCF margin = {fcf_margin:.3f}")

    # ── Project FCFs ──────────────────────────────────────────────────────────
    projection_rows = []
    cumulative_revenue = base_revenue
    pv_sum = 0.0

    for i, g in enumerate(blended_rates):
        year = i + 1
        cumulative_revenue = cumulative_revenue * (1.0 + g)
        fcf = cumulative_revenue * fcf_margin
        discount_factor = (1.0 + wacc) ** year
        pv_fcf = fcf / discount_factor
        pv_sum += pv_fcf

        projection_rows.append({
            "year": year,
            "revenue": cumulative_revenue,
            "growth_rate": g,
            "fcf_margin": fcf_margin,
            "fcf": fcf,
            "discount_factor": discount_factor,
            "pv_fcf": pv_fcf,
        })

        logger.debug(
            f"{stock.ticker} Y{year}: rev=${cumulative_revenue/1e9:.2f}B "
            f"g={g:.2%} fcf=${fcf/1e9:.2f}B pv=${pv_fcf/1e9:.2f}B"
        )

    # ── Terminal value (Gordon Growth) ────────────────────────────────────────
    terminal_fcf = projection_rows[-1]["fcf"]
    # PhD Math Review Finding 1.1: Use terminal_fcf / (wacc - terminal_g)
    # to avoid double-counting growth already applied in year N.
    terminal_value = terminal_fcf / (wacc - terminal_g)
    pv_terminal_value = terminal_value / (1.0 + wacc) ** n_years

    logger.debug(
        f"{stock.ticker}: TV = {terminal_fcf/1e9:.2f}B / "
        f"({wacc:.4f} - {terminal_g:.4f}) = {terminal_value/1e9:.2f}B  "
        f"PV(TV) = {pv_terminal_value/1e9:.2f}B"
    )

    # ── Enterprise value ──────────────────────────────────────────────────────
    enterprise_value = pv_sum + pv_terminal_value
    tv_pct_of_ev = pv_terminal_value / enterprise_value if enterprise_value != 0 else float("nan")

    if tv_pct_of_ev > 0.85:
        notes.append(
            f"Terminal Value represents {tv_pct_of_ev:.0%} of EV. "
            "Valuation is highly sensitive to terminal growth and WACC assumptions."
        )
        logger.warning(
            f"{stock.ticker}: TV = {tv_pct_of_ev:.0%} of EV. "
            "Near-term FCF projection contributes little — terminal assumptions dominate."
        )

    # ── Equity bridge ─────────────────────────────────────────────────────────
    net_debt = _get_net_debt(stock)
    equity_value = enterprise_value - net_debt
    shares = _get_shares(stock)

    if shares is None or shares <= 0:
        raise ValueError(f"{stock.ticker}: missing shares data for DCF.")

    implied_price = equity_value / shares
    current_price = stock.current_price or 0.0
    upside_pct = (implied_price / current_price - 1.0) * 100.0 if current_price > 0 else 0.0

    return {
        "ticker": stock.ticker,
        "n_years": n_years,
        "base_revenue": base_revenue,
        "fcf_margin": fcf_margin,
        "terminal_g": terminal_g,
        "wacc": wacc,
        "enterprise_value": enterprise_value,
        "net_debt": net_debt,
        "equity_value": equity_value,
        "shares": shares,
        "shares_outstanding": shares, # Legacy key for tests
        "implied_price": implied_price,
        "dcf_upside_pct": upside_pct,
        "tv_pct_of_ev": tv_pct_of_ev,
        "terminal_value": terminal_value,
        "pv_terminal_value": pv_terminal_value,
        "pv_fcf_sum": pv_sum,
        "blended_growth_rates": blended_rates,
        "terminal_growth_rate": terminal_g, # Legacy key for tests
        "projection_rows": projection_rows,
        "notes": notes,
    }


def compute_implied_hurdle_rate(stock, config: dict) -> Optional[float]:
    """
    Reverse-engineer the DCF to find the WACC that justifies the current market price.
    Returns the 'Implied Hurdle Rate' (IRR).
    
    CIO Persona: "What is the rate of return the market is pricing in?"
    """
    current_price = stock.current_price
    if not current_price or current_price <= 0:
        return None

    # Binary search for IRR between 1% and 50%
    low = 0.01
    high = 0.50
    best_irr = None
    
    for _ in range(20): # 20 iterations for high precision
        mid = (low + high) / 2
        wacc_mock = {"wacc": mid}
        try:
            res = compute_dcf(stock, wacc_mock, config)
            implied = res["implied_price"]
            
            if abs(implied - current_price) / current_price < 0.001:
                best_irr = mid
                break
            
            # WACC is in denominator: Higher WACC -> Lower Price
            if implied > current_price:
                low = mid
            else:
                high = mid
        except Exception:
            break
            
    return best_irr


# ── Internal logic ───────────────────────────────────────────────────────────

def _get_base_revenue(stock) -> Optional[float]:
    """Return most recent annual revenue, or None if unavailable."""
    if stock.revenue and len(stock.revenue) > 0:
        val = stock.revenue[0]
        if val is not None:
            return float(val)
    return None


def _build_growth_schedule(stock, n: int, terminal_g: float, config: dict, notes: list) -> list[float]:
    """
    Build a list of n growth rates starting from historical CAGR and decaying
    geometrically to terminal_g.
    """
    # 1. Start with 3Y historical CAGR
    revs = stock.revenue[:4] # need 4 points for 3 periods
    if len(revs) >= 2:
        # yfinance revenue is [most_recent, ..., oldest]
        recent = float(revs[0])
        oldest = float(revs[-1])
        years = len(revs) - 1
        if oldest > 0 and recent > 0:
            cagr = (recent / oldest) ** (1 / years) - 1.0
        else:
            cagr = 0.05 # fallback
    else:
        cagr = 0.05 # fallback

    # 2. Blend with sector growth
    sector = getattr(stock, "sector", "default")
    sector_g = SECTOR_GROWTH_MEDIANS.get(sector, SECTOR_GROWTH_MEDIANS["default"])
    
    g_start = 0.5 * cagr + 0.5 * sector_g
    # Sanity clip: don't start below terminal growth or above 40% for the base model
    g_start = max(terminal_g, min(g_start, 0.40))
    
    return _geometric_decay(g_start, terminal_g, n)


def _geometric_decay(g_start: float, g_end: float, n: int) -> list[float]:
    """
    Decay growth rate from g_start to g_end over n steps.
    """
    if n <= 1:
        return [g_end]
    
    # Simple linear decay for now
    step = (g_start - g_end) / (n - 1)
    return [max(g_end, g_start - i * step) for i in range(n)]


def _estimate_fcf_margin(stock, notes: list) -> float:
    """
    Estimate FCF margin from historical data, with normalization for monopolies.
    """
    revs = stock.revenue
    fcfs = stock.fcf
    
    if not revs or not fcfs:
        # Fallback to sector median
        sector = getattr(stock, "sector", "default")
        margin = _SECTOR_FCF_MARGIN_MEDIANS.get(sector, _FCF_MARGIN_FALLBACK)
        notes.append(f"FCF margin: no data; using sector median {margin:.1%}")
        return margin

    pairs = []
    for fcf, rev in zip(fcfs[:5], revs[:5]):
        if rev and rev > 0 and fcf is not None:
            pairs.append(float(fcf) / float(rev))

    if len(pairs) >= 2:
        margin = sum(pairs) / len(pairs)
        
        # Senior SWE / CIO Fix: 'Growth Capex' Normalization.
        # Hardware monopolies (TSM, NVDA) spend billions on fabs/DC which crushes FCF.
        # But their Operating Cash Flow (NI proxy) is massive. If FCF margin is < 80% 
        # of NI margin, it indicates heavy growth investment.
        ni_list = stock.net_income or []
        rev_list = stock.revenue or []
        ni_pairs = [float(ni)/float(rev) for ni, rev in zip(ni_list[:3], rev_list[:3]) if rev and rev > 0]
        if ni_pairs:
            ni_margin = sum(ni_pairs) / len(ni_pairs)
            # If NI margin is significantly higher than FCF margin (Capex Gap)
            if ni_margin > margin * 1.25:
                normalized = (ni_margin * 0.7) + (margin * 0.3) # Heavy weight on earnings power
                notes.append(
                    f"Growth Capex Cycle detected: NI margin ({ni_margin:.1%}) >> FCF margin ({margin:.1%}). "
                    f"Normalizing to {normalized:.1%} to reflect maintenance-state cash power."
                )
                logger.info(f"{stock.ticker}: Capex Normalization applied ({margin:.1%} -> {normalized:.1%})")
                margin = normalized

    elif len(pairs) == 1:
        margin = 0.5 * pairs[0] + 0.5 * _FCF_MARGIN_FALLBACK
        notes.append(f"FCF margin: only 1 year of data; blended with fallback {_FCF_MARGIN_FALLBACK:.0%}")
        logger.warning(f"{stock.ticker}: sparse FCF data — blending 1-year margin with fallback")
    else:
        notes.append(f"FCF margin: no historical data; using fallback {_FCF_MARGIN_FALLBACK:.0%}")
        logger.warning(f"{stock.ticker}: no FCF/revenue history — using fallback margin {_FCF_MARGIN_FALLBACK:.0%}")
        return _FCF_MARGIN_FALLBACK

    if margin < 0:
        # Don't project permanent cash burn. Blend toward sector fallback to represent
        # a normalization assumption (capex cycle ending, turnaround, etc.).
        # 25% weight on historical (anchors to reality) + 75% sector fallback (recovery assumption).
        normalized = max(margin * 0.25 + _FCF_MARGIN_FALLBACK * 0.75, 0.0)
        notes.append(
            f"WARNING: Negative trailing FCF margin ({margin:.1%}). "
            f"Normalized to {normalized:.1%} for projection "
            f"(25% historical + 75% sector fallback). "
            "Set dcf.margin_override for a manual estimate."
        )
        logger.warning(
            f"{stock.ticker}: negative FCF margin {margin:.1%} — "
            f"normalized to {normalized:.1%} for projection"
        )
        return normalized

    # Elevated margins mean-revert, but structural winners (TSM, GOOG, NVDA, MSFT)
    # sustain 30-40%+ margins for decades. Cap at 45%; above that, blend 75% historical
    # with 25% sector median (not the 10% fallback) so high-ROIC tech companies are not
    # systematically undervalued. PhD Math Review fix: was 70/30 with flat 10% fallback.
    _FCF_MARGIN_CAP = 0.45
    if margin > _FCF_MARGIN_CAP:
        sector = getattr(stock, "sector", None) or "default"
        sector_median = _SECTOR_FCF_MARGIN_MEDIANS.get(sector, _SECTOR_FCF_MARGIN_MEDIANS["default"])
        normalized = margin * 0.75 + sector_median * 0.25
        notes.append(
            f"Elevated trailing FCF margin ({margin:.1%}) moderated to {normalized:.1%} "
            f"(75% historical + 25% sector median {sector_median:.0%}). "
            "Set dcf.margin_override to override."
        )
        logger.info(f"{stock.ticker}: elevated FCF margin {margin:.1%} — moderated to {normalized:.1%}")
        return normalized

    logger.debug(f"{stock.ticker}: FCF margin from {len(pairs)} years: {margin:.3f}")
    return margin


def _get_net_debt(stock) -> float:
    """Return net debt (total_debt - cash). Defaults to 0 if data missing."""
    if stock.net_debt is not None:
        return float(stock.net_debt)
    # Partial data fallback
    debt = float(stock.total_debt[0]) if stock.total_debt else 0.0
    cash = float(stock.cash[0]) if stock.cash else 0.0
    return debt - cash


def _get_shares(stock) -> Optional[float]:
    """
    Return share count in the same denomination as the market price.

    Prefer market_cap / current_price over shares_outstanding because
    yfinance returns ordinary shares for foreign ADRs (e.g. TSM returns
    25.9B ordinary shares while the ADR price is per 5-share ADR unit).
    market_cap and current_price are both already in the trading currency,
    so their ratio is always in the correct per-unit denomination.
    """
    if stock.market_cap and stock.market_cap > 0 and stock.current_price and stock.current_price > 0:
        return float(stock.market_cap) / float(stock.current_price)
    if stock.shares_outstanding and stock.shares_outstanding > 0:
        return float(stock.shares_outstanding)
    return None
