"""
bayesian.py — Sequential Bayesian updating for bull/base/bear scenario probabilities.

Updates prior scenario probabilities (bull / base / bear) by sequentially applying
likelihood ratios from observable evidence factors (revenue growth, gross margin,
forward P/E, analyst upside, earnings surprise).

Key assumptions:
  - Likelihoods are subjective priors calibrated by the analyst, not derived from
    historical data. They are reasonable starting points but should be recalibrated
    as you observe outcomes. Do not treat them as objectively correct.
  - Evidence factors are treated as conditionally independent for the sequential
    update. In reality, revenue growth and gross margin are correlated. The posterior
    will be overconfident when factors share common drivers. This limitation is noted
    in the output.
  - Likelihood values are clipped to [likelihood_clip_lo, likelihood_clip_hi] (default
    [0.05, 0.95]) to prevent degenerate posteriors that collapse to 0 or 1.
  - Any evidence factor where required data is None is skipped with a WARNING log.
  - Expected value: E[V] = P(bull) * PT_bull + P(base) * PT_base + P(bear) * PT_bear
    where PT_* are analyst price targets (or current_price multiples as fallback).
"""

import logging
from typing import Optional

logger = logging.getLogger(__name__)

# Fallback price target multipliers when analyst targets are unavailable
_PT_BULL_FALLBACK_MULT = 1.50
_PT_BASE_FALLBACK_MULT = 1.15
_PT_BEAR_FALLBACK_MULT = 0.75


def _clip(val: float, lo: float, hi: float) -> float:
    """Clamp val to [lo, hi]. Pure Python — no numpy dependency."""
    return max(lo, min(hi, val))


def compute_bayesian(stock, config: dict) -> dict:
    """
    Compute posterior scenario probabilities via sequential Bayesian updating.

    Parameters
    ----------
    stock : StockData
        Populated StockData object.
    config : dict
        Full config dict. Uses config['bayesian'] sub-section.

    Returns
    -------
    dict with keys:
        bull              float  posterior P(bull)
        base              float  posterior P(base)
        bear              float  posterior P(bear)
        expected_value    float  Bayesian E[V] = sum(P(scenario) * PT(scenario))
        ev_upside_pct     float  (E[V] / current_price - 1) * 100
        update_trace      list[dict]  full audit trail of updates
            each entry: {label, bull, base, bear, evidence_label, likelihood_bull,
                         likelihood_base, likelihood_bear}
        skipped_factors   list[str]  factors skipped due to missing data
        notes             list[str]
        pt_bull           float  price target used for bull scenario
        pt_base           float  price target used for base scenario
        pt_bear           float  price target used for bear scenario
    """
    cfg = config.get("bayesian", {})

    prior_cfg = cfg.get("prior", {})
    p_bull: float = float(prior_cfg.get("bull", 0.25))
    p_base: float = float(prior_cfg.get("base", 0.50))
    p_bear: float = float(prior_cfg.get("bear", 0.25))

    clip_cfg = cfg.get("likelihood_clip", [0.05, 0.95])
    clip_lo: float = float(clip_cfg[0])
    clip_hi: float = float(clip_cfg[1])

    evidence_factors_cfg: dict = cfg.get("evidence_factors", {})

    notes: list[str] = []
    skipped_factors: list[str] = []
    update_trace: list[dict] = []

    # Record prior state
    update_trace.append({
        "label": "prior",
        "bull": p_bull,
        "base": p_base,
        "bear": p_bear,
        "evidence_label": None,
        "likelihood_bull": None,
        "likelihood_base": None,
        "likelihood_bear": None,
    })

    # ── Sequential evidence updates ───────────────────────────────────────────
    # Order matters for sequential updates (though with normalization at each step,
    # the final posterior is order-invariant when factors are independent).
    factor_handlers = {
        "revenue_growth": _eval_revenue_growth,
        "gross_margin": _eval_gross_margin,
        "forward_pe": _eval_forward_pe,
        "analyst_upside": _eval_analyst_upside,
        "earnings_surprise": _eval_earnings_surprise,
        "news_sentiment": _eval_news_sentiment,
    }

    for factor_name, handler in factor_handlers.items():
        factor_cfg = evidence_factors_cfg.get(factor_name, {})
        if not factor_cfg:
            logger.debug(f"{stock.ticker}: no config for factor '{factor_name}', skipping")
            skipped_factors.append(f"{factor_name} (no config)")
            continue

        evidence_label, lk = handler(stock, factor_cfg)
        if evidence_label is None or lk is None:
            skipped_factors.append(factor_name)
            logger.warning(f"{stock.ticker}: '{factor_name}' skipped — data unavailable")
            continue

        # Clip likelihoods to [clip_lo, clip_hi] to prevent degenerate posteriors.
        # A likelihood of 0.0 would permanently zero out a scenario with no way back.
        lk_bull = _clip(float(lk[0]), clip_lo, clip_hi)
        lk_base = _clip(float(lk[1]), clip_lo, clip_hi)
        lk_bear = _clip(float(lk[2]), clip_lo, clip_hi)

        # Bayesian update: unnormalized posterior = prior * likelihood
        p_bull_new = p_bull * lk_bull
        p_base_new = p_base * lk_base
        p_bear_new = p_bear * lk_bear

        # Normalize so probabilities sum to 1
        total = p_bull_new + p_base_new + p_bear_new
        if total <= 0:
            logger.warning(
                f"{stock.ticker}: '{factor_name}' update produced zero-sum posterior. Skipping."
            )
            skipped_factors.append(f"{factor_name} (zero posterior)")
            continue

        p_bull = p_bull_new / total
        p_base = p_base_new / total
        p_bear = p_bear_new / total

        update_trace.append({
            "label": f"after_{factor_name}",
            "bull": round(p_bull, 6),
            "base": round(p_base, 6),
            "bear": round(p_bear, 6),
            "evidence_label": evidence_label,
            "likelihood_bull": lk_bull,
            "likelihood_base": lk_base,
            "likelihood_bear": lk_bear,
        })

        logger.debug(
            f"{stock.ticker}: [{factor_name}={evidence_label}] "
            f"bull={p_bull:.3f} base={p_base:.3f} bear={p_bear:.3f}"
        )

    if len(update_trace) == 1:
        notes.append("All evidence factors were skipped — posterior equals prior.")
        logger.warning(f"{stock.ticker}: no evidence factors applied; posterior = prior")

    if skipped_factors:
        notes.append(
            "Note: conditionally independent factor assumption. "
            "Revenue growth and margin share common drivers — posterior may be overconfident."
        )

    # ── Price targets and E[V] ────────────────────────────────────────────────
    current: float = float(stock.current_price) if stock.current_price else 0.0

    pt_bull: float
    pt_base: float
    pt_bear: float

    if stock.analyst_target_high:
        pt_bull = float(stock.analyst_target_high)
    else:
        pt_bull = current * _PT_BULL_FALLBACK_MULT
        notes.append(f"PT(bull) not available; using {_PT_BULL_FALLBACK_MULT}x current price")

    if stock.analyst_target_mean:
        pt_base = float(stock.analyst_target_mean)
    else:
        pt_base = current * _PT_BASE_FALLBACK_MULT
        notes.append(f"PT(base) not available; using {_PT_BASE_FALLBACK_MULT}x current price")

    if stock.analyst_target_low:
        pt_bear = float(stock.analyst_target_low)
    else:
        pt_bear = current * _PT_BEAR_FALLBACK_MULT
        notes.append(f"PT(bear) not available; using {_PT_BEAR_FALLBACK_MULT}x current price")

    expected_value: float = p_bull * pt_bull + p_base * pt_base + p_bear * pt_bear
    ev_upside_pct: float = (expected_value / current - 1.0) * 100.0 if current > 0 else float("nan")

    logger.info(
        f"{stock.ticker}: Bayesian E[V]=${expected_value:.2f} ({ev_upside_pct:+.1f}%) "
        f"bull={p_bull:.3f} base={p_base:.3f} bear={p_bear:.3f}"
    )

    return {
        "bull": p_bull,
        "base": p_base,
        "bear": p_bear,
        "expected_value": expected_value,
        "ev_upside_pct": ev_upside_pct,
        "update_trace": update_trace,
        "skipped_factors": skipped_factors,
        "notes": notes,
        "pt_bull": pt_bull,
        "pt_base": pt_base,
        "pt_bear": pt_bear,
    }


