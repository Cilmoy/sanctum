"""
filters.py — Universe filtering for equity screener.

Applies configurable filters to a list of StockData objects to produce
a investable universe. Filters are applied sequentially; each step logs
how many stocks remain.

Filters applied (in order):
  1. min_market_cap_B:  exclude stocks with market_cap < threshold (in billions)
  2. min_avg_volume_M:  exclude stocks with avg_daily_volume < threshold (in millions)
  3. exclude_sectors:   exclude stocks whose sector is in the exclusion list
  4. include_sectors:   keep only stocks whose sector is in the inclusion list (empty = all)

Stocks with None values for a filtered field are excluded with a WARNING log.
"""

import logging
from typing import Optional

logger = logging.getLogger(__name__)


def apply_filters(stocks: list, config: dict) -> list:
    """
    Apply universe filters to a list of StockData objects.

    Parameters
    ----------
    stocks : list[StockData]
        Input universe. May contain objects with missing fields.
    config : dict
        Full config dict. Uses config['universe'] sub-section.

    Returns
    -------
    list[StockData]
        Filtered list. Order is preserved from input (modulo removals).
    """
    universe_cfg = config.get("universe", {})

    min_mktcap_b: Optional[float] = universe_cfg.get("min_market_cap_B")
    min_volume_m: Optional[float] = universe_cfg.get("min_avg_volume_M")
    exclude_sectors: list[str] = universe_cfg.get("exclude_sectors", []) or []
    include_sectors: list[str] = universe_cfg.get("include_sectors", []) or []

    # Normalize sector lists to lowercase for case-insensitive comparison
    exclude_sectors_lower = {s.lower() for s in exclude_sectors}
    include_sectors_lower = {s.lower() for s in include_sectors}

    n_start = len(stocks)
    logger.info(f"Filters: starting with {n_start} stocks")

    # ── 1. Market cap filter ──────────────────────────────────────────────────
    if min_mktcap_b is not None:
        min_mktcap_dollars = float(min_mktcap_b) * 1e9
        before = len(stocks)
        passed = []
        removed_none = 0
        removed_small = 0
        for s in stocks:
            if s.market_cap is None:
                removed_none += 1
                logger.warning(f"{s.ticker}: market_cap is None — excluded by market cap filter")
            elif s.market_cap < min_mktcap_dollars:
                removed_small += 1
                logger.debug(
                    f"{s.ticker}: market_cap ${s.market_cap/1e9:.2f}B < ${min_mktcap_b}B — excluded"
                )
            else:
                passed.append(s)
        stocks = passed
        after = len(stocks)
        logger.info(
            f"Market cap filter (>= ${min_mktcap_b}B): "
            f"{before} → {after} "
            f"({before - after} removed: {removed_small} too small, {removed_none} missing)"
        )

    # ── 2. Average daily volume filter ───────────────────────────────────────
    if min_volume_m is not None:
        min_volume_shares = float(min_volume_m) * 1e6
        before = len(stocks)
        passed = []
        removed_none = 0
        removed_low = 0
        for s in stocks:
            if s.avg_daily_volume is None:
                removed_none += 1
                logger.warning(f"{s.ticker}: avg_daily_volume is None — excluded by volume filter")
            elif s.avg_daily_volume < min_volume_shares:
                removed_low += 1
                logger.debug(
                    f"{s.ticker}: avg_daily_volume {s.avg_daily_volume/1e6:.2f}M < {min_volume_m}M — excluded"
                )
            else:
                passed.append(s)
        stocks = passed
        after = len(stocks)
        logger.info(
            f"Volume filter (>= {min_volume_m}M avg daily): "
            f"{before} → {after} "
            f"({before - after} removed: {removed_low} too illiquid, {removed_none} missing)"
        )

    # ── 3. Sector exclusion filter ────────────────────────────────────────────
    if exclude_sectors_lower:
        before = len(stocks)
        passed = []
        removed_sector = 0
        for s in stocks:
            sector = s.sector or ""
            if sector.lower() in exclude_sectors_lower:
                removed_sector += 1
                logger.debug(f"{s.ticker}: sector '{sector}' in exclude list — excluded")
            else:
                passed.append(s)
        stocks = passed
        after = len(stocks)
        logger.info(
            f"Sector exclusion filter {sorted(exclude_sectors)}: "
            f"{before} → {after} ({removed_sector} removed)"
        )
    else:
        logger.info("Sector exclusion filter: no sectors excluded")

    # ── 4. Sector inclusion filter ────────────────────────────────────────────
    if include_sectors_lower:
        before = len(stocks)
        passed = []
        removed_sector = 0
        removed_none = 0
        for s in stocks:
            sector = s.sector or ""
            if not sector:
                removed_none += 1
                logger.warning(f"{s.ticker}: sector is None — excluded by sector inclusion filter")
            elif sector.lower() not in include_sectors_lower:
                removed_sector += 1
                logger.debug(f"{s.ticker}: sector '{sector}' not in include list — excluded")
            else:
                passed.append(s)
        stocks = passed
        after = len(stocks)
        logger.info(
            f"Sector inclusion filter {sorted(include_sectors)}: "
            f"{before} → {after} ({removed_sector} removed, {removed_none} missing sector)"
        )
    else:
        logger.info("Sector inclusion filter: all sectors included")

    n_end = len(stocks)
    logger.info(f"Filters complete: {n_start} → {n_end} stocks ({n_start - n_end} removed total)")
    return stocks
