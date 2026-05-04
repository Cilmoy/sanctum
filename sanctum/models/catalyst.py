"""
catalyst.py — Short-to-medium term catalyst scoring (0-100).

Parallel to the fundamental DCF/Bayesian score. Answers a different question:
"Is this stock likely to move in the next 2-12 weeks and in which direction?"

The fundamental score tells you if the price is worth paying.
The catalyst score tells you if the timing is right.

Components and default weights:
  earnings_acceleration  30%  Beat streak, magnitude, acceleration, earnings proximity
  smart_money            25%  Insider open-market buying + ownership alignment
  analyst_revisions      20%  Net upgrades minus downgrades, trailing 30 days
  price_momentum         15%  Price vs MA50, position in 52W range
  short_interest         10%  Short % of float + short ratio as squeeze amplifier

All components return [0, 100]. 50 = neutral / no data.
>50 = positive signal, <50 = negative signal.

Trade archetype logic (combined with fundamental score):
  Fundamental >65 + Catalyst >65 → STRONG BUY   — value AND momentum aligned
  Fundamental >65 + Catalyst <45 → VALUE HOLD    — cheap, wait for catalyst
  Fundamental <50 + Catalyst >65 → MOMENTUM PLAY — growth premium; size small, tight stop
  Both <45                        → AVOID
"""

import logging
from typing import Optional

logger = logging.getLogger(__name__)


def compute_catalyst_score(stock, config: dict) -> dict:
    """
    Compute catalyst score and return full breakdown dict.

    Returns
    -------
    dict with keys:
        catalyst_score          float   0-100
        components              dict    per-component scores
        archetype               str     trade archetype label
        days_to_earnings        int | None
        next_earnings_date      str | None
        earnings_beat_streak    int | None
        insider_buys_60d        int | None
        analyst_net_upgrades_30d int | None
        notes                   list[str]
    """
    cfg = config.get("catalyst", {})
    # Jane Street / PhD Fix: Re-weight to prioritize 'Velocity' and 'Squeeze' 
    # potential. Earnings remain the anchor, but short interest is now a 
    # primary amplifier rather than a footnote.
    weights = cfg.get("weights", {
        "earnings_acceleration": 0.25,
        "smart_money":           0.15,
        "analyst_revisions":     0.15,
        "price_momentum":        0.25,
        "short_interest":        0.20,
    })

    notes: list[str] = []

    components = {
        "earnings_acceleration": _score_earnings(stock, notes),
        "smart_money":           _score_smart_money(stock, notes),
        "analyst_revisions":     _score_analyst_revisions(stock, notes),
        "price_momentum":        _score_momentum(stock, notes),
        "short_interest":        _score_short_interest(stock, notes),
    }

    # ── Alpha Pipeline Multipliers ───────────────────────────────────────────
    # 1. Insider Clustering (The 'Intel at $45' signal)
    # If multiple insiders are buying while the stock is near 52W lows, 
    # it indicates a massive fundamental mispricing.
    if stock.insider_buys_60d and stock.insider_buys_60d >= 2:
        price_pos = 0.5
        if stock.current_price and stock.week_52_high and stock.week_52_low:
            rng = stock.week_52_high - stock.week_52_low
            if rng > 0:
                price_pos = (stock.current_price - stock.week_52_low) / rng
        
        if price_pos < 0.25:
            components["smart_money"] = min(100.0, components["smart_money"] * 1.3)
            notes.append("ALPHA SIGNAL: Clustered insider buying near 52-week lows.")

    total_w = sum(float(weights.get(k, 0)) for k in components)
    if total_w > 0:
        score = sum(components[k] * float(weights.get(k, 0)) for k in components) / total_w
    else:
        score = 50.0

    # 2. Volatility Squeeze Multiplier
    # A squeeze often precedes a violent directional move.
    if getattr(stock, "is_squeeze", False):
        score = min(100.0, score + 12.0)
        notes.append(f"ALPHA SIGNAL: Volatility Squeeze detected (BBW Percentile: {getattr(stock, 'squeeze_percentile', 0):.1f}%).")

    return {
        "catalyst_score":           round(score, 1),
        "components":               {k: round(v, 1) for k, v in components.items()},
        "archetype":                _archetype(score, notes),
        "days_to_earnings":         stock.days_to_earnings,
        "next_earnings_date":       stock.next_earnings_date,
        "earnings_beat_streak":     stock.earnings_beat_streak,
        "earnings_beat_avg_pct":    stock.earnings_beat_avg_pct,
        "earnings_beat_accelerating": stock.earnings_beat_accelerating,
        "insider_buys_60d":         stock.insider_buys_60d,
        "insider_buy_value_60d":    stock.insider_buy_value_60d,
        "insider_own_pct":          stock.insider_own_pct,
        "analyst_net_upgrades_30d": stock.analyst_net_upgrades_30d,
        "short_pct_float":          stock.short_pct_float,
        "institutional_own_pct":    stock.institutional_own_pct,
        "is_squeeze":               getattr(stock, "is_squeeze", False),
        "squeeze_percentile":       getattr(stock, "squeeze_percentile", 0.0),
        "notes":                    notes,
    }


