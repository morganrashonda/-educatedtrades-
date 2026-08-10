"""
Live News Ingestion for Educated Trades.

Fetches financial news headlines from the Finnhub API and prepares them
for consumption by the Sentiment Analysis Engine.

Fails closed with no fabricated data when the Finnhub provider is unavailable.

Environment variables:
    FINNHUB_API_KEY  — Finnhub API key (free tier: https://finnhub.io/register)

Usage:
    from news_ingestion import NewsIngestion

    news = NewsIngestion()
    headlines = news.fetch_headlines()      # Returns list of strings
    raw = news.fetch_raw()                  # Returns list of dicts with metadata
"""

import json
import logging
import os
import time
from dataclasses import dataclass, field
from datetime import datetime, timezone
from typing import Dict, List, Optional

logger = logging.getLogger(__name__)

# ---------------------------------------------------------------------------
# Constants
# ---------------------------------------------------------------------------

FINNHUB_BASE = "https://finnhub.io/api/v1"

# Categories Finnhub supports
FINNHUB_CATEGORIES = [
    "general", "forex", "crypto", "merger",
    "earnings", "dividend", "ipo",
]

# Rate limit: Free Finnhub tier is 60 requests/minute
RATE_LIMIT_CALLS = 60
RATE_LIMIT_WINDOW_S = 60

# Default fetch parameters
DEFAULT_SYMBOLS = ["SPY", "QQQ", "AAPL", "MSFT", "TSLA", "AMZN", "GOOGL", "NVDA", "META"]

# Headlines older than this are not "news". A week-old story fed into today's
# sentiment does not merely add noise -- it reappears in every cycle until it
# ages out, pushing conviction the same direction each time. That is a
# systematic bias, which is far worse than random error.
MAX_HEADLINE_AGE_MINUTES = float(os.environ.get("MAX_HEADLINE_AGE_MINUTES", "1440"))
# Company-news lookback. Finnhub defaulted to 7 days here.
COMPANY_NEWS_LOOKBACK_DAYS = int(os.environ.get("COMPANY_NEWS_LOOKBACK_DAYS", "2"))


# ---------------------------------------------------------------------------
# Data structures
# ---------------------------------------------------------------------------
@dataclass
class NewsArticle:
    """A single news article from the Finnhub provider."""

    headline: str
    summary: str = ""
    source: str = "finnhub"
    url: str = ""
    symbol: str = ""
    datetime: float = 0.0
    category: str = "general"
    sentiment_score: Optional[float] = None
    related: str = ""

    @property
    def age_minutes(self) -> float:
        if self.datetime == 0:
            return 0.0
        return (time.time() - self.datetime) / 60.0

    def is_fresh(self, max_minutes: float = 60.0) -> bool:
        """Whether this article is within `max_minutes` of now.

        Undated articles (datetime == 0) report fresh: a missing timestamp is
        absence of evidence, not evidence of staleness.
        """
        if not self.datetime:
            return True
        return self.age_minutes <= max_minutes

    @property
    def is_recent(self) -> bool:
        """Within the last hour.

        Was declared as ``@property def is_recent(self, max_minutes=60)`` --
        a property cannot accept arguments, so the window was unreachable and
        any caller passing one got "'bool' object is not callable". Use
        is_fresh() when a specific window is needed.
        """
        return self.is_fresh(60.0)


