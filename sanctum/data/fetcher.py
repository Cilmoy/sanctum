"""
fetcher.py — Market data retrieval via yfinance with local caching.

Fetches price, financials, analyst targets, beta, market cap, sector,
and earnings history for equity analysis.
"""

import logging
import math
import random
import threading
import time
from concurrent.futures import ThreadPoolExecutor, as_completed
from typing import Optional

import pandas as pd
import yfinance as yf

from sanctum.data.cache import SanctumDB

logger = logging.getLogger(__name__)

_FETCH_WORKERS = 10         # concurrent stocks
_INTERNAL_WORKERS = 6       # concurrent attributes per stock
_RETRY_ATTEMPTS = 3
_RETRY_BASE_DELAY = 3.0

class _RateLimiter:
    """
    Shared token-bucket rate limiter for all worker threads.
    Tuned for higher throughput without triggering blocks.
    """
    def __init__(self, min_interval: float = 0.1, jitter: float = 0.1):
        self._min_interval = min_interval
        self._jitter = jitter
        self._lock = threading.Lock()
        self._last = 0.0

    def acquire(self) -> None:
        with self._lock:
            now = time.monotonic()
            gap = self._min_interval - (now - self._last)
            if gap > 0:
                time.sleep(gap + random.uniform(0.0, self._jitter))
            self._last = time.monotonic()

_rate_limiter = _RateLimiter(min_interval=0.1, jitter=0.1)


def _yf_call(fn):
    """
    Execute a yfinance call with rate-limiting and exponential-backoff retry.
    """
    last_exc = None
    for attempt in range(_RETRY_ATTEMPTS):
        _rate_limiter.acquire()
        try:
            return fn()
        except Exception as e:
            msg = str(e).lower()
            is_rate = any(k in msg for k in ["429", "rate limit", "too many", "connection", "read timeout"])
            if is_rate:
                delay = _RETRY_BASE_DELAY * (2 ** attempt) + random.uniform(0.0, 1.0)
                logger.warning(f"yfinance retry {attempt + 1}/{_RETRY_ATTEMPTS} after {delay:.1f}s: {e}")
                time.sleep(delay)
                last_exc = e
            else:
                raise
    raise last_exc