# ── Evidence factor evaluators ────────────────────────────────────────────────
# Each returns (evidence_label: str, likelihoods: tuple[float, float, float])
# or (None, None) if the required data is absent.
# likelihoods ordering: (P(evidence | bull), P(evidence | base), P(evidence | bear))

def _eval_revenue_growth(stock, factor_cfg: dict):
    """
    Classify trailing revenue growth rate and return likelihoods.

    Uses most recent vs prior-year revenue (1-year growth rate).
    Categories: high (>20%), moderate (5-20%), low (0-5%), decline (<0%)
    Returns (label, likelihoods_tuple) or (None, None) if data unavailable.
    """
    rev = stock.revenue
    if not rev or len(rev) < 2:
        return None, None

    r0, r1 = rev[0], rev[1]
    if r1 is None or r1 <= 0 or r0 is None:
        return None, None

    growth_rate = float(r0) / float(r1) - 1.0
    thresholds = factor_cfg.get("thresholds", [0.20, 0.05, 0.0])
    lk_map = factor_cfg.get("likelihoods", {})

    if growth_rate >= thresholds[0]:
        label, key = f"high ({growth_rate:.1%})", "high"
    elif growth_rate >= thresholds[1]:
        label, key = f"moderate ({growth_rate:.1%})", "moderate"
    elif growth_rate >= thresholds[2]:
        label, key = f"low ({growth_rate:.1%})", "low"
    else:
        label, key = f"decline ({growth_rate:.1%})", "decline"

    lk = lk_map.get(key)
    if not lk or len(lk) < 3:
        return None, None
    return label, tuple(lk)


