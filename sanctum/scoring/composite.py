"""
composite.py — Composite scoring and orchestration layer.

Orchestrates WACC → DCF → Monte Carlo → Bayesian → Sensitivity for each stock
and assembles a final scored output dict.

Scoring methodology:
  - Each component is normalized to [0, 100]:
      upside_pct → score_component:  0% upside = 50, +50% = 100, -50% = 0
      Formula: component = clamp(50 + upside_pct, 0, 100)
  - margin_trend: OLS slope of gross_margin_history vs time (most-recent-last).
    Normalized: 0 slope = 50, +2pp/year = 100, -2pp/year = 0.
  - earnings_momentum: blend of eps_surprise_pct (60%) and eps_revision_trend (40%).
    Each normalized independently then blended.
  - Composite score = weighted average of components per config['scoring']['weights'].

IMPORTANT: The composite score is a triage heuristic, not a model. It is a tool
to decide what to read first. The underlying model outputs are the actual analysis.
Never present the score as the conclusion.
"""

import logging
import math
from typing import Optional

from models.wacc import compute_wacc
from models.dcf import compute_dcf
from models.montecarlo import run_montecarlo
from models.bayesian import compute_bayesian
from models.sensitivity import compute_sensitivity

logger = logging.getLogger(__name__)

# Normalization constants for upside → score
_UPSIDE_MIDPOINT = 0.0      # 0% upside maps to score = 50
_UPSIDE_FULL_SCORE = 50.0   # +50% upside maps to score = 100
_UPSIDE_ZERO_SCORE = -50.0  # -50% upside maps to score = 0

# Margin trend normalization: slope in decimal/year → score
_MARGIN_SLOPE_FULL = 0.02   # +2pp/year → 100
_MARGIN_SLOPE_ZERO = -0.02  # -2pp/year → 0

# Earnings momentum normalization
_SURPRISE_FULL = 0.20   # +20% surprise → 100
_SURPRISE_ZERO = -0.20  # -20% surprise → 0
_REVISION_FULL = 0.10   # +10% upward revision → 100
_REVISION_ZERO = -0.10  # -10% downward revision → 0

# Blend weights for earnings_momentum component
_SURPRISE_WEIGHT = 0.60
_REVISION_WEIGHT = 0.40

# Sentiment normalization: VADER compound ±0.3 spans the full [0, 100] range
_SENTIMENT_SCALE = 0.3


def _upside_to_score(upside_pct: Optional[float]) -> float:
    """
    Convert upside percentage to [0, 100] score component.

    0% upside → 50. +50% → 100. -50% → 0. Linear interpolation, clamped.

    Parameters
    ----------
    upside_pct : float or None
        Upside as a percentage (e.g. 25.0 for 25%). None → returns 50.0 (neutral).
    """
    if upside_pct is None or (upside_pct != upside_pct):  # None or NaN
        return 50.0
    # Normalize over ±100% range: 0% → 50, +100% → 100, -100% → 0.
    # Prior range (±50%) caused hard saturation on any stock with >50% model upside,
    # destroying rank-ordering signal in deep-value or high-growth screens.
    raw = 50.0 + upside_pct / 2.0
    return float(max(0.0, min(100.0, raw)))


def _slope_to_score(slope: float, full_val: float, zero_val: float) -> float:
    """
    Normalize a slope value to [0, 100] given the full-score and zero-score thresholds.
    Linear interpolation, clamped.
    """
    span = full_val - zero_val
    if span == 0:
        return 50.0
    raw = (slope - zero_val) / span * 100.0
    return float(max(0.0, min(100.0, raw)))


def _compute_margin_trend(stock) -> float:
    """
    Compute OLS slope of gross_margin_history over time.

    gross_margin_history is most-recent-first; we reverse to get chronological order.
    Returns the slope (units: margin change per year). Returns 0.0 if < 2 data points.
    """
    margins = stock.gross_margin_history
    if not margins or len(margins) < 2:
        return 0.0

    # Reverse to chronological (oldest first)
    y = list(reversed(margins))
    n = len(y)
    x = list(range(n))

    # OLS slope: sum((xi - xbar)(yi - ybar)) / sum((xi - xbar)^2)
    xbar = sum(x) / n
    ybar = sum(y) / n

    num = sum((x[i] - xbar) * (y[i] - ybar) for i in range(n))
    den = sum((x[i] - xbar) ** 2 for i in range(n))

    if den == 0:
        return 0.0
    return num / den