class StockData:
    """Container for all data needed by the model pipeline for a single stock."""

    def __init__(self, ticker: str):
        self.ticker = ticker

        # Price / market
        self.company_name: Optional[str] = None
        self.current_price: Optional[float] = None
        self.market_cap: Optional[float] = None  # in dollars
        self.beta: Optional[float] = None
        self.sector: Optional[str] = None
        self.industry: Optional[str] = None      # for semiconductor classification
        self.shares_outstanding: Optional[float] = None  # diluted

        # Historical financials (most recent 5Y, annual, most-recent-first)
        self.revenue: list[float] = []
        self.gross_profit: list[float] = []
        self.operating_income: list[float] = []
        self.net_income: list[float] = []
        self.fcf: list[float] = []               # operating CF - capex
        self.total_debt: list[float] = []
        self.cash: list[float] = []
        self.interest_expense: list[float] = []
        self.ebit: list[float] = []

        # Analyst consensus
        self.analyst_target_mean: Optional[float] = None
        self.analyst_target_high: Optional[float] = None
        self.analyst_target_low: Optional[float] = None

        # Earnings
        self.eps_surprise_pct: Optional[float] = None   # most recent quarter
        self.eps_revision_trend: Optional[float] = None  # 3-month change in consensus

        # Liquidity
        self.avg_daily_volume: Optional[float] = None

        # Forward estimates
        self.forward_pe: Optional[float] = None
        self.forward_eps: Optional[float] = None
        self.trailing_pe: Optional[float] = None

        # Technical / market stats
        self.ma_50: Optional[float] = None
        self.ma_200: Optional[float] = None
        self.week_52_high: Optional[float] = None
        self.week_52_low: Optional[float] = None
        self.dividend_yield: Optional[float] = None   # decimal, e.g. 0.015 = 1.5%
        self.short_ratio: Optional[float] = None       # days to cover

        # Alpha Pipeline Extras (Finviz / Technicals)
        self.relative_volume: Optional[float] = None
        self.rsi_14: Optional[float] = None
        self.volatility_week: Optional[float] = None
        self.volatility_month: Optional[float] = None
        self.bbw: Optional[float] = None
        self.is_squeeze: Optional[bool] = None
        self.squeeze_percentile: Optional[float] = None

        # Currency metadata
        self.financial_currency: Optional[str] = None
        self.price_currency: Optional[str] = None
        self.fx_rate_applied: Optional[float] = None

        # Ownership / short interest (from info dict, available in screen mode)
        self.short_pct_float: Optional[float] = None      # short interest as % of float
        self.institutional_own_pct: Optional[float] = None
        self.insider_own_pct: Optional[float] = None

        # Earnings cadence (expanded from history already fetched in screen mode)
        self.earnings_beat_streak: Optional[int] = None   # consecutive quarters beating
        self.earnings_beat_avg_pct: Optional[float] = None  # avg beat magnitude (decimal)
        self.earnings_beat_accelerating: Optional[bool] = None  # last beat > prior avg

        # Earnings calendar + catalyst extras (populated in full/analyze mode only)
        self.next_earnings_date: Optional[str] = None  # ISO date string e.g. "2025-10-23"
        self.days_to_earnings: Optional[int] = None
        self.analyst_net_upgrades_30d: Optional[int] = None  # upgrades - downgrades, 30d
        self.insider_buys_60d: Optional[int] = None    # unique insiders buying, 60d
        self.insider_buy_value_60d: Optional[float] = None  # total $ value purchased, 60d

        # News sentiment — VADER compound average over recent headlines [-1, 1]
        self.news_sentiment: Optional[float] = None

    @property
    def gross_margin_history(self) -> list[float]:
        """Returns list of gross margin ratios (most recent first)."""
        margins = []
        for gp, rev in zip(self.gross_profit, self.revenue):
            if rev and rev != 0:
                margins.append(gp / rev)
        return margins

    @property
    def latest_gross_margin(self) -> Optional[float]:
        m = self.gross_margin_history
        return m[0] if m else None

    @property
    def net_debt(self) -> Optional[float]:
        if self.total_debt and self.cash:
            d = self.total_debt[0] if self.total_debt else 0.0
            c = self.cash[0] if self.cash else 0.0
            return d - c
        return None

    def to_dict(self) -> dict:
        """Serialize to a JSON-safe dict. Non-finite floats become None."""
        def _clean(v):
            if isinstance(v, float) and not math.isfinite(v):
                return None
            return v

        def _clean_list(lst):
            return [_clean(v) for v in lst]

        return {
            "ticker": self.ticker,
            "company_name": self.company_name,
            "current_price": _clean(self.current_price),
            "market_cap": _clean(self.market_cap),
            "beta": _clean(self.beta),
            "sector": self.sector,
            "industry": self.industry,
            "shares_outstanding": _clean(self.shares_outstanding),
            "revenue": _clean_list(self.revenue),
            "gross_profit": _clean_list(self.gross_profit),
            "operating_income": _clean_list(self.operating_income),
            "net_income": _clean_list(self.net_income),
            "fcf": _clean_list(self.fcf),
            "total_debt": _clean_list(self.total_debt),
            "cash": _clean_list(self.cash),
            "interest_expense": _clean_list(self.interest_expense),
            "ebit": _clean_list(self.ebit),
            "analyst_target_mean": _clean(self.analyst_target_mean),
            "analyst_target_high": _clean(self.analyst_target_high),
            "analyst_target_low": _clean(self.analyst_target_low),
            "eps_surprise_pct": _clean(self.eps_surprise_pct),
            "eps_revision_trend": _clean(self.eps_revision_trend),
            "avg_daily_volume": _clean(self.avg_daily_volume),
            "forward_pe": _clean(self.forward_pe),
            "forward_eps": _clean(self.forward_eps),
            "trailing_pe": _clean(self.trailing_pe),
            "ma_50": _clean(self.ma_50),
            "ma_200": _clean(self.ma_200),
            "week_52_high": _clean(self.week_52_high),
            "week_52_low": _clean(self.week_52_low),
            "dividend_yield": _clean(self.dividend_yield),
            "short_ratio": _clean(self.short_ratio),
            "relative_volume": _clean(self.relative_volume),
            "rsi_14": _clean(self.rsi_14),
            "volatility_week": _clean(self.volatility_week),
            "volatility_month": _clean(self.volatility_month),
            "bbw": _clean(self.bbw),
            "is_squeeze": self.is_squeeze,
            "squeeze_percentile": _clean(self.squeeze_percentile),
            "financial_currency": self.financial_currency,
            "price_currency": self.price_currency,
            "fx_rate_applied": _clean(self.fx_rate_applied),
            "short_pct_float": _clean(self.short_pct_float),
            "institutional_own_pct": _clean(self.institutional_own_pct),
            "insider_own_pct": _clean(self.insider_own_pct),
            "earnings_beat_streak": self.earnings_beat_streak,
            "earnings_beat_avg_pct": _clean(self.earnings_beat_avg_pct),
            "earnings_beat_accelerating": self.earnings_beat_accelerating,
            "next_earnings_date": self.next_earnings_date,
            "days_to_earnings": self.days_to_earnings,
            "analyst_net_upgrades_30d": self.analyst_net_upgrades_30d,
            "insider_buys_60d": self.insider_buys_60d,
            "insider_buy_value_60d": _clean(self.insider_buy_value_60d),
            "news_sentiment": _clean(self.news_sentiment),
        }

    @classmethod
    def from_dict(cls, d: dict) -> "StockData":
        """Reconstruct a StockData from a serialized dict."""
        stock = cls(d["ticker"])
        stock.company_name = d.get("company_name")
        stock.current_price = d.get("current_price")
        stock.market_cap = d.get("market_cap")
        stock.beta = d.get("beta")
        stock.sector = d.get("sector")
        stock.industry = d.get("industry")
        stock.shares_outstanding = d.get("shares_outstanding")
        stock.revenue = d.get("revenue") or []
        stock.gross_profit = d.get("gross_profit") or []
        stock.operating_income = d.get("operating_income") or []
        stock.net_income = d.get("net_income") or []
        stock.fcf = d.get("fcf") or []
        stock.total_debt = d.get("total_debt") or []
        stock.cash = d.get("cash") or []
        stock.interest_expense = d.get("interest_expense") or []
        stock.ebit = d.get("ebit") or []
        stock.analyst_target_mean = d.get("analyst_target_mean")
        stock.analyst_target_high = d.get("analyst_target_high")
        stock.analyst_target_low = d.get("analyst_target_low")
        stock.eps_surprise_pct = d.get("eps_surprise_pct")
        stock.eps_revision_trend = d.get("eps_revision_trend")
        stock.avg_daily_volume = d.get("avg_daily_volume")
        stock.forward_pe = d.get("forward_pe")
        stock.forward_eps = d.get("forward_eps")
        stock.trailing_pe = d.get("trailing_pe")
        stock.ma_50 = d.get("ma_50")
        stock.ma_200 = d.get("ma_200")
        stock.week_52_high = d.get("week_52_high")
        stock.week_52_low = d.get("week_52_low")
        stock.dividend_yield = d.get("dividend_yield")
        stock.short_ratio = d.get("short_ratio")
        stock.relative_volume = d.get("relative_volume")
        stock.rsi_14 = d.get("rsi_14")
        stock.volatility_week = d.get("volatility_week")
        stock.volatility_month = d.get("volatility_month")
        stock.bbw = d.get("bbw")
        stock.is_squeeze = d.get("is_squeeze")
        stock.squeeze_percentile = d.get("squeeze_percentile")
        stock.financial_currency = d.get("financial_currency")
        stock.price_currency = d.get("price_currency")
        stock.fx_rate_applied = d.get("fx_rate_applied")
        stock.short_pct_float = d.get("short_pct_float")
        stock.institutional_own_pct = d.get("institutional_own_pct")
        stock.insider_own_pct = d.get("insider_own_pct")
        stock.earnings_beat_streak = d.get("earnings_beat_streak")
        stock.earnings_beat_avg_pct = d.get("earnings_beat_avg_pct")
        stock.earnings_beat_accelerating = d.get("earnings_beat_accelerating")
        stock.next_earnings_date = d.get("next_earnings_date")
        stock.days_to_earnings = d.get("days_to_earnings")
        stock.analyst_net_upgrades_30d = d.get("analyst_net_upgrades_30d")
        stock.insider_buys_60d = d.get("insider_buys_60d")
        stock.insider_buy_value_60d = d.get("insider_buy_value_60d")
        stock.news_sentiment = d.get("news_sentiment")
        return stock


