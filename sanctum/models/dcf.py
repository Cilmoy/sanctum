"""
dcf.py — Discounted Cash Flow model.

Computes intrinsic equity value via unlevered FCF → Enterprise Value → equity bridge.

Key assumptions:
  - Revenue growth is blended from: (a) 3-year historical CAGR, (b) sector median,
    (c) analyst consensus proxy (analyst_target_mean implied growth, if available).
    Weights: 50% historical, 30% sector, 20% analyst proxy. Decay toward terminal
    growth using geometric interpolation over the projection period.
  - FCF margin: historical average FCF/revenue (last 3 years). Blended with a 10%
    sector fallback when fewer than 2 years of FCF data are available.
  - Terminal value via Gordon Growth: TV = FCF_N * (1 + g) / (WACC - g).
    WACC must exceed terminal growth rate — raises ValueError if violated.
  - Equity bridge: EV = PV(FCFs) + PV(TV); equity = EV - net_debt; price = equity / shares.
  - Negative FCF in early years is handled correctly — the model does not break on
    cash-burn companies; it simply produces a low or negative EV contribution for those years.

Terminal value typically dominates (60–90% of EV). tv_pct_of_ev is reported explicitly
as a transparency metric — treat any result where TV > 85% of EV with extra skepticism.
"""

import logging
import math
from typing import Optional

logger = logging.getLogger(__name__)

# Sector revenue growth medians (5-year forward, Damodaran / consensus estimates).
# Used as one input to the blended growth rate when sector data is available.
SECTOR_GROWTH_MEDIANS: dict[str, float] = {
    "Technology": 0.12,
    "Communication Services": 0.08,
    "Consumer Discretionary": 0.07,
    "Consumer Staples": 0.04,
    "Energy": 0.03,
    "Financials": 0.06,
    "Health Care": 0.08,
    "Industrials": 0.06,
    "Materials": 0.04,
    "Real Estate": 0.05,
    "Utilities": 0.03,
    "default": 0.06,
}

# Blending weights for growth rate estimation.
# [historical_cagr, sector_median, analyst_proxy]
_GROWTH_BLEND_WEIGHTS = (0.50, 0.30, 0.20)

# Fallback FCF margin when history is insufficient.
_FCF_MARGIN_FALLBACK = 0.10


