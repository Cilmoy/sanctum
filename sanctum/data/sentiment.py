"""
sentiment.py — News headline sentiment scoring via VADER.

Fetches recent news headlines and summaries for a ticker via yfinance and
returns the average VADER compound sentiment in [-1, 1].

Returns None if vaderSentiment is not installed or no news is available,
so the upstream caller can treat it as a skipped evidence factor.
"""

import logging
from typing import Optional

logger = logging.getLogger(__name__)

_MAX_ARTICLES = 10


def fetch_news_sentiment(yticker, ticker: str) -> Optional[float]:
    """
    Fetch recent news and return mean VADER compound sentiment score in [-1, 1].

    Returns None if vaderSentiment is not installed or no news is found.
    """
    try:
        news = yticker.news or []
    except Exception as e:
        logger.debug(f"{ticker}: news fetch failed — {e}")
        return None

    return process_news_data(news, ticker)


def process_news_data(news: list, ticker: str) -> Optional[float]:
    """
    Process raw news data (list of articles) into a mean VADER sentiment score.
    """
    if not news:
        return None

    try:
        from vaderSentiment.vaderSentiment import SentimentIntensityAnalyzer
    except ImportError:
        logger.debug(f"{ticker}: vaderSentiment not installed; news sentiment skipped")
        return None

    analyzer = SentimentIntensityAnalyzer()
    scores = []

    for article in news[:_MAX_ARTICLES]:
        content = article.get("content", article)
        title = content.get("title", "") or ""
        summary = content.get("summary", "") or ""
        text = f"{title}. {summary}".strip(". ")
        if not text:
            continue
        scores.append(analyzer.polarity_scores(text)["compound"])

    if not scores:
        return None

    mean_score = sum(scores) / len(scores)
    logger.debug(f"{ticker}: news sentiment={mean_score:.3f} (n={len(scores)} articles)")
    return mean_score