class DataFetcher:
    """Fetches and normalizes stock data from yfinance, using local cache."""

    def __init__(self, config: dict, db: Optional[SanctumDB] = None):
        self.config = config
        if db is not None:
            self.cache = db
        else:
            self.cache = SanctumDB(config) if config.get("cache", {}).get("enabled", True) else None

    def fetch_single(self, ticker: str, full: bool = False) -> Optional[StockData]:
        """
        Fetch all data for a single ticker. Returns None on failure.

        full=True fetches slow extras (earnings calendar, upgrades/downgrades,
        insider transactions) used only during analyze — not bulk screen.
        """
        if self.cache and not full:
            cached = self.cache.get(ticker)
            if cached is not None:
                logger.debug(f"{ticker}: cache hit")
                return cached

        logger.info(f"{ticker}: fetching from yfinance (full={full})")
        try:
            yticker = yf.Ticker(ticker)
            data = self._fetch_from_yfinance(ticker, yticker)
            if data is not None and full:
                self._fetch_catalyst_extras(data, yticker, ticker)
        except Exception as e:
            logger.warning(f"{ticker}: fetch failed — {e}")
            return None

        if self.cache and data is not None and not full:
            self.cache.set(ticker, data)

        return data

    def fetch_bulk(
        self,
        tickers: list[str],
        on_ticker_complete=None,
    ) -> list[StockData]:
        """
        Fetch data for multiple tickers concurrently.

        on_ticker_complete : optional callable(ticker: str, index: int, total: int)
            Called after each ticker completes (success or fail).
        """
        results: dict[str, Optional[StockData]] = {}
        total = len(tickers)

        with ThreadPoolExecutor(max_workers=_FETCH_WORKERS) as executor:
            future_to_ticker = {
                executor.submit(self.fetch_single, ticker): ticker
                for ticker in tickers
            }
            
            for i, future in enumerate(as_completed(future_to_ticker), 1):
                ticker = future_to_ticker[future]
                try:
                    results[ticker] = future.result()
                except Exception as e:
                    logger.warning(f"{ticker}: unexpected error in fetch — {e}")
                    results[ticker] = None
                
                if on_ticker_complete:
                    on_ticker_complete(ticker, i, total)

        # Preserve original order, drop failures
        output = []
        for ticker in tickers:
            data = results.get(ticker)
            if data is not None:
                output.append(data)
            else:
                logger.warning(f"{ticker}: skipped (fetch failed)")
        return output

    def _fetch_from_yfinance(self, ticker: str, yticker=None) -> Optional[StockData]:
        if yticker is None:
            yticker = yf.Ticker(ticker)

        # ── Parallelized Attribute Fetching ───────────────────────────────────
        # FIRE: Start all network requests in parallel
        with ThreadPoolExecutor(max_workers=_INTERNAL_WORKERS) as executor:
            f_info = executor.submit(_yf_call, lambda: yticker.info)
            f_inc  = executor.submit(_yf_call, lambda: yticker.financials)
            f_bal  = executor.submit(_yf_call, lambda: yticker.balance_sheet)
            f_cf   = executor.submit(_yf_call, lambda: yticker.cashflow)
            f_eh   = executor.submit(_yf_call, lambda: yticker.earnings_history)
            f_news = executor.submit(_yf_call, lambda: yticker.news)

            # COLLECT: Wait for all results
            info     = f_info.result() or {}
            income   = f_inc.result()
            balance  = f_bal.result()
            cashflow = f_cf.result()
            earnings = f_eh.result()
            news_raw = f_news.result()

        if not info or (info.get("regularMarketPrice") is None and info.get("currentPrice") is None):
            logger.warning(f"{ticker}: no price data available")
            return None

        stock = StockData(ticker)

        # ── Price / market ──
        stock.company_name = info.get("longName") or info.get("shortName")
        stock.current_price = info.get("currentPrice") or info.get("regularMarketPrice")
        stock.market_cap = info.get("marketCap")
        stock.beta = info.get("beta")
        stock.sector = info.get("sector")
        stock.industry = info.get("industry")
        stock.shares_outstanding = info.get("sharesOutstanding")
        stock.avg_daily_volume = info.get("averageVolume")
        stock.forward_pe = info.get("forwardPE")
        stock.forward_eps = info.get("forwardEps")
        stock.trailing_pe = info.get("trailingPE")
        stock.ma_50 = info.get("fiftyDayAverage")
        stock.ma_200 = info.get("twoHundredDayAverage")
        stock.week_52_high = info.get("fiftyTwoWeekHigh")
        stock.week_52_low = info.get("fiftyTwoWeekLow")
        stock.dividend_yield = info.get("dividendYield")
        stock.short_ratio = info.get("shortRatio")
        stock.short_pct_float = info.get("shortPercentOfFloat")
        stock.institutional_own_pct = info.get("heldPercentInstitutions")
        stock.insider_own_pct = info.get("heldPercentInsiders")

        # ── Analyst targets ──
        stock.analyst_target_mean = info.get("targetMeanPrice")
        stock.analyst_target_high = info.get("targetHighPrice")
        stock.analyst_target_low = info.get("targetLowPrice")

        # ── Financials ──
        try:
            if income is not None and not income.empty:
                stock.revenue = _extract_row(income, ["Total Revenue"])
                stock.gross_profit = _extract_row(income, ["Gross Profit"])
                stock.operating_income = _extract_row(income, ["Operating Income", "EBIT"])
                stock.net_income = _extract_row(income, ["Net Income"])
                stock.interest_expense = _extract_row(income, ["Interest Expense"])
                stock.ebit = _extract_row(income, ["EBIT", "Operating Income"])

            if balance is not None and not balance.empty:
                stock.total_debt = _extract_row(balance, ["Total Debt", "Long Term Debt"])
                stock.cash = _extract_row(balance, ["Cash And Cash Equivalents", "Cash"])

            if cashflow is not None and not cashflow.empty:
                op_cf = _extract_row(cashflow, ["Operating Cash Flow", "Total Cash From Operating Activities"])
                capex = _extract_row(cashflow, ["Capital Expenditure", "Purchase Of Plant And Equipment"])
                if op_cf and capex:
                    min_len = min(len(op_cf), len(capex))
                    stock.fcf = [op_cf[i] + capex[i] for i in range(min_len)]
        except Exception as e:
            logger.warning(f"{ticker}: financials parse error — {e}")

        # ── Currency metadata and FX conversion ──────────────────────────────
        fin_currency = info.get("financialCurrency") or "USD"
        price_currency = info.get("currency") or "USD"
        stock.financial_currency = fin_currency
        stock.price_currency = price_currency

        if fin_currency != price_currency:
            fx = _get_fx_rate(fin_currency, price_currency)
            if fx and fx > 0:
                stock.fx_rate_applied = fx
                _scale_financials(stock, fx)
                logger.info(
                    f"{ticker}: FX conversion {fin_currency}→{price_currency} "
                    f"@ {fx:.6f} applied to all statement data"
                )

        # ── Earnings history ──
        try:
            if earnings is not None and not earnings.empty and "surprisePercent" in earnings.columns:
                sorted_e = earnings.sort_index(ascending=False)
                stock.eps_surprise_pct = float(sorted_e.iloc[0]["surprisePercent"])
                surprises = sorted_e["surprisePercent"].dropna().tolist()[:8]
                streak = 0
                for s in surprises:
                    if float(s) > 0: streak += 1
                    else: break
                stock.earnings_beat_streak = streak
                if len(surprises) >= 2:
                    stock.earnings_beat_avg_pct = float(sum(surprises[:4]) / min(len(surprises[:4]), 4))
                    prior_avg = sum(surprises[1:4]) / min(len(surprises[1:4]), 3) if len(surprises) > 1 else 0.0
                    stock.earnings_beat_accelerating = float(surprises[0]) > prior_avg
        except Exception as e:
            logger.debug(f"{ticker}: earnings history unavailable — {e}")

        # ── News sentiment ──
        try:
            from sanctum.data.sentiment import process_news_data
            stock.news_sentiment = process_news_data(news_raw, ticker)
        except Exception as e:
            logger.debug(f"{ticker}: news sentiment failed — {e}")

        return stock

    def _fetch_catalyst_extras(self, stock, yticker, ticker: str) -> None:
        """
        Additional API calls only made during analyze (not bulk screen).
        Each call is independent — failures are silently skipped.
        """
        import datetime as dt

        # ── Earnings calendar ─────────────────────────────────────────────────
        try:
            cal = _yf_call(lambda: yticker.calendar)
            next_date = None
            if cal is not None:
                if hasattr(cal, "empty") and not cal.empty:
                    # DataFrame: columns are dates, rows are fields
                    dates = cal.columns.tolist() if hasattr(cal, "columns") else []
                    if dates:
                        next_date = dates[0]
                elif isinstance(cal, dict):
                    raw = cal.get("Earnings Date", [])
                    if hasattr(raw, "__iter__") and not isinstance(raw, str):
                        next_date = next(iter(raw), None)
                    else:
                        next_date = raw or None
            if next_date is not None:
                if hasattr(next_date, "date"):
                    d = next_date.date()
                elif hasattr(next_date, "year"):
                    d = next_date
                else:
                    d = dt.date.fromisoformat(str(next_date)[:10])
                stock.next_earnings_date = d.isoformat()
                stock.days_to_earnings = max(0, (d - dt.date.today()).days)
        except Exception as e:
            logger.debug(f"{ticker}: earnings calendar unavailable — {e}")

        # ── Analyst upgrades/downgrades (trailing 30 days) ───────────────────
        try:
            upgrades = _yf_call(lambda: yticker.upgrades_downgrades)
            if upgrades is not None and not upgrades.empty:
                cutoff = pd.Timestamp.now(tz="UTC") - pd.Timedelta(days=30)
                idx = pd.to_datetime(upgrades.index, utc=True, errors="coerce")
                recent = upgrades[idx >= cutoff]
                if not recent.empty:
                    action_col = next((c for c in recent.columns if "action" in c.lower()), None)
                    if action_col:
                        actions = recent[action_col].str.lower().fillna("")
                        ups = actions.isin(["up", "upgrade"]).sum()
                        downs = actions.isin(["down", "downgrade"]).sum()
                        stock.analyst_net_upgrades_30d = int(ups - downs)
        except Exception as e:
            logger.debug(f"{ticker}: upgrades_downgrades unavailable — {e}")

        # ── Insider transactions (trailing 60 days, open-market purchases only) ─
        try:
            insiders = _yf_call(lambda: yticker.insider_transactions)
            if insiders is not None and not insiders.empty:
                cutoff = pd.Timestamp.now(tz="UTC") - pd.Timedelta(days=60)
                date_col = next((c for c in insiders.columns
                                 if any(k in c.lower() for k in ["date", "start"])), None)
                trans_col = next((c for c in insiders.columns
                                  if any(k in c.lower() for k in ["transaction", "text"])), None)
                if date_col and trans_col:
                    insiders[date_col] = pd.to_datetime(insiders[date_col], utc=True, errors="coerce")
                    recent = insiders[insiders[date_col] >= cutoff]
                    buys = recent[recent[trans_col].str.contains(
                        r"Buy|Purchase", case=False, na=False, regex=True
                    )]
                    # Exclude automatic / plan purchases
                    buys = buys[~buys[trans_col].str.contains(
                        r"Automatic|Plan|Exercise", case=False, na=False, regex=True
                    )]
                    insider_col = next((c for c in buys.columns
                                        if "insider" in c.lower()), None)
                    stock.insider_buys_60d = int(buys[insider_col].nunique()
                                                 if insider_col else len(buys))
                    val_col = next((c for c in buys.columns
                                    if "value" in c.lower()), None)
                    if val_col:
                        stock.insider_buy_value_60d = float(
                            pd.to_numeric(buys[val_col], errors="coerce").sum()
                        )
        except Exception as e:
            logger.debug(f"{ticker}: insider transactions unavailable — {e}")

        # ── Finviz Alpha Pipeline Scraper (Fallback/Supplement) ────────────────
        try:
            from sanctum.data.finviz_scraper import fetch_finviz_data
            fv = fetch_finviz_data(ticker)
            
            # Use Finviz as primary for Alpha Pipeline specific fields
            stock.relative_volume = fv.get("relative_volume")
            stock.rsi_14 = fv.get("rsi_14")
            stock.volatility_week = fv.get("volatility_week")
            stock.volatility_month = fv.get("volatility_month")
            
            # Use Finviz as fallback for short interest / insider buying if yf failed
            if stock.short_pct_float is None:
                stock.short_pct_float = fv.get("short_pct_float")
            
            if (stock.insider_buys_60d or 0) == 0:
                stock.insider_buys_60d = fv.get("insider_buys_60d", 0)
                stock.insider_buy_value_60d = fv.get("insider_buy_value_60d", 0.0)
                
        except Exception as e:
            logger.debug(f"{ticker}: Finviz scraper failed — {e}")


