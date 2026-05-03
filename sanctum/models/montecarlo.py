"""
montecarlo.py — Monte Carlo simulation for equity intrinsic value distribution.

Simulates the distribution of DCF-implied prices by drawing random shocks to:
  - Revenue growth rate (log-normal draws; revenue cannot go negative)
  - FCF margin (normal draws; clamped to avoid absurd values)
  - Terminal growth rate (normal draws; clamped to [0, WACC - epsilon])

Key assumptions:
  - Shocks to revenue growth, FCF margin, and terminal growth are drawn
    independently in each simulation. Cross-factor correlations (e.g.,
    revenue growth and margin are positively correlated in practice) are
    NOT modeled. This causes the distribution to be slightly overconfident.
    Flag this to users.
  - Revenue growth uses log-normal shocks so simulated revenue stays positive.
  - Antithetic variates (if enabled) halve variance with no extra cost:
    for each random draw z, we also run -z. This doubles effective samples
    but only half the number of full sim paths are drawn.
  - Seed is fixed: same config + same data = same output, always.
  - analyze mode uses n_simulations; screen mode uses n_simulations_screen
    for speed during bulk screening.
"""

import logging
import math
import numpy as np
from typing import Optional

logger = logging.getLogger(__name__)

# Epsilon to ensure WACC > terminal_g in all simulated paths
_TERMINAL_G_WACC_BUFFER = 0.005