# ---------------------------------------------------------------------------
# Finnhub Client
# ---------------------------------------------------------------------------
class FinnhubClient:
    """
    Lightweight Finnhub API client for fetching market news.

    Handles rate limiting gracefully and returns structured results.
    """

    def __init__(self, api_key: Optional[str] = None):
        self.api_key = api_key or os.environ.get("FINNHUB_API_KEY", "")
        self._last_request = 0.0
        self._request_count = 0
        self._window_start = 0.0

        if self.api_key:
            logger.info(
                "FinnhubClient initialised (key=%s...%s)",
                self.api_key[:4], self.api_key[-4:],
            )
        else:
            logger.warning(
                "No FINNHUB_API_KEY set; provider unavailable (fail-closed). "
                "No fabricated headlines will be used."
            )

    @property
    def available(self) -> bool:
        return bool(self.api_key)

    def _check_rate_limit(self) -> None:
        """Ensure we don't exceed Finnhub's 60 req/min limit."""
        now = time.time()
        if now - self._window_start > RATE_LIMIT_WINDOW_S:
            # Reset window
            self._window_start = now
            self._request_count = 0

        if self._request_count >= RATE_LIMIT_CALLS:
            sleep_time = RATE_LIMIT_WINDOW_S - (now - self._window_start)
            if sleep_time > 0:
                logger.warning(
                    "Rate limit reached. Sleeping %.1fs", sleep_time
                )
                time.sleep(sleep_time)
            self._window_start = time.time()
            self._request_count = 0

        # Polite spacing between requests
        elapsed = now - self._last_request
        if elapsed < 0.5:
            time.sleep(0.5 - elapsed)

    def _get(self, endpoint: str, params: dict) -> Optional[dict]:
        """Make a rate-limited GET request to Finnhub."""
        if not self.available:
            return None

        import requests as req

        self._check_rate_limit()
        self._last_request = time.time()
        self._request_count += 1

        url = f"{FINNHUB_BASE}{endpoint}"
        params["token"] = self.api_key

        try:
            resp = req.get(url, params=params, timeout=15)
            resp.raise_for_status()
            return resp.json()
        except req.exceptions.HTTPError as e:
            if resp.status_code == 429:
                logger.error("Finnhub rate limit hit (429). Waiting 60s.")
                time.sleep(60)
            elif resp.status_code == 403:
                logger.error("Finnhub auth error (403). Check API key.")
            else:
                logger.error("Finnhub HTTP error %s: %s", resp.status_code, e)
        except req.exceptions.ConnectionError as e:
            logger.error("Finnhub connection error: %s", e)
        except req.exceptions.Timeout:
            logger.error("Finnhub request timed out")
        except req.exceptions.RequestException as e:
            logger.error("Finnhub request failed: %s", e)
        except Exception as e:
            logger.error("Unexpected Finnhub error: %s", e)

        return None

    # ------------------------------------------------------------------
    # News fetching
    # ------------------------------------------------------------------
    def fetch_company_news(
        self, symbol: str, from_date: str = "", to_date: str = "",
    ) -> List[NewsArticle]:
        """
        Fetch news for a specific company/symbol.
        
        Args:
            symbol: Stock symbol (e.g., "AAPL", "SPY")
            from_date: "YYYY-MM-DD" (default: 7 days ago)
            to_date: "YYYY-MM-DD" (default: today)
        
        Returns:
            List of NewsArticle objects.
        """
        if not from_date:
            from_dt = (datetime.now(timezone.utc).timestamp()
                       - COMPANY_NEWS_LOOKBACK_DAYS * 86400)
            from_date = datetime.fromtimestamp(from_dt).strftime("%Y-%m-%d")
        if not to_date:
            to_date = datetime.now(timezone.utc).strftime("%Y-%m-%d")

        data = self._get(
            f"/company-news",
            {"symbol": symbol, "from": from_date, "to": to_date},
        )
        if data is None:
            raise RuntimeError(
                f"Finnhub fetch failed for /company-news (symbol={symbol})"
            )
        return self._parse_articles(data or [], symbol)

    def fetch_market_news(
        self, category: str = "general"
    ) -> List[NewsArticle]:
        """
        Fetch general market news by category.
        
        Args:
            category: One of: general, forex, crypto, merger, earnings, etc.
        
        Returns:
            List of NewsArticle objects.
        """
        if category not in FINNHUB_CATEGORIES:
            category = "general"

        data = self._get("/news", {"category": category})
        if data is None:
            raise RuntimeError(
                f"Finnhub fetch failed for /news (category={category})"
            )
        return self._parse_articles(data or [])

    def _parse_articles(
        self, data: list, symbol: str = ""
    ) -> List[NewsArticle]:
        """Parse Finnhub JSON response into NewsArticle objects."""
        articles = []
        for item in data[:50]:  # Cap at 50 per call
            try:
                headline = item.get("headline", "").strip()
                if not headline:
                    continue

                # Convert datetime (Finnhub uses seconds since epoch)
                ts = item.get("datetime", 0)

                articles.append(NewsArticle(
                    headline=headline,
                    summary=item.get("summary", ""),
                    source=item.get("source", "finnhub"),
                    url=item.get("url", ""),
                    symbol=symbol or item.get("related", ""),
                    datetime=float(ts),
                    category=item.get("category", "general"),
                    related=item.get("related", ""),
                ))
            except Exception as e:
                logger.debug("Skipping malformed article: %s", e)
                continue

        return articles