def _get_fx_rate(from_currency: str, to_currency: str) -> Optional[float]:
    """
    Fetch spot exchange rate: 1 unit of from_currency expressed in to_currency.
    Uses yfinance FX tickers (e.g. TWDUSD=X → price of 1 TWD in USD).
    """
    ticker_str = f"{from_currency}{to_currency}=X"
    try:
        fx_ticker = yf.Ticker(ticker_str)
        rate = _yf_call(lambda: fx_ticker.fast_info.get("lastPrice")
                        or fx_ticker.fast_info.get("last_price"))
        if rate and float(rate) > 0:
            return float(rate)
        rate = (_yf_call(lambda: fx_ticker.info) or {}).get("regularMarketPrice")
        if rate and float(rate) > 0:
            return float(rate)
    except Exception as e:
        logger.debug(f"FX rate fetch failed for {ticker_str}: {e}")
    return None


def _scale_financials(stock, fx: float) -> None:
    """Multiply all financial statement lists in-place by fx (converts local currency → price currency)."""
    for attr in ("revenue", "gross_profit", "operating_income", "net_income",
                 "fcf", "total_debt", "cash", "interest_expense", "ebit"):
        original = getattr(stock, attr, [])
        if original:
            setattr(stock, attr, [v * fx if v is not None else None for v in original])