def run_montecarlo(
    stock,
    wacc_result: dict,
    dcf_result: dict,
    config: dict,
    mode: str = "analyze",
) -> dict:
    """
    Run Monte Carlo simulation over DCF inputs.

    Parameters
    ----------
    stock : StockData
        Populated StockData object.
    wacc_result : dict
        Output of compute_wacc().
    dcf_result : dict
        Output of compute_dcf(). Provides base-case values for simulation.
    config : dict
        Full config dict. Uses config['montecarlo'] sub-section.
    mode : str
        'analyze' uses n_simulations; 'screen' uses n_simulations_screen.

    Returns
    -------
    dict with keys:
        percentiles        dict {P5, P10, P25, P50, P75, P90, P95}
        mean               float
        std                float
        p_above_current    float  probability simulated price > current_price
        p_above_target     float  probability > analyst_target_mean (or NaN)
        n_sims             int
        implied_prices     np.ndarray  (full distribution, for downstream use)
        notes              list[str]
    """
    cfg = config.get("montecarlo", {})

    n_sims_analyze: int = int(cfg.get("n_simulations", 10000))
    n_sims_screen: int = int(cfg.get("n_simulations_screen", 1000))
    seed: int = int(cfg.get("seed", 42))
    revenue_vol: float = float(cfg.get("revenue_vol", 0.08))
    growth_vol: float = float(cfg.get("growth_vol", 0.06))
    margin_vol: float = float(cfg.get("margin_vol", 0.03))
    terminal_growth_vol: float = float(cfg.get("terminal_growth_vol", 0.005))
    antithetic: bool = bool(cfg.get("antithetic_variates", True))

    n_sims = n_sims_analyze if mode == "analyze" else n_sims_screen
    notes: list[str] = []

    if antithetic and n_sims % 2 != 0:
        n_sims += 1  # ensure even count for antithetic pairs

    # Pull base-case values from DCF result
    base_growth_rates: list[float] = dcf_result["blended_growth_rates"]
    base_fcf_margin: float = dcf_result["fcf_margin"]
    base_terminal_g: float = dcf_result["terminal_growth_rate"]
    base_revenue: float = dcf_result["base_revenue"]
    net_debt: float = dcf_result["net_debt"]
    shares: float = dcf_result["shares_outstanding"]
    wacc: float = dcf_result["wacc"]
    n_years: int = len(base_growth_rates)
    current_price: float = stock.current_price or float("nan")

    notes.append(f"n_sims={n_sims}, mode={mode}, antithetic={antithetic}, seed={seed}")
    notes.append("Cross-factor correlations not modeled — distribution is slightly overconfident.")

    # ── Random number generation ───────────────────────────────────────────────
    # Derive a per-stock seed from the global seed + ticker hash so each stock's
    # MC paths are independent. Same ticker + same global seed = identical output.
    import hashlib
    ticker_hash = int(hashlib.md5(stock.ticker.encode()).hexdigest()[:8], 16)
    per_stock_seed = (seed ^ ticker_hash) % (2 ** 31)
    rng = np.random.default_rng(per_stock_seed)

    # Number of half-paths needed (antithetic doubles them)
    half = n_sims // 2 if antithetic else n_sims

    # Draw standard normal variates for each shock source
    # Shape: (half, n_years) for per-year growth shocks; (half,) for scalar shocks
    z_growth = rng.standard_normal((half, n_years))   # per-year growth rate shocks
    z_margin = rng.standard_normal(half)               # terminal FCF margin shock
    z_terminal = rng.standard_normal(half)             # terminal growth rate shock
    z_revenue = rng.standard_normal(half)              # initial revenue level shock

    if antithetic:
        # Stack z and -z to produce full set of antithetic pairs
        z_growth = np.vstack([z_growth, -z_growth])
        z_margin = np.concatenate([z_margin, -z_margin])
        z_terminal = np.concatenate([z_terminal, -z_terminal])
        z_revenue = np.concatenate([z_revenue, -z_revenue])

    # ── Per-simulation DCF ────────────────────────────────────────────────────
    implied_prices = _vectorized_dcf(
        base_growth_rates=np.array(base_growth_rates, dtype=np.float64),
        base_fcf_margin=base_fcf_margin,
        base_terminal_g=base_terminal_g,
        base_revenue=base_revenue,
        net_debt=net_debt,
        shares=shares,
        wacc=wacc,
        z_growth=z_growth,
        z_margin=z_margin,
        z_terminal=z_terminal,
        z_revenue=z_revenue,
        growth_vol=growth_vol,
        margin_vol=margin_vol,
        terminal_growth_vol=terminal_growth_vol,
        revenue_vol=revenue_vol,
    )

    # Remove NaN/Inf that can arise from edge-case divisions
    valid_mask = np.isfinite(implied_prices)
    n_invalid = int((~valid_mask).sum())
    if n_invalid > 0:
        notes.append(f"{n_invalid} simulations produced non-finite prices and were excluded.")
        logger.warning(f"{stock.ticker}: {n_invalid} MC simulations yielded non-finite prices (excluded)")
    implied_prices = implied_prices[valid_mask]

    if len(implied_prices) == 0:
        raise ValueError(f"{stock.ticker}: all MC simulations produced non-finite prices. Check inputs.")

    # ── Output statistics ─────────────────────────────────────────────────────
    pct_levels = [5, 10, 25, 50, 75, 90, 95]
    pct_values = np.percentile(implied_prices, pct_levels)
    percentiles = {f"P{p}": float(v) for p, v in zip(pct_levels, pct_values)}

    mean_price = float(np.mean(implied_prices))
    std_price = float(np.std(implied_prices))

    p_above_current = float(np.mean(implied_prices > current_price)) if np.isfinite(current_price) else float("nan")

    analyst_target = stock.analyst_target_mean
    p_above_target = (
        float(np.mean(implied_prices > analyst_target))
        if analyst_target is not None
        else float("nan")
    )

    logger.info(
        f"{stock.ticker}: MC P50=${percentiles['P50']:.2f} "
        f"P(above current)={p_above_current:.1%} "
        f"mean=${mean_price:.2f} std=${std_price:.2f}"
    )

    return {
        "percentiles": percentiles,
        "mean": mean_price,
        "std": std_price,
        "p_above_current": p_above_current,
        "p_above_target": p_above_target,
        "n_sims": len(implied_prices),
        "implied_prices": implied_prices,
        "notes": notes,
    }