def _sentiment_to_score(sentiment: Optional[float]) -> float:
    """
    Map VADER compound sentiment [-1, 1] to [0, 100].

    0.0 → 50 (neutral). ±_SENTIMENT_SCALE saturates to 100/0.
    Compressed range vs upside normalization because sentiment is noisy.
    """
    if sentiment is None or (sentiment != sentiment):
        return 50.0
    raw = 50.0 + (sentiment / _SENTIMENT_SCALE) * 50.0
    return float(max(0.0, min(100.0, raw)))


def _compute_earnings_momentum(stock) -> float:
    """
    Blend eps_surprise_pct and eps_revision_trend into a single [0, 100] score.

    Normalizes each component independently then applies blend weights.
    Returns 50.0 (neutral) if both inputs are missing.
    """
    has_surprise = stock.eps_surprise_pct is not None
    has_revision = stock.eps_revision_trend is not None

    if not has_surprise and not has_revision:
        return 50.0

    if has_surprise and has_revision:
        s_score = _slope_to_score(stock.eps_surprise_pct, _SURPRISE_FULL, _SURPRISE_ZERO)
        r_score = _slope_to_score(stock.eps_revision_trend, _REVISION_FULL, _REVISION_ZERO)
        return _SURPRISE_WEIGHT * s_score + _REVISION_WEIGHT * r_score
    elif has_surprise:
        return _slope_to_score(stock.eps_surprise_pct, _SURPRISE_FULL, _SURPRISE_ZERO)
    else:
        return _slope_to_score(stock.eps_revision_trend, _REVISION_FULL, _REVISION_ZERO)