def _extract_row(df: pd.DataFrame, candidate_labels: list[str]) -> list[float]:
    """Extract a financial row by trying candidate labels in order."""
    for label in candidate_labels:
        if label in df.index:
            values = df.loc[label].dropna().tolist()
            return [float(v) for v in values]
    return []


def fetch_sp500_tickers() -> list[str]:
    """
    Fetch current S&P 500 constituents.

    Primary: Wikipedia table (id='constituents', or first wikitable on the page).
    Fallback: pandas read_html, which handles Wikipedia table changes automatically.
    """
    import requests
    from bs4 import BeautifulSoup

    url = "https://en.wikipedia.org/wiki/List_of_S%26P_500_companies"
    headers = {"User-Agent": "Mozilla/5.0 (compatible; sanctum-screener/1.0)"}

    try:
        resp = requests.get(url, timeout=15, headers=headers)
        resp.raise_for_status()
        soup = BeautifulSoup(resp.text, "html.parser")

        # Try id='constituents' first, then fall back to first sortable wikitable
        table = (
            soup.find("table", {"id": "constituents"})
            or soup.find("table", {"class": "wikitable sortable"})
        )
        if table is None:
            raise ValueError("No constituents table found on page")

        tickers = []
        for row in table.find_all("tr")[1:]:
            cols = row.find_all("td")
            if cols:
                ticker = cols[0].text.strip().replace(".", "-")
                if ticker:
                    tickers.append(ticker)

        if not tickers:
            raise ValueError("Parsed zero tickers from table")

        logger.info(f"fetch_sp500_tickers: {len(tickers)} tickers loaded")
        return tickers

    except Exception as e:
        logger.warning(f"Wikipedia S&P 500 scrape failed ({e}), falling back to pandas")

    # Fallback: pandas.read_html is more resilient to Wikipedia markup changes
    try:
        import pandas as pd
        tables = pd.read_html(url, attrs={"id": "constituents"})
        if not tables:
            tables = pd.read_html(url, match="Symbol")
        df = tables[0]
        col = next((c for c in df.columns if "symbol" in str(c).lower()), df.columns[0])
        tickers = [str(t).strip().replace(".", "-") for t in df[col] if str(t).strip()]
        logger.info(f"fetch_sp500_tickers (pandas fallback): {len(tickers)} tickers loaded")
        return tickers
    except Exception as e2:
        raise RuntimeError(f"fetch_sp500_tickers: both scrapers failed. Last error: {e2}") from e2