def _eval_gross_margin(stock, factor_cfg: dict):
    """
    Classify latest gross margin and return likelihoods.

    Categories: high (>60%), moderate (40-60%), low (<40%)
    Returns (label, likelihoods_tuple) or (None, None) if data unavailable.
    """
    margin = stock.latest_gross_margin
    if margin is None:
        return None, None

    thresholds = factor_cfg.get("thresholds", [0.60, 0.40])
    lk_map = factor_cfg.get("likelihoods", {})

    if margin >= thresholds[0]:
        label, key = f"high ({margin:.1%})", "high"
    elif margin >= thresholds[1]:
        label, key = f"moderate ({margin:.1%})", "moderate"
    else:
        label, key = f"low ({margin:.1%})", "low"

    lk = lk_map.get(key)
    if not lk or len(lk) < 3:
        return None, None
    return label, tuple(lk)


def _eval_forward_pe(stock, factor_cfg: dict):
    """
    Classify forward P/E ratio and return likelihoods.

    Categories: attractive (<22x), fair (22-35x), elevated (35-60x), extreme (>60x)
    Returns (label, likelihoods_tuple) or (None, None) if data unavailable.
    """
    fpe = stock.forward_pe
    if fpe is None:
        return None, None

    thresholds = factor_cfg.get("thresholds", [22, 35, 60])
    lk_map = factor_cfg.get("likelihoods", {})

    if fpe < thresholds[0]:
        label, key = f"attractive ({fpe:.1f}x)", "attractive"
    elif fpe < thresholds[1]:
        label, key = f"fair ({fpe:.1f}x)", "fair"
    elif fpe < thresholds[2]:
        label, key = f"elevated ({fpe:.1f}x)", "elevated"
    else:
        label, key = f"extreme ({fpe:.1f}x)", "extreme"

    lk = lk_map.get(key)
    if not lk or len(lk) < 3:
        return None, None
    return label, tuple(lk)


def _eval_analyst_upside(stock, factor_cfg: dict):
    """
    Classify analyst consensus upside (mean target / current price - 1).

    Categories: strong (>30%), moderate (10-30%), slim (0-10%), downside (<0%)
    Returns (label, likelihoods_tuple) or (None, None) if data unavailable.
    """
    if stock.analyst_target_mean is None or stock.current_price is None:
        return None, None
    if stock.current_price <= 0:
        return None, None

    upside = float(stock.analyst_target_mean) / float(stock.current_price) - 1.0
    thresholds = factor_cfg.get("thresholds", [0.30, 0.10, 0.0])
    lk_map = factor_cfg.get("likelihoods", {})

    if upside >= thresholds[0]:
        label, key = f"strong ({upside:.1%})", "strong"
    elif upside >= thresholds[1]:
        label, key = f"moderate ({upside:.1%})", "moderate"
    elif upside >= thresholds[2]:
        label, key = f"slim ({upside:.1%})", "slim"
    else:
        label, key = f"downside ({upside:.1%})", "downside"

    lk = lk_map.get(key)
    if not lk or len(lk) < 3:
        return None, None
    return label, tuple(lk)


def _eval_news_sentiment(stock, factor_cfg: dict):
    """
    Classify VADER news headline sentiment and return likelihoods.

    Uses stock.news_sentiment (mean VADER compound over recent headlines).
    Categories: bullish (>0.15), neutral (-0.15 to 0.15), bearish (<-0.15)

    Intentionally weak likelihoods — news sentiment is noisy. Its job is to
    nudge probabilities, not dominate the posterior.
    """
    sentiment = stock.news_sentiment
    if sentiment is None:
        return None, None

    thresholds = factor_cfg.get("thresholds", [0.15, -0.15])
    lk_map = factor_cfg.get("likelihoods", {})

    if sentiment >= thresholds[0]:
        label, key = f"bullish ({sentiment:.2f})", "bullish"
    elif sentiment >= thresholds[1]:
        label, key = f"neutral ({sentiment:.2f})", "neutral"
    else:
        label, key = f"bearish ({sentiment:.2f})", "bearish"

    lk = lk_map.get(key)
    if not lk or len(lk) < 3:
        return None, None
    return label, tuple(lk)


def _eval_earnings_surprise(stock, factor_cfg: dict):
    """
    Classify most recent EPS surprise percentage.

    eps_surprise_pct is stored as a decimal (0.05 = 5% beat).
    Categories: beat (>10%), inline (0-10%), miss (<0%)
    Returns (label, likelihoods_tuple) or (None, None) if data unavailable.
    """
    surprise = stock.eps_surprise_pct
    if surprise is None:
        return None, None

    thresholds = factor_cfg.get("thresholds", [0.10, 0.0])
    lk_map = factor_cfg.get("likelihoods", {})

    if surprise >= thresholds[0]:
        label, key = f"beat ({surprise:.1%})", "beat"
    elif surprise >= thresholds[1]:
        label, key = f"inline ({surprise:.1%})", "inline"
    else:
        label, key = f"miss ({surprise:.1%})", "miss"

    lk = lk_map.get(key)
    if not lk or len(lk) < 3:
        return None, None
    return label, tuple(lk)