class CompositeScorer:
    """
    Orchestrates the full model pipeline and produces composite scores.

    Parameters
    ----------
    config : dict
        Full application config dict.
    mode : str
        'analyze' for full simulation count; 'screen' for fast bulk screening.
    """

    def __init__(self, config: dict, mode: str = "analyze"):
        self.config = config
        self.mode = mode
        self._weights = config.get("scoring", {}).get("weights", {})

    def score_one(self, stock) -> dict:
        """
        Run the full model pipeline for a single stock and return the result dict.

        Gracefully handles model failures by setting affected fields to NaN
        and recording the error in the 'errors' key. Never raises.

        Returns
        -------
        dict with all keys from the output contract plus 'errors' list.
        """
        result = {
            "ticker": stock.ticker,
            "company_name": stock.company_name,
            "sector": stock.sector,
            "industry": stock.industry,
            "current_price": stock.current_price,
            "market_cap": stock.market_cap,
            "beta": stock.beta,
            "ma_50": stock.ma_50,
            "ma_200": stock.ma_200,
            "week_52_high": stock.week_52_high,
            "week_52_low": stock.week_52_low,
            "trailing_pe": stock.trailing_pe,
            "forward_pe": stock.forward_pe,
            "dividend_yield": stock.dividend_yield,
            "short_ratio": stock.short_ratio,
            "avg_daily_volume": stock.avg_daily_volume,
            "errors": [],
        }

        # ── WACC ──────────────────────────────────────────────────────────────
        wacc_result = None
        try:
            wacc_result = compute_wacc(stock, self.config)
            result["wacc"] = wacc_result["wacc"]
            result["wacc_detail"] = wacc_result
        except Exception as e:
            logger.error(f"{stock.ticker}: WACC failed — {e}")
            result["errors"].append(f"wacc: {e}")
            result["wacc"] = float("nan")
            result["wacc_detail"] = {}

        # ── DCF ───────────────────────────────────────────────────────────────
        dcf_result = None
        if wacc_result is not None:
            try:
                dcf_result = compute_dcf(stock, wacc_result, self.config)
                result["dcf_implied_price"] = dcf_result["implied_price"]
                result["dcf_upside_pct"] = dcf_result["dcf_upside_pct"]
                result["dcf_detail"] = dcf_result
            except Exception as e:
                logger.error(f"{stock.ticker}: DCF failed — {e}")
                result["errors"].append(f"dcf: {e}")
                result["dcf_implied_price"] = float("nan")
                result["dcf_upside_pct"] = float("nan")
                result["dcf_detail"] = {}
        else:
            result["dcf_implied_price"] = float("nan")
            result["dcf_upside_pct"] = float("nan")
            result["dcf_detail"] = {}

        # ── Monte Carlo ───────────────────────────────────────────────────────
        mc_result = None
        if wacc_result is not None and dcf_result is not None:
            try:
                mc_result = run_montecarlo(stock, wacc_result, dcf_result, self.config, mode=self.mode)
                result["mc_p50"] = mc_result["percentiles"]["P50"]
                mc_upside_pct = (
                    (mc_result["percentiles"]["P50"] / stock.current_price - 1.0) * 100.0
                    if stock.current_price and stock.current_price > 0
                    else float("nan")
                )
                result["mc_upside_pct"] = mc_upside_pct
                result["mc_detail"] = mc_result
            except Exception as e:
                logger.error(f"{stock.ticker}: Monte Carlo failed — {e}")
                result["errors"].append(f"montecarlo: {e}")
                result["mc_p50"] = float("nan")
                result["mc_upside_pct"] = float("nan")
                result["mc_detail"] = {}
        else:
            result["mc_p50"] = float("nan")
            result["mc_upside_pct"] = float("nan")
            result["mc_detail"] = {}

        # ── Bayesian ──────────────────────────────────────────────────────────
        bayes_result = None
        try:
            bayes_result = compute_bayesian(stock, self.config)
            result["expected_value"] = bayes_result["expected_value"]
            result["bayesian_bull_prob"] = bayes_result["bull"]
            result["bayesian_base_prob"] = bayes_result["base"]
            result["bayesian_bear_prob"] = bayes_result["bear"]
            result["bayesian_upside_pct"] = bayes_result["ev_upside_pct"]
            result["bayesian_trace"] = bayes_result["update_trace"]
        except Exception as e:
            logger.error(f"{stock.ticker}: Bayesian failed — {e}")
            result["errors"].append(f"bayesian: {e}")
            result["expected_value"] = float("nan")
            result["bayesian_bull_prob"] = float("nan")
            result["bayesian_base_prob"] = float("nan")
            result["bayesian_bear_prob"] = float("nan")
            result["bayesian_upside_pct"] = float("nan")
            result["bayesian_trace"] = []

        # ── Sensitivity ───────────────────────────────────────────────────────
        if wacc_result is not None:
            try:
                sens_result = compute_sensitivity(stock, wacc_result, self.config)
                result["sensitivity_detail"] = sens_result
            except Exception as e:
                logger.error(f"{stock.ticker}: Sensitivity failed — {e}")
                result["errors"].append(f"sensitivity: {e}")
                result["sensitivity_detail"] = {}
        else:
            result["sensitivity_detail"] = {}

        # ── Component scores ──────────────────────────────────────────────────
        margin_slope = _compute_margin_trend(stock)
        margin_trend_score = _slope_to_score(margin_slope, _MARGIN_SLOPE_FULL, _MARGIN_SLOPE_ZERO)
        earnings_momentum_score = _compute_earnings_momentum(stock)

        sentiment_score = _sentiment_to_score(getattr(stock, "news_sentiment", None))

        components = {
            "bayesian_upside": _upside_to_score(result.get("bayesian_upside_pct")),
            "mc_upside": _upside_to_score(result.get("mc_upside_pct")),
            "dcf_upside": _upside_to_score(result.get("dcf_upside_pct")),
            "margin_trend": margin_trend_score,
            "earnings_momentum": earnings_momentum_score,
            "sentiment_score": sentiment_score,
        }
        result["news_sentiment"] = getattr(stock, "news_sentiment", None)
        result["score_components"] = components
        result["margin_slope"] = margin_slope

        # ── Composite score (weighted average of components) ──────────────────
        default_weights = {
            "bayesian_upside": 0.27,
            "mc_upside": 0.22,
            "dcf_upside": 0.18,
            "margin_trend": 0.09,
            "earnings_momentum": 0.14,
            "sentiment_score": 0.10,
        }
        weights = {k: float(self._weights.get(k, default_weights.get(k, 0.0))) for k in components}

        total_weight = sum(weights.values())
        if total_weight > 0:
            composite = sum(components[k] * weights[k] for k in components) / total_weight
        else:
            composite = 50.0

        result["score"] = round(float(composite), 2)

        logger.info(
            f"{stock.ticker}: composite score={result['score']:.1f} "
            f"(dcf={result['dcf_upside_pct']:+.1f}% "
            f"mc_p50=${result['mc_p50']:.2f} "
            f"ev=${result['expected_value']:.2f})"
        )

        # ── Catalyst score ────────────────────────────────────────────────────
        try:
            from models.catalyst import compute_catalyst_score
            catalyst = compute_catalyst_score(stock, self.config)
            result["catalyst_score"] = catalyst["catalyst_score"]
            result["catalyst_detail"] = catalyst
        except Exception as e:
            logger.warning(f"{stock.ticker}: catalyst score failed — {e}")
            result["catalyst_score"] = 50.0
            result["catalyst_detail"] = {}

        # Combined archetype using both scores
        result["trade_archetype"] = _combined_archetype(
            result["score"], result["catalyst_score"]
        )

        # ── Options analysis (analyze mode only — skip during bulk screen) ──────
        result["options_analysis"] = None
        if self.mode == "analyze":
            try:
                from models.options import analyze_options, suggest_strategy
                opts = analyze_options(stock, self.config)
                if opts:
                    opts["strategy"] = suggest_strategy(
                        opts,
                        result["score"],
                        result.get("bayesian_bull_prob", 0.25),
                        result.get("bayesian_bear_prob", 0.25),
                    )
                result["options_analysis"] = opts
            except Exception as e:
                logger.warning(f"{stock.ticker}: options analysis failed — {e}")

        return result

    def score_all(self, stocks: list) -> list:
        """
        Score a list of stocks and return results sorted by score descending.

        Stocks that fail entirely (e.g. no price data) are included with score=0
        and their errors logged.

        Parameters
        ----------
        stocks : list[StockData]

        Returns
        -------
        list of result dicts, sorted by 'score' descending.
        """
        results = []
        for stock in stocks:
            try:
                r = self.score_one(stock)
            except Exception as e:
                logger.error(f"{stock.ticker}: score_one raised unexpectedly — {e}")
                r = {
                    "ticker": stock.ticker,
                    "sector": getattr(stock, "sector", None),
                    "current_price": getattr(stock, "current_price", None),
                    "score": 0.0,
                    "errors": [f"fatal: {e}"],
                }
            results.append(r)

        results.sort(key=lambda x: x.get("score", 0.0), reverse=True)
        logger.info(f"score_all: scored {len(results)} stocks")
        return results