# ── Component scorers ─────────────────────────────────────────────────────────

def _score_earnings(stock, notes: list) -> float:
    """
    Earnings acceleration: beat streak, magnitude, trajectory, and timing.

    The highest-signal component for 2-12 week PEAD (post-earnings announcement drift).
    Accelerating beats within the pre-earnings window (15-45 DTE) are the ideal setup.
    """
    scores = []

    # Beat streak — consecutive quarters beating consensus
    if stock.earnings_beat_streak is not None:
        streak = stock.earnings_beat_streak
        if streak == 0:
            scores.append(35.0)
        elif streak == 1:
            scores.append(55.0)
        elif streak == 2:
            scores.append(68.0)
        elif streak == 3:
            scores.append(80.0)
        else:
            scores.append(90.0)
        if streak >= 3:
            notes.append(f"Beat streak: {streak} consecutive quarters above consensus.")

    # Average beat magnitude (decimal — e.g. 0.08 = 8% beat)
    if stock.earnings_beat_avg_pct is not None:
        avg = stock.earnings_beat_avg_pct
        if avg < -0.05:
            scores.append(12.0)
        elif avg < 0.0:
            scores.append(32.0)
        elif avg < 0.03:
            scores.append(52.0)
        elif avg < 0.08:
            scores.append(68.0)
        elif avg < 0.15:
            scores.append(82.0)
        else:
            scores.append(90.0)

    # Beat acceleration — last beat bigger than prior average
    if stock.earnings_beat_accelerating is not None:
        scores.append(72.0 if stock.earnings_beat_accelerating else 38.0)
        if stock.earnings_beat_accelerating:
            notes.append("Earnings beats are accelerating — momentum building.")

    # Earnings date proximity — the pre-earnings drift window
    if stock.days_to_earnings is not None:
        dte = stock.days_to_earnings
        if 15 <= dte <= 45:
            scores.append(78.0)
            notes.append(f"Earnings in {dte} days — prime pre-earnings positioning window.")
        elif 8 <= dte < 15:
            scores.append(65.0)
            notes.append(f"Earnings in {dte} days — imminent; high risk/reward.")
        elif 46 <= dte <= 75:
            scores.append(58.0)
        else:
            scores.append(42.0)

    return sum(scores) / len(scores) if scores else 50.0


def _score_smart_money(stock, notes: list) -> float:
    """
    Insider open-market buying + ownership alignment.

    Insiders buying in the open market (not exercising options, not automatic plans)
    is one of the most reliable signals for an undiscovered inflection. They buy
    for one reason: they think the stock is going up.
    """
    scores = []

    # Open-market insider purchases in last 60 days (unique buyers)
    if stock.insider_buys_60d is not None:
        buys = stock.insider_buys_60d
        if buys == 0:
            scores.append(44.0)   # no buying ≠ bearish, just neutral
        elif buys == 1:
            scores.append(66.0)
            notes.append("1 insider made open-market purchases in the last 60 days.")
        elif buys == 2:
            scores.append(78.0)
            notes.append("2 insiders bought in the open market in the last 60 days.")
        else:
            scores.append(90.0)
            notes.append(f"{buys} insiders bought in the open market in the last 60 days — strong signal.")

    # Total dollar value of insider purchases
    if stock.insider_buy_value_60d is not None:
        val = stock.insider_buy_value_60d
        if val >= 5_000_000:
            scores.append(90.0)
            notes.append(f"Insider purchase value: ${val/1e6:.1f}M — very significant.")
        elif val >= 1_000_000:
            scores.append(78.0)
        elif val >= 100_000:
            scores.append(65.0)
        elif val > 0:
            scores.append(58.0)
        else:
            scores.append(44.0)

    # Insider ownership — aligned interests (founder-led or heavy insider stake)
    if stock.insider_own_pct is not None:
        pct = stock.insider_own_pct
        if pct > 0.20:
            scores.append(80.0)
            notes.append(f"Insiders own {pct*100:.1f}% of the company — highly aligned.")
        elif pct > 0.10:
            scores.append(70.0)
        elif pct > 0.05:
            scores.append(60.0)
        else:
            scores.append(50.0)

    return sum(scores) / len(scores) if scores else 50.0