# ---------------------------------------------------------------------------
# News Ingestion Engine
# ---------------------------------------------------------------------------
class NewsIngestion:
    """
    High-level news ingestion engine that coordinates fetching from
    Finnhub when available; fails closed when the provider is unavailable.

    The output is designed for direct consumption by the Sentiment Engine.
    """

    def __init__(
        self,
        finnhub_client: Optional[FinnhubClient] = None,
        symbols: Optional[List[str]] = None,
        categories: Optional[List[str]] = None,
        simulate: bool = False,
    ):
        self.simulate = simulate
        self.news_fetch_degraded = False
        self.news_degraded_reason = None
        self.categories_attempted = 0
        self.categories_failed = 0
        self.news_articles_retrieved_total = 0
        self.news_headlines_used = 0
        self.finnhub = finnhub_client or FinnhubClient()
        self.symbols = symbols or DEFAULT_SYMBOLS
        self.categories = categories or ["general", "merger", "earnings"]

        self._cache: List[NewsArticle] = []
        self.stale_articles_dropped = 0
        self._last_fetch = 0.0
        self._fetch_count = 0

        logger.info(
            "NewsIngestion initialised (source=%s, symbols=%d)",
            "Finnhub" if self.finnhub.available else "unavailable (fail-closed)",
            len(self.symbols),
        )

    def status(self) -> dict:
        """Return provider and degradation telemetry without fetching data."""
        return {
            "provider": "finnhub" if self.finnhub.available else "unavailable",
            "available": self.finnhub.available,
            "degraded": self.news_fetch_degraded,
            "reason": self.news_degraded_reason,
            "fetch_count": self._fetch_count,
            "cached_articles": len(self._cache),
        }

    def get_headlines(self, max_headlines: int = 25) -> List[str]:
        """Compatibility alias for :meth:`fetch_headlines`."""
        return self.fetch_headlines(max_headlines)

    def get_articles(self, max_articles: int = 25) -> List[NewsArticle]:
        """Fetch provider articles for callers needing structured records."""
        return self._fetch_all(max_articles)[:max_articles]

    # ------------------------------------------------------------------
    # Fetch methods
    # ------------------------------------------------------------------
    def fetch_headlines(self, max_headlines: int = 25) -> List[str]:
        """
        Fetch headlines as a flat list of strings — directly compatible
        with SentimentEngine.analyze() and quick_batch().
        """
        articles = self._fetch_all(max_headlines)
        headlines = [a.headline for a in articles[:max_headlines]]
        self.news_headlines_used = len(headlines)
        return headlines

    def fetch_raw(self, max_articles: int = 25) -> List[dict]:
        """
        Fetch articles as JSON-serializable dicts with full metadata.
        """
        articles = self._fetch_all(max_articles)
        return [
            {
                "headline": a.headline,
                "summary": a.summary[:200] if a.summary else "",
                "source": a.source,
                "url": a.url,
                "symbol": a.symbol,
                "datetime": a.datetime,
                "category": a.category,
                "age_minutes": round(a.age_minutes, 1),
            }
            for a in articles[:max_articles]
        ]

    def _fetch_all(self, limit: int = 25) -> List[NewsArticle]:
        """
        Internal: fetch from Finnhub, merge and deduplicate; fail closed when unavailable.
        """
        self._fetch_count += 1
        self.news_fetch_degraded = False
        self.news_degraded_reason = None
        self.categories_attempted = 0
        self.categories_failed = 0
        self.news_articles_retrieved_total = 0
        self.news_headlines_used = 0

        if self.finnhub.available:
            articles = self._fetch_from_finnhub()
        else:
            self.news_fetch_degraded = True
            self.news_degraded_reason = "Finnhub unavailable; no fabricated headlines permitted"
            articles = []

        # Newest first, so truncating to `limit` keeps the most current news
        # rather than whichever category happened to be fetched first.
        articles = sorted(
            articles,
            key=lambda a: (a.datetime or 0.0),
            reverse=True,
        )

        # Drop anything past the freshness window. Articles with no timestamp
        # are kept -- absence of a date is not evidence of staleness -- but
        # they sort last.
        fresh = []
        stale_dropped = 0
        for a in articles:
            if a.datetime and not a.is_fresh(MAX_HEADLINE_AGE_MINUTES):
                stale_dropped += 1
                continue
            fresh.append(a)
        self.stale_articles_dropped = stale_dropped
        if stale_dropped:
            logger.info(
                "Dropped %d headline(s) older than %.0f minutes",
                stale_dropped, MAX_HEADLINE_AGE_MINUTES,
            )
        articles = fresh

        # Deduplicate by headline, keeping the newest occurrence.
        seen = set()
        unique = []
        for a in articles:
            key = a.headline.lower().strip()[:80]
            if key not in seen:
                seen.add(key)
                unique.append(a)

        # Everything we had was stale: that is a degraded feed, not a quiet
        # news day. Trading on an empty batch would read as neutral sentiment.
        if not unique and self.finnhub.available:
            self.news_fetch_degraded = True
            self.news_degraded_reason = (
                "all %d retrieved headline(s) were older than %.0f minutes"
                % (stale_dropped, MAX_HEADLINE_AGE_MINUTES)
            ) if stale_dropped else "provider returned no headlines"

        self._cache = unique
        self.news_articles_retrieved_total = len(unique)
        self._last_fetch = time.time()

        if self.finnhub.available:
            logger.info(
                "Fetched %d unique headlines from Finnhub (total=%d)",
                len(unique), self._fetch_count,
            )
        else:
            logger.info(
                "No headlines available; provider unavailable (total_fetches=%d)",
                self._fetch_count,
            )

        return unique

    def _fetch_from_finnhub(self) -> List[NewsArticle]:
        """Fetch from Finnhub across multiple categories and symbols."""
        all_articles: List[NewsArticle] = []

        # 1. Market news by category
        self.categories_attempted = len(self.categories)
        self.categories_failed = 0
        for cat in self.categories:
            try:
                articles = self.finnhub.fetch_market_news(cat)
                all_articles.extend(articles)
            except Exception as e:
                logger.warning("Failed to fetch category '%s': %s", cat, e)
                self.categories_failed += 1
                self.news_fetch_degraded = True
                self.news_degraded_reason = f"Finnhub category fetch failed: {cat}"

        # 2. Company news for top symbols (limited to avoid rate limits)
        for sym in self.symbols[:3]:  # Only 3 symbols to stay within rate limits
            try:
                articles = self.finnhub.fetch_company_news(sym)
                all_articles.extend(articles)
            except Exception as e:
                logger.warning("Failed to fetch news for %s: %s", sym, e)
                # Single-symbol failures don't set degraded — only category
                # failures (above) skew the aggregate market picture.

        return all_articles

    # ------------------------------------------------------------------
    # Status
    # ------------------------------------------------------------------