def _combined_archetype(fundamental: float, catalyst: float) -> str:
    """
    Classify the trade setup using both scores.

    Jane Street / PhD Fix: Identify 'CONVERGENCE' where deep fundamental mispricing 
    meets extreme catalyst velocity. This is the 30%-10x zone.
    """
    if fundamental >= 75 and catalyst >= 75:
        return "CONVERGENCE — extreme mispricing + velocity (10x Potential)"
    elif fundamental >= 65 and catalyst >= 65:
        return "STRONG BUY — value + momentum aligned"
    elif fundamental >= 65 and catalyst >= 50:
        return "GOOD ENTRY — fundamentals strong, catalyst building"
    elif fundamental >= 65 and catalyst < 45:
        return "VALUE HOLD — wait for catalyst before sizing up"
    elif fundamental >= 50 and catalyst >= 65:
        return "MOMENTUM SETUP — solid fundamentals, catalyst active"
    elif fundamental >= 50 and catalyst >= 50:
        return "NEUTRAL — no strong signal either way"
    elif fundamental < 50 and catalyst >= 70:
        return "MOMENTUM ONLY — no fundamental support; size small, tight stop"
    elif fundamental < 45 and catalyst < 45:
        return "AVOID — weak on both dimensions"
    else:
        return "WATCH — mixed signals"