def _score_analyst_revisions(stock, notes: list) -> float:
    """
    Net analyst upgrades minus downgrades, trailing 30 days.

    When multiple analysts independently raise their view in a short window,
    it typically signals new fundamental information is flowing through.
    Among the most durable short-term alpha factors in academic literature.
    """
    if stock.analyst_net_upgrades_30d is None:
        return 50.0

    net = stock.analyst_net_upgrades_30d
    if net <= -3:
        notes.append(f"Analyst revisions: {net} net (strong negative consensus shift).")
        return 8.0
    elif net == -2:
        return 22.0
    elif net == -1:
        return 38.0
    elif net == 0:
        return 52.0
    elif net == 1:
        return 65.0
    elif net == 2:
        notes.append("2 net analyst upgrades in the last 30 days.")
        return 78.0
    else:
        notes.append(f"{net} net analyst upgrades in the last 30 days — strong revision momentum.")
        return 90.0


def _score_momentum(stock, notes: list) -> float:
    """
    Price momentum: position relative to MA50 and 52-week range.

    For 30%+ gainers, the stock is usually already in an uptrend when the
    fundamental inflection becomes clear. Buying above MA50 with a rising
    52W range position filters out falling knives.
    """
    scores = []
    price = stock.current_price

    # Price vs 50-day moving average
    if price and stock.ma_50 and stock.ma_50 > 0:
        ratio = price / stock.ma_50
        if ratio >= 1.08:
            scores.append(85.0)
        elif ratio >= 1.02:
            scores.append(70.0)
        elif ratio >= 0.98:
            scores.append(52.0)
        elif ratio >= 0.93:
            scores.append(35.0)
        else:
            scores.append(18.0)
            notes.append(f"Price is {(1-ratio)*100:.1f}% below 50-day MA — weak momentum.")

    # Position in 52-week range: (price - 52W low) / (52W high - 52W low)
    if price and stock.week_52_high and stock.week_52_low:
        rng = stock.week_52_high - stock.week_52_low
        if rng > 0:
            pos = (price - stock.week_52_low) / rng
            if pos >= 0.80:
                scores.append(88.0)
                notes.append("Trading near 52-week highs — strong price momentum.")
            elif pos >= 0.60:
                scores.append(70.0)
            elif pos >= 0.40:
                scores.append(52.0)
            elif pos >= 0.25:
                scores.append(35.0)
            else:
                scores.append(20.0)

    return sum(scores) / len(scores) if scores else 50.0


def _score_short_interest(stock, notes: list) -> float:
    """
    Short interest as a squeeze amplifier.

    High short interest alone is a bearish signal. But combined with a positive
    catalyst (high fundamental + catalyst scores), it amplifies the upside —
    shorts must cover, creating a forced-buying dynamic.
    Score represents squeeze potential rather than directional conviction.
    """
    scores = []

    if stock.short_pct_float is not None:
        pct = stock.short_pct_float
        if pct >= 0.25:
            scores.append(70.0)
            notes.append(f"Short interest: {pct*100:.1f}% of float — high squeeze potential.")
        elif pct >= 0.15:
            scores.append(62.0)
        elif pct >= 0.08:
            scores.append(54.0)
        elif pct >= 0.04:
            scores.append(48.0)
        else:
            scores.append(50.0)

    if stock.short_ratio is not None:
        ratio = stock.short_ratio
        if ratio >= 10.0:
            scores.append(68.0)
            notes.append(f"Short ratio: {ratio:.1f} days to cover — fuel for a squeeze.")
        elif ratio >= 5.0:
            scores.append(57.0)
        else:
            scores.append(50.0)

    return sum(scores) / len(scores) if scores else 50.0


def _archetype(catalyst_score: float, notes: list) -> str:
    """
    Trade archetype label based on catalyst score alone.
    Combined with fundamental score in the composite layer for full classification.
    """
    if catalyst_score >= 72:
        return "STRONG CATALYST"
    elif catalyst_score >= 60:
        return "CATALYST BUILDING"
    elif catalyst_score >= 45:
        return "NEUTRAL"
    elif catalyst_score >= 32:
        return "CATALYST FADING"
    else:
        return "WEAK CATALYST"