def fetch_nasdaq100_tickers() -> list[str]:
    """
    Fetch current Nasdaq-100 constituents.

    Primary: Wikipedia table. Fallback: pandas read_html.
    """
    import requests
    from bs4 import BeautifulSoup

    url = "https://en.wikipedia.org/wiki/Nasdaq-100"
    headers = {"User-Agent": "Mozilla/5.0 (compatible; sanctum-screener/1.0)"}

    try:
        resp = requests.get(url, timeout=15, headers=headers)
        resp.raise_for_status()
        soup = BeautifulSoup(resp.text, "html.parser")

        table = (
            soup.find("table", {"id": "constituents"})
            or soup.find("table", {"class": "wikitable sortable"})
        )
        if table is None:
            raise ValueError("No constituents table found on page")

        tickers = []
        for row in table.find_all("tr")[1:]:
            cols = row.find_all("td")
            if len(cols) >= 2:
                ticker = cols[1].text.strip()
                if ticker:
                    tickers.append(ticker)

        if not tickers:
            raise ValueError("Parsed zero tickers from table")

        logger.info(f"fetch_nasdaq100_tickers: {len(tickers)} tickers loaded")
        return tickers

    except Exception as e:
        logger.warning(f"Wikipedia Nasdaq-100 scrape failed ({e}), falling back to pandas")

    try:
        import pandas as pd
        tables = pd.read_html(url, attrs={"id": "constituents"})
        if not tables:
            tables = pd.read_html(url, match="Ticker")
        df = tables[0]
        col = next((c for c in df.columns if "ticker" in str(c).lower() or "symbol" in str(c).lower()), df.columns[1])
        tickers = [str(t).strip() for t in df[col] if str(t).strip()]
        logger.info(f"fetch_nasdaq100_tickers (pandas fallback): {len(tickers)} tickers loaded")
        return tickers
    except Exception as e2:
        raise RuntimeError(f"fetch_nasdaq100_tickers: both scrapers failed. Last error: {e2}") from e2