def get_headlines(max_headlines: int = 25) -> List[str]:
    """
    One-shot: get headlines from Finnhub; returns empty when unavailable.
    """
    ingestion = NewsIngestion()
    return ingestion.fetch_headlines(max_headlines)


def get_articles(max_articles: int = 25) -> List[dict]:
    """
    One-shot: get full article data as JSON-serializable dicts.
    """
    ingestion = NewsIngestion()
    return ingestion.fetch_raw(max_articles)


# ---------------------------------------------------------------------------
# CLI
# ---------------------------------------------------------------------------
if __name__ == "__main__":
    import sys

    logging.basicConfig(
        level=logging.INFO,
        format="%(asctime)s %(levelname)s %(message)s",
    )

    news = NewsIngestion()

    if "--json" in sys.argv:
        import json
        articles = news.fetch_raw(10)
        print(json.dumps(articles, indent=2))
    else:
        headlines = news.fetch_headlines(15)
        print(f"\n{'='*60}")
        print(f"  Educated Trades — News Ingestion")
        print(f"  Source: {'Finnhub' if news.finnhub.available else 'unavailable (fail-closed)'}")
        print(f"{'='*60}\n")
        for i, h in enumerate(headlines, 1):
            print(f"  {i:2d}. {h}")
        print(f"\n  --- Status ---")
        for k, v in news.status().items():
            print(f"  {k}: {v}")
        print()