def compute_dcf(stock, wacc_result: dict, config: dict) -> dict:
    """
    Compute DCF-implied equity price.

    Parameters
    ----------
    stock : StockData
        Populated StockData object.
    wacc_result : dict
        Output of compute_wacc(). Must contain 'wacc' key.
    config : dict
        Full config dict. Uses config['dcf'] sub-section.

    Returns
    -------
    dict with keys:
        projection_rows       list of dicts (year, revenue, fcf, pv_fcf, growth_rate, fcf_margin)
        terminal_value        float
        pv_terminal_value     float
        enterprise_value      float
        net_debt              float
        equity_value          float
        shares_outstanding    float
        implied_price         float
        dcf_upside_pct        float  (implied/current - 1) * 100
        tv_pct_of_ev          float  fraction (e.g. 0.72 = 72%)
        wacc                  float
        terminal_growth_rate  float
        base_revenue          float
        blended_growth_rates  list[float]
        fcf_margin            float
        notes                 list[str]
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
            f"WARNING: TV is {tv_pct_of_ev:.0%} of EV — intrinsic value is highly "
            "sensitive to terminal growth and WACC assumptions."
        )
        logger.warning(
            f"{stock.ticker}: TV = {tv_pct_of_ev:.0%} of EV. "
            "Near-term FCF projection contributes little — terminal assumptions dominate."
        )

    # ── Equity bridge ─────────────────────────────────────────────────────────
    net_debt = _get_net_debt(stock)
    equity_value_calc = enterprise_value - net_debt
    shares = _get_shares(stock)

    if shares is None or shares <= 0:
        raise ValueError(f"{stock.ticker}: shares_outstanding unavailable — cannot compute implied price.")

    implied_price = equity_value_calc / shares
    current_price = stock.current_price or float("nan")
    dcf_upside_pct = (implied_price / current_price - 1.0) * 100.0 if current_price else float("nan")

    logger.info(
        f"{stock.ticker}: DCF implied ${implied_price:.2f} vs current ${current_price:.2f} "
        f"({dcf_upside_pct:+.1f}%)  TV={tv_pct_of_ev:.0%} of EV"
    )

    return {
        "projection_rows": projection_rows,
        "terminal_value": terminal_value,
        "pv_terminal_value": pv_terminal_value,
        "pv_fcf_sum": pv_sum,
        "enterprise_value": enterprise_value,
        "net_debt": net_debt,
        "equity_value": equity_value_calc,
        "shares_outstanding": shares,
        "implied_price": implied_price,
        "dcf_upside_pct": dcf_upside_pct,
        "tv_pct_of_ev": tv_pct_of_ev,
        "wacc": wacc,
        "terminal_growth_rate": terminal_g,
        "base_revenue": base_revenue,
        "blended_growth_rates": blended_rates,
        "fcf_margin": fcf_margin,
        "notes": notes,
    }


# ── Growth schedule construction ──────────────────────────────────────────────

def _build_growth_schedule(
    stock, n_years: int, terminal_g: float, config: dict, notes: list[str]
) -> list[float]:
    """
    Build a per-year revenue growth schedule that decays toward terminal_g.

    Blend: 50% historical 3-year CAGR + 30% sector median + 20% analyst proxy.
    Decay is geometric interpolation from blended rate to terminal_g over n_years.

    Returns list of n_years floats.
    """
    hist_cagr = _compute_revenue_cagr(stock, years=3)
    sector_median = _get_sector_median(stock)
    analyst_proxy = _get_analyst_growth_proxy(stock)

    w_hist, w_sect, w_anal = _GROWTH_BLEND_WEIGHTS

    if hist_cagr is None and analyst_proxy is None:
        # Only sector available — full weight to sector
        blended_start = sector_median
        notes.append("growth blend: sector median only (no historical or analyst data)")
        logger.warning(f"{stock.ticker}: insufficient growth data; using sector median {sector_median:.2%}")
    elif hist_cagr is None:
        # Historical missing
        blended_start = (w_sect + w_hist) * sector_median + w_anal * analyst_proxy
        notes.append("growth blend: sector + analyst (no historical CAGR)")
        logger.warning(f"{stock.ticker}: no historical CAGR; blending sector + analyst")
    elif analyst_proxy is None:
        # Analyst missing — redistribute analyst weight to historical
        blended_start = (w_hist + w_anal) * hist_cagr + w_sect * sector_median
        notes.append("growth blend: historical + sector (no analyst proxy)")
    else:
        blended_start = w_hist * hist_cagr + w_sect * sector_median + w_anal * analyst_proxy

    # Clamp starting growth to reasonable range: [-20%, +80%]
    blended_start = max(-0.20, min(0.80, blended_start))

    logger.debug(
        f"{stock.ticker}: growth blend start={blended_start:.3f} "
        f"(hist={hist_cagr}, sector={sector_median:.3f}, analyst={analyst_proxy})"
    )

    # Geometric interpolation from blended_start → terminal_g over n_years steps.
    # At year 1: blended_start. At year n_years: terminal_g.
    # Uses log-space linear interpolation so the decay is smooth regardless of sign.
    schedule = _geometric_decay(blended_start, terminal_g, n_years)
    return schedule


def _geometric_decay(g_start: float, g_end: float, n: int) -> list[float]:
    """
    Smoothly interpolate growth from g_start to g_end over n periods.

    Uses log-linear interpolation on the growth factor (1+g) when both are
    positive, falls back to linear interpolation otherwise.

    Returns list of n floats.
    """
    if n == 1:
        return [g_start]

    # Work with (1+g) to avoid log(negative) issues when g_start or g_end < 0
    factor_start = 1.0 + g_start
    factor_end = 1.0 + g_end

    if factor_start > 0 and factor_end > 0:
        # Log-linear interpolation of the growth factor
        log_start = math.log(factor_start)
        log_end = math.log(factor_end)
        schedule = []
        for i in range(n):
            t = i / (n - 1)  # 0 → 1
            log_factor = log_start + t * (log_end - log_start)
            g = math.exp(log_factor) - 1.0
            schedule.append(g)
    else:
        # Linear fallback for edge cases
        schedule = [g_start + (g_end - g_start) * i / (n - 1) for i in range(n)]

    return schedule


def _compute_revenue_cagr(stock, years: int = 3) -> Optional[float]:
    """
    Compute revenue CAGR over up to `years` periods.

    Revenue list is most-recent-first, so revenue[0] is latest, revenue[N-1] is oldest.
    CAGR = (rev_recent / rev_old)^(1/periods) - 1.

    Returns None if insufficient data.
    """
    rev = stock.revenue
    if not rev or len(rev) < 2:
        return None

    n_periods = min(years, len(rev) - 1)
    rev_recent = rev[0]
    rev_old = rev[n_periods]

    if rev_old is None or rev_old <= 0 or rev_recent is None:
        return None

    try:
        cagr = (rev_recent / rev_old) ** (1.0 / n_periods) - 1.0
        return float(cagr)
    except (ValueError, ZeroDivisionError):
        return None


def _get_sector_median(stock) -> float:
    """Return sector median growth rate, defaulting to 'default' key."""
    sector = stock.sector or "default"
    return SECTOR_GROWTH_MEDIANS.get(sector, SECTOR_GROWTH_MEDIANS["default"])


def _get_analyst_growth_proxy(stock) -> Optional[float]:
    """
    Estimate implied forward growth from analyst mean target vs current price.

    This is a rough proxy: (target/price - 1) compressed to a 1-year growth
    equivalent. Treat as directional signal, not a precise estimate.
    Returns None if data unavailable.
    """
    if stock.analyst_target_mean and stock.current_price and stock.current_price > 0:
        raw_upside = stock.analyst_target_mean / stock.current_price - 1.0
        # Compress: analyst target is ~12-month; scale to be comparable to revenue growth
        # by applying a 0.5 discount (price upside != revenue growth, this is approximate)
        proxy = raw_upside * 0.5
        return float(proxy)
    return None


# ── Financial helpers ─────────────────────────────────────────────────────────

def _get_base_revenue(stock) -> Optional[float]:
    """Return most recent annual revenue."""
    if stock.revenue and stock.revenue[0] is not None:
        return float(stock.revenue[0])
    return None


def _estimate_fcf_margin(stock, notes: list[str]) -> float:
    """
    Estimate FCF/revenue margin from historical data.

    Uses average of last 3 years. Falls back to _FCF_MARGIN_FALLBACK (10%)
    when fewer than 2 data points are available.

    Negative margins (capex cycles, turnarounds) are normalized toward the
    sector fallback rather than projected forward, which would produce nonsensical
    negative enterprise values. Use dcf.margin_override for a manual estimate.
    """
    fcf_list = stock.fcf or []
    rev_list = stock.revenue or []

    pairs = []
    for fcf, rev in zip(fcf_list[:3], rev_list[:3]):
        if rev and rev > 0 and fcf is not None:
            pairs.append(float(fcf) / float(rev))

    if len(pairs) >= 2:
        margin = sum(pairs) / len(pairs)
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

    # Elevated margins mean-revert, but top-tier leaders (TSM, GOOG, NVDA) 
    # sustain 30-40%+ margins for decades. The 25% cap was too restrictive.
    # We now moderate only above 45% and use a 70/30 historical/fallback blend.
    _FCF_MARGIN_CAP = 0.45
    if margin > _FCF_MARGIN_CAP:
        normalized = margin * 0.7 + _FCF_MARGIN_FALLBACK * 0.3
        notes.append(
            f"Extremely high trailing FCF margin ({margin:.1%}) moderated to {normalized:.1%} "
            f"(70% historical + 30% sector fallback — conservative normalization). "
            "Set dcf.margin_override to use a different assumption."
        )
        logger.info(f"{stock.ticker}: extreme FCF margin {margin:.1%} — moderated to {normalized:.1%}")
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
