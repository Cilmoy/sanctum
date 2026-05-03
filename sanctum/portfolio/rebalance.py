"""
rebalance.py — Suggested trades given new scores and existing positions.
"""

import logging

logger = logging.getLogger(__name__)

TRIM_THRESHOLD = 45    # score below this: consider trimming
ADD_THRESHOLD = 70     # score above this: consider adding


def suggest_rebalance(
    holdings: dict[str, float],
    scored: list[dict],
    config: dict,
) -> list[dict]:
    """
    Generate simple rebalancing suggestions based on conviction scores.

    Returns list of {action, ticker, reason} dicts.
    """
    constraints = config.get("portfolio", {})
    max_positions = constraints.get("max_positions", 15)

    scored_map = {r["ticker"]: r for r in scored}
    suggestions = []

    for ticker in holdings:
        r = scored_map.get(ticker)
        if r is None:
            continue
        score = r.get("score", 0)
        upside = r.get("dcf_upside_pct", 0)

        if score < TRIM_THRESHOLD:
            suggestions.append({
                "action": "TRIM",
                "ticker": ticker,
                "reason": f"Score {score:.0f} below threshold {TRIM_THRESHOLD}",
            })
        elif score >= ADD_THRESHOLD and upside > 15:
            suggestions.append({
                "action": "ADD",
                "ticker": ticker,
                "reason": f"Score {score:.0f}, upside {upside:+.1f}%",
            })

    # New ideas not in portfolio
    current_tickers = set(holdings.keys())
    shortlist = [
        r for r in scored
        if r["ticker"] not in current_tickers
        and r.get("score", 0) >= config.get("scoring", {}).get("shortlist_threshold", 60)
    ]
    shortlist.sort(key=lambda r: r.get("score", 0), reverse=True)

    remaining_slots = max_positions - len(holdings)
    for r in shortlist[:max(0, remaining_slots)]:
        suggestions.append({
            "action": "BUY",
            "ticker": r["ticker"],
            "reason": f"Score {r.get('score', 0):.0f}, upside {r.get('dcf_upside_pct', 0):+.1f}%",
        })

    return suggestions