def _vectorized_dcf(
    base_growth_rates: np.ndarray,
    base_fcf_margin: float,
    base_terminal_g: float,
    base_revenue: float,
    net_debt: float,
    shares: float,
    wacc: float,
    z_growth: np.ndarray,
    z_margin: np.ndarray,
    z_terminal: np.ndarray,
    z_revenue: np.ndarray,
    growth_vol: float,
    margin_vol: float,
    terminal_growth_vol: float,
    revenue_vol: float,
) -> np.ndarray:
    """
    Vectorized DCF computation across all simulations.

    revenue_vol: log-normal shock to the starting revenue level (permanent level shift).
    growth_vol:  log-normal shock to each year's growth rate (trajectory uncertainty).
    margin_vol:  normal shock to FCF margin, clamped to [1%, 60%].
    terminal_growth_vol: normal shock to terminal growth rate, clamped to [0, wacc - buffer].

    Parameters
    ----------
    z_growth  : ndarray shape (n_sims, n_years)
    z_margin  : ndarray shape (n_sims,)
    z_terminal: ndarray shape (n_sims,)
    z_revenue : ndarray shape (n_sims,)

    Returns
    -------
    ndarray of implied equity prices, shape (n_sims,)
    """
    n_years = len(base_growth_rates)

    # ── Simulate starting revenue level (log-normal shock) ────────────────────
    # Mean-corrected: E[exp(σZ - σ²/2)] = 1, so shocks are centered on the base case.
    # Without the -σ²/2 correction, E[exp(σZ)] = exp(σ²/2) > 1, biasing MC prices upward.
    sim_base_revenues = base_revenue * np.exp(
        revenue_vol * z_revenue - 0.5 * revenue_vol ** 2
    )  # (n_sims,)

    # ── Simulate growth rates (log-normal shocks per year) ────────────────────
    base_factors = 1.0 + base_growth_rates  # (n_years,)
    lognorm_shocks = np.exp(
        growth_vol * z_growth - 0.5 * growth_vol ** 2
    )  # (n_sims, n_years) — mean-corrected
    sim_factors = base_factors[np.newaxis, :] * lognorm_shocks  # (n_sims, n_years)

    # ── Simulate FCF margin (normal shock, clamped) ────────────────────────────
    sim_margins = base_fcf_margin + margin_vol * z_margin   # (n_sims,)
    # Floor is anchored to the base case (allows ~15pp downside, never below -5%)
    # so MC and DCF stay consistent when base_fcf_margin is low.
    margin_floor = max(base_fcf_margin - 0.15, -0.05)
    sim_margins = np.clip(sim_margins, margin_floor, 0.60)

    # ── Simulate terminal growth (normal shock, clamped) ──────────────────────
    sim_terminal_g = base_terminal_g + terminal_growth_vol * z_terminal  # (n_sims,)
    max_terminal_g = wacc - _TERMINAL_G_WACC_BUFFER
    sim_terminal_g = np.clip(sim_terminal_g, 0.0, max_terminal_g)

    # ── Project revenues and FCFs ─────────────────────────────────────────────
    cum_revenue_factors = np.cumprod(sim_factors, axis=1)           # (n_sims, n_years)
    revenues = sim_base_revenues[:, np.newaxis] * cum_revenue_factors  # (n_sims, n_years)

    # FCF = revenue * margin (margin is same for all years in this sim)
    fcfs = revenues * sim_margins[:, np.newaxis]  # (n_sims, n_years)

    # ── Discount factors ──────────────────────────────────────────────────────
    years = np.arange(1, n_years + 1, dtype=np.float64)
    discount_factors = (1.0 + wacc) ** years  # (n_years,)

    # PV of each year's FCF
    pv_fcfs = fcfs / discount_factors[np.newaxis, :]  # (n_sims, n_years)
    pv_sum = pv_fcfs.sum(axis=1)  # (n_sims,)

    # ── Terminal value ────────────────────────────────────────────────────────
    terminal_fcf = fcfs[:, -1]  # last year's simulated FCF, shape (n_sims,)
    # PhD Math Review Finding 1.1 consistency: Use terminal_fcf / (wacc - terminal_g)
    # to avoid double-counting growth already applied in the final projection year.
    tv = terminal_fcf / (wacc - sim_terminal_g)  # (n_sims,)
    pv_tv = tv / (1.0 + wacc) ** n_years  # (n_sims,)

    # ── Equity price ──────────────────────────────────────────────────────────
    ev = pv_sum + pv_tv  # (n_sims,)
    equity = ev - net_debt  # (n_sims,)
    prices = equity / shares  # (n_sims,)

    return prices