def fetch_all_us_tickers() -> list[str]:
    """
    Fetch all US-listed equity tickers.

    Primary: NASDAQ's public directory files (NYSE + NASDAQ + AMEX, ~8K tickers).
    Fallback: SEC EDGAR company tickers JSON (all SEC-registered public companies).

    Both sources filter out ETFs, test issues, and special securities.
    """
    tickers = _fetch_all_us_nasdaq()
    if tickers:
        return tickers

    logger.warning("NASDAQ directory fetch returned no tickers — trying SEC EDGAR fallback")
    tickers = _fetch_all_us_sec_edgar()
    if tickers:
        return tickers

    raise RuntimeError(
        "fetch_all_us_tickers: both NASDAQ and SEC EDGAR sources failed. "
        "Check network connectivity."
    )


def _fetch_all_us_nasdaq() -> list[str]:
    """Fetch from NASDAQ's pipe-delimited directory files."""
    import requests

    sources = [
        ("https://ftp.nasdaqtrader.com/dynamic/SymDir/nasdaqlisted.txt", "nasdaq"),
        ("https://ftp.nasdaqtrader.com/dynamic/SymDir/otherlisted.txt", "other"),
    ]
    tickers: list[str] = []

    for url, exchange in sources:
        try:
            resp = requests.get(url, timeout=15)
            resp.raise_for_status()
            lines = resp.text.splitlines()
            if not lines:
                continue

            header = lines[0].split("|")

            for line in lines[1:]:
                if line.startswith("File Creation Time"):
                    break
                cols = line.split("|")
                if len(cols) < len(header):
                    continue

                row = dict(zip(header, cols))

                # nasdaqlisted.txt columns: Symbol, Security Name, Market Category,
                #                           Test Issue, Financial Status, Round Lot Size, ETF, NextShares
                # otherlisted.txt columns:  ACT Symbol, Security Name, Exchange, CQS Symbol,
                #                           ETF, Round Lot Size, Test Issue, NASDAQ Symbol
                symbol_key = "Symbol" if exchange == "nasdaq" else "ACT Symbol"
                symbol   = row.get(symbol_key, "").strip()
                is_etf   = row.get("ETF", "N").strip().upper() == "Y"
                is_test  = row.get("Test Issue", "N").strip().upper() == "Y"

                if not symbol or is_etf or is_test:
                    continue
                if any(c in symbol for c in ("$", "+", ".", "^", "~", "%")):
                    continue
                if symbol.endswith("P") and len(symbol) > 4:
                    continue

                tickers.append(symbol)

        except Exception as e:
            logger.warning(f"NASDAQ directory fetch failed for {exchange}: {e}")

    unique = _dedupe(tickers)
    if unique:
        logger.info(f"fetch_all_us_tickers (NASDAQ): {len(unique)} tickers loaded")
    return unique


