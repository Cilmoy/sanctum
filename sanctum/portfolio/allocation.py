"""
allocation.py — Position sizing and concentration checks.
"""

import logging
from typing import Optional

logger = logging.getLogger(__name__)


def check_constraints(
    holdings: dict[str, float],
    scored: list[dict],
    config: dict,
) -> list[str]:
    """
    Check portfolio constraints against config limits.

    Returns list of violation messages (empty = compliant).
    """
    constraints = config.get("portfolio", {})
    max_single = constraints.get("max_single_position_pct", 20) / 100
    max_sector = constraints.get("max_sector_pct", 40) / 100
    max_semi = constraints.get("max_semi_pct", 50) / 100

    scored_map = {r["ticker"]: r for r in scored}
    violations = []

    total_value = sum(
        holdings.get(t, 0) * scored_map.get(t, {}).get("current_price", 0)
        for t in holdings
    )

    if total_value == 0:
        return ["Cannot check constraints: no price data available."]

    sector_exposure: dict[str, float] = {}
    semi_exposure = 0.0

    for ticker, shares in holdings.items():
        r = scored_map.get(ticker, {})
        price = r.get("current_price", 0)
        value = shares * price
        weight = value / total_value

        if weight > max_single:
            violations.append(
                f"{ticker}: {weight:.1%} exceeds max single position {max_single:.1%}"
            )

        sector = r.get("sector", "Unknown")
        sector_exposure[sector] = sector_exposure.get(sector, 0) + weight

        industry = r.get("industry", "") or ""
        if "semiconductor" in industry.lower():
            semi_exposure += weight

    for sector, exp in sector_exposure.items():
        if exp > max_sector:
            violations.append(
                f"Sector '{sector}': {exp:.1%} exceeds max sector {max_sector:.1%}"
            )

    if semi_exposure > max_semi:
        violations.append(
            f"Semiconductor exposure {semi_exposure:.1%} exceeds hard cap {max_semi:.1%}"
        )

    return violations


def suggest_position_size(
    ticker: str,
    score: float,
    total_portfolio_value: float,
    config: dict,
) -> float:
    """
    Suggest a position size in dollars based on conviction score.

    Simple linear scaling: score 60 → min size, score 100 → max size.
    """
    constraints = config.get("portfolio", {})
    max_positions = constraints.get("max_positions", 15)
    max_single_pct = constraints.get("max_single_position_pct", 20) / 100

    equal_weight = 1.0 / max_positions
    # Scale from equal weight (at score=60) to max single position (at score=100)
    t = max(0, (score - 60) / 40)
    weight = equal_weight + t * (max_single_pct - equal_weight)
    weight = min(weight, max_single_pct)

    return weight * total_portfolio_value