def _fetch_all_us_sec_edgar() -> list[str]:
    """
    Fallback: fetch all SEC-registered company tickers from EDGAR.

    Returns ~10K tickers including OTC; filters to symbols ≤5 chars (exchange-listed).
    """
    import requests

    url = "https://www.sec.gov/files/company_tickers.json"
    headers = {"User-Agent": "sanctum-screener/1.0 (contact@sanctumllc.com)"}

    try:
        resp = requests.get(url, timeout=15, headers=headers)
        resp.raise_for_status()
        data = resp.json()

        tickers = []
        for entry in data.values():
            symbol = str(entry.get("ticker", "")).strip().upper()
            if not symbol:
                continue
            # Exclude OTC/pink sheet symbols (>5 chars) and special chars
            if len(symbol) > 5:
                continue
            if any(c in symbol for c in ("$", "+", ".", "^", "~", "%", "-")):
                continue
            tickers.append(symbol)

        unique = _dedupe(tickers)
        logger.info(f"fetch_all_us_tickers (SEC EDGAR fallback): {len(unique)} tickers loaded")
        return unique

    except Exception as e:
        logger.warning(f"SEC EDGAR fallback failed: {e}")
        return []


def _dedupe(tickers: list[str]) -> list[str]:
    seen: set[str] = set()
    out: list[str] = []
    for t in tickers:
        if t not in seen:
            seen.add(t)
            out.append(t)
    return out
