"""
title: Stock Data
author: mdelponte
author_url: https://github.com/mdelponte
version: 1.1.0
required_open_webui_version: 0.4.0
license: MIT
description: Query stock market data — quotes, fundamentals, financials, earnings, news, and stock screening. Uses Finnhub (primary, free API key), yfinance (no-key fallback), and Financial Modeling Prep for deep financials and screening.
requirements: requests, yfinance
"""

import asyncio
import json
import time
from datetime import datetime, timezone
from typing import Any, Awaitable, Callable, Literal, Optional

import requests
from pydantic import BaseModel, Field


# -------------------------- Helpers --------------------------

def _safe_float(v: Any) -> Optional[float]:
    try:
        if v is None or v == "":
            return None
        return float(v)
    except (TypeError, ValueError):
        return None


def _safe_int(v: Any) -> Optional[int]:
    try:
        if v is None or v == "":
            return None
        return int(v)
    except (TypeError, ValueError):
        return None


def _ts_to_iso(ts: Optional[int]) -> Optional[str]:
    if not ts:
        return None
    try:
        return datetime.fromtimestamp(int(ts), tz=timezone.utc).isoformat()
    except (TypeError, ValueError, OSError):
        return None


def _format_large_number(n: Optional[float]) -> Optional[str]:
    """Render large numbers like market cap in human-readable form."""
    if n is None:
        return None
    try:
        n = float(n)
    except (TypeError, ValueError):
        return None
    abs_n = abs(n)
    if abs_n >= 1e12:
        return f"{n / 1e12:.2f}T"
    if abs_n >= 1e9:
        return f"{n / 1e9:.2f}B"
    if abs_n >= 1e6:
        return f"{n / 1e6:.2f}M"
    if abs_n >= 1e3:
        return f"{n / 1e3:.2f}K"
    return f"{n:.2f}"


# -------------------------- Tool --------------------------

class Tools:
    """
    Stock market data tool for Open WebUI.

    Provides:
      - Real-time / latest stock quotes (price, volume, day range, change)
      - Company profile and fundamentals (market cap, sector, industry, P/E, etc.)
      - Annual/quarterly financial statements (income, balance sheet, cash flow)
      - Earnings calendar and historical earnings (actual vs estimate, surprises)
      - Recent company news
      - Analyst recommendations
      - Stock screening by basic criteria (market cap, price, volume, sector, etc.)
      - Advanced fundamental screening (cash on hand, P/E, ROE, margins, debt, etc.)

    Provider strategy (controlled by valves):
      - "auto":      Try Finnhub first if key set, else yfinance. Fall back across providers on failure.
      - "finnhub":   Force Finnhub (requires API key).
      - "yfinance":  Force yfinance (no key needed; unofficial Yahoo scraping).
      - "fmp":       Force Financial Modeling Prep (requires API key).

    Note: Stock screening tools require an FMP API key (free tier available at
    financialmodelingprep.com). Quotes/profiles/news work without any keys via yfinance.
    """

    class Valves(BaseModel):
        # ---- API keys ----
        finnhub_api_key: str = Field(
            default="",
            description="Finnhub API key (free at finnhub.io). Recommended primary provider — 60 calls/min on free tier.",
            json_schema_extra={"input": {"type": "password"}},
        )
        fmp_api_key: str = Field(
            default="",
            description="Financial Modeling Prep API key (free tier ~250/day). Required for stock screening. Used for deep financial statements when set.",
            json_schema_extra={"input": {"type": "password"}},
        )
        alpha_vantage_api_key: str = Field(
            default="",
            description="Alpha Vantage API key (optional, free tier 25/day). Reserved for future use / manual fallback.",
            json_schema_extra={"input": {"type": "password"}},
        )

        # ---- Provider behavior ----
        default_provider: Literal["auto", "finnhub", "yfinance", "fmp"] = Field(
            default="auto",
            description="Default data provider. 'auto' picks the best available based on configured keys.",
        )
        financials_provider: Literal["auto", "fmp", "yfinance", "finnhub"] = Field(
            default="auto",
            description="Provider for deep financial statements. FMP gives the cleanest data, yfinance is free/no-key.",
        )
        prefer_yfinance_fallback: bool = Field(
            default=True,
            description="If a paid/limited provider fails or returns nothing, automatically retry with yfinance.",
        )

        # ---- Networking / safety ----
        request_timeout: int = Field(
            default=15,
            description="HTTP request timeout in seconds for API calls.",
        )
        cache_ttl_seconds: int = Field(
            default=60,
            description="Cache responses for this many seconds to reduce API calls. Set to 0 to disable.",
        )
        max_news_items: int = Field(
            default=5,
            description="Maximum number of news articles to return per query.",
        )
        max_financial_periods: int = Field(
            default=4,
            description="Maximum number of historical financial statement periods (years or quarters) to return.",
        )
        screener_result_limit: int = Field(
            default=25,
            description="Maximum number of stocks returned by screening tools. Higher values use more API quota, especially for fundamentals-based screening.",
        )
        screener_universe_size: int = Field(
            default=200,
            description="When screening by fundamentals (cash, P/E, etc.), how large an initial universe to pull from FMP before filtering. Higher = more thorough but slower.",
        )

    class UserValves(BaseModel):
        verbose_status: bool = Field(
            default=True,
            description="Show progress status messages while the tool fetches data.",
        )
        include_raw_numbers: bool = Field(
            default=False,
            description="Include raw numeric values alongside human-readable formatting (e.g. market cap in both '3.45T' and 3450000000000).",
        )

    def __init__(self):
        self.valves = self.Valves()
        self._cache: dict[str, tuple[float, Any]] = {}
        # Open WebUI uses these flags; setting citation True groups output as a citation block
        self.citation = True

    # -------------------------- Internal: cache + HTTP --------------------------

    def _cache_get(self, key: str) -> Optional[Any]:
        if self.valves.cache_ttl_seconds <= 0:
            return None
        entry = self._cache.get(key)
        if not entry:
            return None
        ts, value = entry
        if time.time() - ts > self.valves.cache_ttl_seconds:
            self._cache.pop(key, None)
            return None
        return value

    def _cache_set(self, key: str, value: Any) -> None:
        if self.valves.cache_ttl_seconds <= 0:
            return
        self._cache[key] = (time.time(), value)

    def _http_get_json(self, url: str, params: Optional[dict] = None) -> Any:
        cache_key = f"GET::{url}::{json.dumps(params or {}, sort_keys=True)}"
        cached = self._cache_get(cache_key)
        if cached is not None:
            return cached
        resp = requests.get(
            url,
            params=params,
            timeout=self.valves.request_timeout,
            headers={"User-Agent": "OpenWebUI-StockDataTool/1.0"},
        )
        resp.raise_for_status()
        data = resp.json()
        self._cache_set(cache_key, data)
        return data

    # -------------------------- Internal: status emitter --------------------------

    async def _emit(
        self,
        event_emitter: Optional[Callable[[dict], Awaitable[None]]],
        description: str,
        done: bool = False,
        user: Optional[dict] = None,
    ) -> None:
        """Emit a status event if user has verbose_status enabled."""
        if not event_emitter:
            return
        try:
            uv = (user or {}).get("valves")
            verbose = getattr(uv, "verbose_status", True) if uv is not None else True
        except Exception:
            verbose = True
        if not verbose and not done:
            return
        try:
            await event_emitter(
                {"type": "status", "data": {"description": description, "done": done}}
            )
        except Exception:
            # Don't let emitter errors break the actual tool
            pass

    # -------------------------- Internal: provider resolution --------------------------

    def _resolve_provider(self, requested: str, *, for_financials: bool = False) -> str:
        """Resolve 'auto' to a concrete provider based on configured keys."""
        if requested != "auto":
            return requested

        if for_financials:
            # Financials prefer FMP > yfinance > finnhub
            if self.valves.fmp_api_key:
                return "fmp"
            return "yfinance"  # yfinance has reasonable financials with no key

        # Quotes/profiles prefer finnhub > yfinance
        if self.valves.finnhub_api_key:
            return "finnhub"
        return "yfinance"

    # ===================================================================
    #                          PUBLIC TOOL METHODS
    # ===================================================================

    async def get_stock_quote(
        self,
        symbol: str,
        __user__: Optional[dict] = None,
        __event_emitter__: Optional[Callable[[dict], Awaitable[None]]] = None,
    ) -> str:
        """
        Get the current stock quote for a ticker symbol — including price, day's change,
        open/high/low/previous close, and trading volume.

        :param symbol: The stock ticker symbol (e.g. "AAPL", "MSFT", "TSLA").
        :return: A JSON string with the latest quote data.
        """
        symbol = (symbol or "").strip().upper()
        if not symbol:
            return json.dumps({"error": "Symbol is required."})

        await self._emit(__event_emitter__, f"Fetching quote for {symbol}…", user=__user__)

        provider = self._resolve_provider(self.valves.default_provider)
        result: Optional[dict] = None
        errors: list[str] = []

        try:
            if provider == "finnhub":
                result = await asyncio.to_thread(self._finnhub_quote, symbol)
            elif provider == "yfinance":
                result = await asyncio.to_thread(self._yfinance_quote, symbol)
            elif provider == "fmp":
                result = await asyncio.to_thread(self._fmp_quote, symbol)
        except Exception as e:
            errors.append(f"{provider}: {type(e).__name__}: {e}")
            result = None

        # Fallback to yfinance if primary failed
        if (not result) and self.valves.prefer_yfinance_fallback and provider != "yfinance":
            await self._emit(__event_emitter__, f"Primary provider failed, trying yfinance…", user=__user__)
            try:
                result = await asyncio.to_thread(self._yfinance_quote, symbol)
            except Exception as e:
                errors.append(f"yfinance: {type(e).__name__}: {e}")

        await self._emit(__event_emitter__, f"Quote retrieved for {symbol}", done=True, user=__user__)

        if not result:
            return json.dumps({
                "symbol": symbol,
                "error": "Could not retrieve quote.",
                "provider_errors": errors,
            })

        return json.dumps(result, default=str)

    async def get_company_profile(
        self,
        symbol: str,
        __user__: Optional[dict] = None,
        __event_emitter__: Optional[Callable[[dict], Awaitable[None]]] = None,
    ) -> str:
        """
        Get the company profile and key fundamentals for a ticker — name, sector, industry,
        market cap, employees, exchange, P/E, EPS, dividend yield, 52-week range, and beta.

        :param symbol: The stock ticker symbol (e.g. "AAPL").
        :return: A JSON string with the company profile and key metrics.
        """
        symbol = (symbol or "").strip().upper()
        if not symbol:
            return json.dumps({"error": "Symbol is required."})

        await self._emit(__event_emitter__, f"Fetching company profile for {symbol}…", user=__user__)

        provider = self._resolve_provider(self.valves.default_provider)
        result: Optional[dict] = None
        errors: list[str] = []

        try:
            if provider == "finnhub":
                result = await asyncio.to_thread(self._finnhub_profile, symbol)
            elif provider == "yfinance":
                result = await asyncio.to_thread(self._yfinance_profile, symbol)
            elif provider == "fmp":
                result = await asyncio.to_thread(self._fmp_profile, symbol)
        except Exception as e:
            errors.append(f"{provider}: {type(e).__name__}: {e}")

        if (not result) and self.valves.prefer_yfinance_fallback and provider != "yfinance":
            await self._emit(__event_emitter__, "Primary provider failed, trying yfinance…", user=__user__)
            try:
                result = await asyncio.to_thread(self._yfinance_profile, symbol)
            except Exception as e:
                errors.append(f"yfinance: {type(e).__name__}: {e}")

        await self._emit(__event_emitter__, f"Profile retrieved for {symbol}", done=True, user=__user__)

        if not result:
            return json.dumps({
                "symbol": symbol,
                "error": "Could not retrieve profile.",
                "provider_errors": errors,
            })

        return json.dumps(result, default=str)

    async def get_financials(
        self,
        symbol: str,
        statement: Literal["income", "balance", "cashflow"] = "income",
        period: Literal["annual", "quarterly"] = "annual",
        __user__: Optional[dict] = None,
        __event_emitter__: Optional[Callable[[dict], Awaitable[None]]] = None,
    ) -> str:
        """
        Get financial statements for a company — income statement, balance sheet, or cash flow.
        Returns the most recent N periods (configured by the max_financial_periods valve).

        :param symbol: The stock ticker symbol (e.g. "AAPL").
        :param statement: Which statement to fetch — "income", "balance", or "cashflow".
        :param period: "annual" for yearly statements, "quarterly" for quarterly.
        :return: A JSON string with the requested financial statements.
        """
        symbol = (symbol or "").strip().upper()
        if not symbol:
            return json.dumps({"error": "Symbol is required."})

        if statement not in ("income", "balance", "cashflow"):
            return json.dumps({"error": "statement must be one of: income, balance, cashflow"})
        if period not in ("annual", "quarterly"):
            return json.dumps({"error": "period must be 'annual' or 'quarterly'"})

        await self._emit(
            __event_emitter__,
            f"Fetching {period} {statement} statement for {symbol}…",
            user=__user__,
        )

        provider = self._resolve_provider(self.valves.financials_provider, for_financials=True)
        result: Optional[dict] = None
        errors: list[str] = []

        try:
            if provider == "fmp":
                result = await asyncio.to_thread(self._fmp_financials, symbol, statement, period)
            elif provider == "yfinance":
                result = await asyncio.to_thread(self._yfinance_financials, symbol, statement, period)
            elif provider == "finnhub":
                result = await asyncio.to_thread(self._finnhub_financials, symbol, statement, period)
        except Exception as e:
            errors.append(f"{provider}: {type(e).__name__}: {e}")

        if (not result) and self.valves.prefer_yfinance_fallback and provider != "yfinance":
            await self._emit(__event_emitter__, "Primary provider failed, trying yfinance…", user=__user__)
            try:
                result = await asyncio.to_thread(self._yfinance_financials, symbol, statement, period)
            except Exception as e:
                errors.append(f"yfinance: {type(e).__name__}: {e}")

        await self._emit(__event_emitter__, f"Financials retrieved for {symbol}", done=True, user=__user__)

        if not result:
            return json.dumps({
                "symbol": symbol,
                "error": "Could not retrieve financials.",
                "provider_errors": errors,
            })

        return json.dumps(result, default=str)

    async def get_earnings(
        self,
        symbol: str,
        __user__: Optional[dict] = None,
        __event_emitter__: Optional[Callable[[dict], Awaitable[None]]] = None,
    ) -> str:
        """
        Get historical earnings reports for a company — actual EPS, estimated EPS,
        surprise %, and revenue figures by quarter.

        :param symbol: The stock ticker symbol (e.g. "AAPL").
        :return: A JSON string with historical earnings data.
        """
        symbol = (symbol or "").strip().upper()
        if not symbol:
            return json.dumps({"error": "Symbol is required."})

        await self._emit(__event_emitter__, f"Fetching earnings for {symbol}…", user=__user__)

        result: Optional[dict] = None
        errors: list[str] = []

        # Earnings: prefer Finnhub (clean structured data), fall back to yfinance
        provider = self._resolve_provider(self.valves.default_provider)
        try:
            if provider == "finnhub":
                result = await asyncio.to_thread(self._finnhub_earnings, symbol)
            elif provider == "fmp":
                result = await asyncio.to_thread(self._fmp_earnings, symbol)
            else:
                result = await asyncio.to_thread(self._yfinance_earnings, symbol)
        except Exception as e:
            errors.append(f"{provider}: {type(e).__name__}: {e}")

        if (not result) and self.valves.prefer_yfinance_fallback and provider != "yfinance":
            try:
                result = await asyncio.to_thread(self._yfinance_earnings, symbol)
            except Exception as e:
                errors.append(f"yfinance: {type(e).__name__}: {e}")

        await self._emit(__event_emitter__, f"Earnings retrieved for {symbol}", done=True, user=__user__)

        if not result:
            return json.dumps({
                "symbol": symbol,
                "error": "Could not retrieve earnings.",
                "provider_errors": errors,
            })

        return json.dumps(result, default=str)

    async def get_company_news(
        self,
        symbol: str,
        __user__: Optional[dict] = None,
        __event_emitter__: Optional[Callable[[dict], Awaitable[None]]] = None,
    ) -> str:
        """
        Get recent news articles about a specific company.

        :param symbol: The stock ticker symbol (e.g. "AAPL").
        :return: A JSON string with recent news articles (headline, source, summary, url, published date).
        """
        symbol = (symbol or "").strip().upper()
        if not symbol:
            return json.dumps({"error": "Symbol is required."})

        await self._emit(__event_emitter__, f"Fetching news for {symbol}…", user=__user__)

        result: Optional[dict] = None
        errors: list[str] = []

        # News: Finnhub has a clean dedicated endpoint; yfinance also works
        provider = self._resolve_provider(self.valves.default_provider)
        try:
            if provider == "finnhub":
                result = await asyncio.to_thread(self._finnhub_news, symbol)
            else:
                result = await asyncio.to_thread(self._yfinance_news, symbol)
        except Exception as e:
            errors.append(f"{provider}: {type(e).__name__}: {e}")

        if (not result) and self.valves.prefer_yfinance_fallback and provider != "yfinance":
            try:
                result = await asyncio.to_thread(self._yfinance_news, symbol)
            except Exception as e:
                errors.append(f"yfinance: {type(e).__name__}: {e}")

        await self._emit(__event_emitter__, f"News retrieved for {symbol}", done=True, user=__user__)

        if not result:
            return json.dumps({
                "symbol": symbol,
                "error": "Could not retrieve news.",
                "provider_errors": errors,
            })

        return json.dumps(result, default=str)

    async def get_analyst_recommendations(
        self,
        symbol: str,
        __user__: Optional[dict] = None,
        __event_emitter__: Optional[Callable[[dict], Awaitable[None]]] = None,
    ) -> str:
        """
        Get the latest analyst recommendation trends for a stock —
        counts of strong buy / buy / hold / sell / strong sell ratings over recent months.

        :param symbol: The stock ticker symbol (e.g. "AAPL").
        :return: A JSON string with analyst recommendation data.
        """
        symbol = (symbol or "").strip().upper()
        if not symbol:
            return json.dumps({"error": "Symbol is required."})

        await self._emit(__event_emitter__, f"Fetching analyst recommendations for {symbol}…", user=__user__)

        result: Optional[dict] = None
        errors: list[str] = []
        provider = self._resolve_provider(self.valves.default_provider)

        try:
            if provider == "finnhub":
                result = await asyncio.to_thread(self._finnhub_recommendations, symbol)
            else:
                result = await asyncio.to_thread(self._yfinance_recommendations, symbol)
        except Exception as e:
            errors.append(f"{provider}: {type(e).__name__}: {e}")

        if (not result) and self.valves.prefer_yfinance_fallback and provider != "yfinance":
            try:
                result = await asyncio.to_thread(self._yfinance_recommendations, symbol)
            except Exception as e:
                errors.append(f"yfinance: {type(e).__name__}: {e}")

        await self._emit(__event_emitter__, f"Recommendations retrieved for {symbol}", done=True, user=__user__)

        if not result:
            return json.dumps({
                "symbol": symbol,
                "error": "Could not retrieve recommendations.",
                "provider_errors": errors,
            })

        return json.dumps(result, default=str)

    async def search_symbol(
        self,
        query: str,
        __user__: Optional[dict] = None,
        __event_emitter__: Optional[Callable[[dict], Awaitable[None]]] = None,
    ) -> str:
        """
        Search for a stock ticker symbol by company name or partial symbol.
        Useful when the user names a company but you don't know its ticker.

        :param query: The company name or partial ticker to search for (e.g. "apple", "microsoft").
        :return: A JSON string with matching tickers and company names.
        """
        query = (query or "").strip()
        if not query:
            return json.dumps({"error": "Query is required."})

        await self._emit(__event_emitter__, f"Searching symbols for '{query}'…", user=__user__)

        # Symbol search needs Finnhub (yfinance doesn't have a clean search endpoint)
        if not self.valves.finnhub_api_key:
            await self._emit(__event_emitter__, "Search complete", done=True, user=__user__)
            return json.dumps({
                "error": "Symbol search requires a Finnhub API key. Configure it in the tool valves.",
                "query": query,
            })

        try:
            data = await asyncio.to_thread(
                self._http_get_json,
                "https://finnhub.io/api/v1/search",
                {"q": query, "token": self.valves.finnhub_api_key},
            )
            results = []
            for item in (data.get("result") or [])[:10]:
                results.append({
                    "symbol": item.get("symbol"),
                    "description": item.get("description"),
                    "type": item.get("type"),
                })
            await self._emit(__event_emitter__, f"Found {len(results)} matches", done=True, user=__user__)
            return json.dumps({"query": query, "count": len(results), "results": results})
        except Exception as e:
            await self._emit(__event_emitter__, "Search failed", done=True, user=__user__)
            return json.dumps({"error": f"Search failed: {type(e).__name__}: {e}", "query": query})

    async def screen_stocks(
        self,
        market_cap_more_than: Optional[float] = None,
        market_cap_less_than: Optional[float] = None,
        price_more_than: Optional[float] = None,
        price_less_than: Optional[float] = None,
        volume_more_than: Optional[int] = None,
        volume_less_than: Optional[int] = None,
        beta_more_than: Optional[float] = None,
        beta_less_than: Optional[float] = None,
        dividend_more_than: Optional[float] = None,
        dividend_less_than: Optional[float] = None,
        sector: Optional[str] = None,
        industry: Optional[str] = None,
        country: Optional[str] = None,
        exchange: Optional[str] = None,
        is_etf: Optional[bool] = None,
        is_fund: Optional[bool] = None,
        is_actively_trading: Optional[bool] = True,
        __user__: Optional[dict] = None,
        __event_emitter__: Optional[Callable[[dict], Awaitable[None]]] = None,
    ) -> str:
        """
        Screen stocks by basic market criteria. Returns a list of companies matching all the
        specified filters. All parameters are optional — only provided ones are applied.

        Use this for queries like "find tech stocks with market cap over $10B" or
        "show me NYSE-listed dividend stocks under $50". For deeper fundamentals-based
        screening (cash on hand, P/E ratio, ROE, etc.), use screen_by_fundamentals instead.

        :param market_cap_more_than: Minimum market cap in raw dollars (e.g. 10000000000 for $10B).
        :param market_cap_less_than: Maximum market cap in raw dollars.
        :param price_more_than: Minimum share price.
        :param price_less_than: Maximum share price.
        :param volume_more_than: Minimum daily trading volume.
        :param volume_less_than: Maximum daily trading volume.
        :param beta_more_than: Minimum beta (volatility relative to market; >1 means more volatile).
        :param beta_less_than: Maximum beta.
        :param dividend_more_than: Minimum dividend per share.
        :param dividend_less_than: Maximum dividend per share.
        :param sector: Sector name. Common values: "Technology", "Healthcare", "Financial Services",
            "Consumer Cyclical", "Consumer Defensive", "Energy", "Industrials", "Basic Materials",
            "Communication Services", "Real Estate", "Utilities".
        :param industry: Industry name (e.g. "Software", "Banks", "Semiconductors").
        :param country: ISO-2 country code (e.g. "US", "UK", "CA", "JP", "DE").
        :param exchange: Exchange name. Common values: "nyse", "nasdaq", "amex", "tsx", "lse".
        :param is_etf: Filter to/from ETFs (True = only ETFs, False = exclude ETFs).
        :param is_fund: Filter to/from mutual funds.
        :param is_actively_trading: Only return actively trading securities (default True).
        :return: A JSON string with matching stocks (symbol, name, market cap, price, sector, etc.).
        """
        if not self.valves.fmp_api_key:
            return json.dumps({
                "error": "Stock screening requires a Financial Modeling Prep (FMP) API key. "
                         "Configure 'fmp_api_key' in the tool valves. Free tier available at financialmodelingprep.com."
            })

        await self._emit(__event_emitter__, "Screening stocks…", user=__user__)

        params: dict[str, Any] = {
            "apikey": self.valves.fmp_api_key,
            "limit": self.valves.screener_result_limit,
        }
        if market_cap_more_than is not None:
            params["marketCapMoreThan"] = int(market_cap_more_than)
        if market_cap_less_than is not None:
            params["marketCapLowerThan"] = int(market_cap_less_than)
        if price_more_than is not None:
            params["priceMoreThan"] = price_more_than
        if price_less_than is not None:
            params["priceLowerThan"] = price_less_than
        if volume_more_than is not None:
            params["volumeMoreThan"] = int(volume_more_than)
        if volume_less_than is not None:
            params["volumeLowerThan"] = int(volume_less_than)
        if beta_more_than is not None:
            params["betaMoreThan"] = beta_more_than
        if beta_less_than is not None:
            params["betaLowerThan"] = beta_less_than
        if dividend_more_than is not None:
            params["dividendMoreThan"] = dividend_more_than
        if dividend_less_than is not None:
            params["dividendLowerThan"] = dividend_less_than
        if sector:
            params["sector"] = sector
        if industry:
            params["industry"] = industry
        if country:
            params["country"] = country.upper()
        if exchange:
            params["exchange"] = exchange.lower()
        if is_etf is not None:
            params["isEtf"] = "true" if is_etf else "false"
        if is_fund is not None:
            params["isFund"] = "true" if is_fund else "false"
        if is_actively_trading is not None:
            params["isActivelyTrading"] = "true" if is_actively_trading else "false"

        try:
            data = await asyncio.to_thread(
                self._http_get_json,
                "https://financialmodelingprep.com/api/v3/stock-screener",
                params,
            )
        except requests.HTTPError as e:
            await self._emit(__event_emitter__, "Screening failed", done=True, user=__user__)
            return json.dumps({
                "error": f"FMP screener API error: {e}",
                "hint": "If this is a 403, your free tier may not include the screener endpoint, or you've hit the daily limit.",
            })
        except Exception as e:
            await self._emit(__event_emitter__, "Screening failed", done=True, user=__user__)
            return json.dumps({"error": f"Screener failed: {type(e).__name__}: {e}"})

        if not isinstance(data, list):
            return json.dumps({
                "error": "Unexpected response from FMP screener.",
                "raw": data,
            })

        results = []
        for item in data[: self.valves.screener_result_limit]:
            mc = _safe_float(item.get("marketCap"))
            results.append({
                "symbol": item.get("symbol"),
                "name": item.get("companyName"),
                "sector": item.get("sector"),
                "industry": item.get("industry"),
                "exchange": item.get("exchangeShortName") or item.get("exchange"),
                "country": item.get("country"),
                "price": _safe_float(item.get("price")),
                "market_cap": mc,
                "market_cap_formatted": _format_large_number(mc),
                "beta": _safe_float(item.get("beta")),
                "volume": _safe_int(item.get("volume")),
                "last_dividend": _safe_float(item.get("lastAnnualDividend")),
            })

        # Build a summary of which filters were applied for the model's context
        applied = {k: v for k, v in {
            "market_cap_more_than": market_cap_more_than,
            "market_cap_less_than": market_cap_less_than,
            "price_more_than": price_more_than,
            "price_less_than": price_less_than,
            "volume_more_than": volume_more_than,
            "volume_less_than": volume_less_than,
            "beta_more_than": beta_more_than,
            "beta_less_than": beta_less_than,
            "dividend_more_than": dividend_more_than,
            "dividend_less_than": dividend_less_than,
            "sector": sector,
            "industry": industry,
            "country": country,
            "exchange": exchange,
            "is_etf": is_etf,
            "is_fund": is_fund,
            "is_actively_trading": is_actively_trading,
        }.items() if v is not None}

        await self._emit(__event_emitter__, f"Found {len(results)} matches", done=True, user=__user__)
        return json.dumps({
            "filters_applied": applied,
            "count": len(results),
            "results": results,
        }, default=str)

    async def screen_by_fundamentals(
        self,
        cash_more_than: Optional[float] = None,
        cash_less_than: Optional[float] = None,
        debt_less_than: Optional[float] = None,
        revenue_more_than: Optional[float] = None,
        net_income_more_than: Optional[float] = None,
        pe_more_than: Optional[float] = None,
        pe_less_than: Optional[float] = None,
        pb_less_than: Optional[float] = None,
        ps_less_than: Optional[float] = None,
        roe_more_than: Optional[float] = None,
        gross_margin_more_than: Optional[float] = None,
        net_margin_more_than: Optional[float] = None,
        debt_to_equity_less_than: Optional[float] = None,
        current_ratio_more_than: Optional[float] = None,
        dividend_yield_more_than: Optional[float] = None,
        market_cap_more_than: Optional[float] = None,
        market_cap_less_than: Optional[float] = None,
        sector: Optional[str] = None,
        industry: Optional[str] = None,
        country: Optional[str] = None,
        exchange: Optional[str] = None,
        __user__: Optional[dict] = None,
        __event_emitter__: Optional[Callable[[dict], Awaitable[None]]] = None,
    ) -> str:
        """
        Advanced screener that filters companies by fundamental balance-sheet and
        valuation metrics — including cash on hand, debt, P/E ratio, ROE, margins, etc.

        Internally this works in two passes: it first screens by basic market criteria
        (market cap, sector, country, exchange) to narrow the universe, then fetches
        TTM key metrics for each candidate and applies the fundamental filters.

        Use this for queries like "find tech companies with more than $10B in cash and
        P/E under 20" or "small caps with positive ROE and low debt-to-equity".

        Note: This makes one API call per candidate company on top of the initial screen,
        so it consumes more API quota than screen_stocks. The screener_universe_size valve
        controls how many candidates are pulled before filtering (default 200).

        :param cash_more_than: Minimum cash and short-term investments in raw dollars (e.g. 10000000000 for $10B).
        :param cash_less_than: Maximum cash and short-term investments.
        :param debt_less_than: Maximum total debt in raw dollars.
        :param revenue_more_than: Minimum trailing-twelve-month revenue.
        :param net_income_more_than: Minimum trailing-twelve-month net income (profitability filter).
        :param pe_more_than: Minimum P/E ratio.
        :param pe_less_than: Maximum P/E ratio (e.g. 20 for "value" stocks).
        :param pb_less_than: Maximum price-to-book ratio.
        :param ps_less_than: Maximum price-to-sales ratio.
        :param roe_more_than: Minimum return on equity (as a decimal, e.g. 0.15 for 15%).
        :param gross_margin_more_than: Minimum gross margin (decimal, e.g. 0.40 for 40%).
        :param net_margin_more_than: Minimum net profit margin (decimal).
        :param debt_to_equity_less_than: Maximum debt-to-equity ratio.
        :param current_ratio_more_than: Minimum current ratio (liquidity filter; >1 typical).
        :param dividend_yield_more_than: Minimum dividend yield (decimal, e.g. 0.03 for 3%).
        :param market_cap_more_than: Minimum market cap to narrow the initial universe.
        :param market_cap_less_than: Maximum market cap.
        :param sector: Sector to limit the search to.
        :param industry: Industry to limit the search to.
        :param country: ISO-2 country code (e.g. "US").
        :param exchange: Exchange name (e.g. "nasdaq").
        :return: A JSON string with matching companies and the metrics that triggered the match.
        """
        if not self.valves.fmp_api_key:
            return json.dumps({
                "error": "Fundamental screening requires a Financial Modeling Prep (FMP) API key. "
                         "Configure 'fmp_api_key' in the tool valves. Free tier available at financialmodelingprep.com."
            })

        await self._emit(__event_emitter__, "Building candidate universe…", user=__user__)

        # Stage 1: Build a candidate universe via the basic screener using whatever
        # quick filters we have. We pull screener_universe_size candidates here.
        universe_params: dict[str, Any] = {
            "apikey": self.valves.fmp_api_key,
            "limit": self.valves.screener_universe_size,
            "isActivelyTrading": "true",
        }
        if market_cap_more_than is not None:
            universe_params["marketCapMoreThan"] = int(market_cap_more_than)
        if market_cap_less_than is not None:
            universe_params["marketCapLowerThan"] = int(market_cap_less_than)
        if sector:
            universe_params["sector"] = sector
        if industry:
            universe_params["industry"] = industry
        if country:
            universe_params["country"] = country.upper()
        if exchange:
            universe_params["exchange"] = exchange.lower()

        try:
            universe = await asyncio.to_thread(
                self._http_get_json,
                "https://financialmodelingprep.com/api/v3/stock-screener",
                universe_params,
            )
        except Exception as e:
            await self._emit(__event_emitter__, "Universe build failed", done=True, user=__user__)
            return json.dumps({"error": f"Universe screener failed: {type(e).__name__}: {e}"})

        if not isinstance(universe, list) or not universe:
            await self._emit(__event_emitter__, "No candidates found", done=True, user=__user__)
            return json.dumps({
                "filters_applied": {},
                "count": 0,
                "results": [],
                "note": "No companies matched the initial market-cap/sector/country filters.",
            })

        candidate_count = len(universe)
        await self._emit(
            __event_emitter__,
            f"Filtering {candidate_count} candidates by fundamentals…",
            user=__user__,
        )

        # Stage 2: Fetch TTM key metrics for each candidate and filter.
        # We do this concurrently with a bounded thread pool to keep it reasonably fast
        # while respecting FMP's rate limits.
        sem_limit = 5  # concurrent in-flight requests
        sem = asyncio.Semaphore(sem_limit)

        async def fetch_metrics(symbol: str) -> Optional[dict]:
            async with sem:
                try:
                    return await asyncio.to_thread(
                        self._http_get_json,
                        f"https://financialmodelingprep.com/api/v3/key-metrics-ttm/{symbol}",
                        {"apikey": self.valves.fmp_api_key},
                    )
                except Exception:
                    return None

        symbols = [u.get("symbol") for u in universe if u.get("symbol")]
        metric_responses = await asyncio.gather(*[fetch_metrics(s) for s in symbols])

        # Build a quick lookup: symbol -> universe row (for company name/price)
        universe_by_symbol = {u.get("symbol"): u for u in universe}

        results: list[dict] = []
        for symbol, metric_resp in zip(symbols, metric_responses):
            if not metric_resp or not isinstance(metric_resp, list) or not metric_resp[0]:
                continue
            m = metric_resp[0]

            # Extract TTM values; FMP names are camelCase TTM fields
            cash = _safe_float(m.get("cashAndShortTermInvestmentsTTM") or m.get("cashAndCashEquivalentsTTM"))
            debt = _safe_float(m.get("totalDebtTTM"))
            pe = _safe_float(m.get("peRatioTTM"))
            pb = _safe_float(m.get("pbRatioTTM"))
            ps = _safe_float(m.get("priceToSalesRatioTTM"))
            roe = _safe_float(m.get("roeTTM"))
            gross_margin = _safe_float(m.get("grossProfitMarginTTM"))
            net_margin = _safe_float(m.get("netProfitMarginTTM"))
            debt_to_equity = _safe_float(m.get("debtToEquityTTM"))
            current_ratio = _safe_float(m.get("currentRatioTTM"))
            dividend_yield = _safe_float(m.get("dividendYieldTTM"))
            market_cap = _safe_float(m.get("marketCapTTM"))

            # Apply each filter. None means "skip this filter".
            def fail_min(value: Optional[float], threshold: Optional[float]) -> bool:
                return threshold is not None and (value is None or value < threshold)

            def fail_max(value: Optional[float], threshold: Optional[float]) -> bool:
                return threshold is not None and (value is None or value > threshold)

            if fail_min(cash, cash_more_than):
                continue
            if fail_max(cash, cash_less_than):
                continue
            if fail_max(debt, debt_less_than):
                continue
            if fail_min(pe, pe_more_than):
                continue
            if fail_max(pe, pe_less_than):
                continue
            if fail_max(pb, pb_less_than):
                continue
            if fail_max(ps, ps_less_than):
                continue
            if fail_min(roe, roe_more_than):
                continue
            if fail_min(gross_margin, gross_margin_more_than):
                continue
            if fail_min(net_margin, net_margin_more_than):
                continue
            if fail_max(debt_to_equity, debt_to_equity_less_than):
                continue
            if fail_min(current_ratio, current_ratio_more_than):
                continue
            if fail_min(dividend_yield, dividend_yield_more_than):
                continue

            # Revenue and net income filters require an extra income-statement call.
            # Do it lazily — only for candidates that have already passed all other filters.
            if revenue_more_than is not None or net_income_more_than is not None:
                try:
                    inc = await asyncio.to_thread(
                        self._http_get_json,
                        f"https://financialmodelingprep.com/api/v3/income-statement/{symbol}",
                        {"apikey": self.valves.fmp_api_key, "limit": 1, "period": "annual"},
                    )
                    row = inc[0] if (inc and isinstance(inc, list)) else {}
                    actual_rev = _safe_float(row.get("revenue"))
                    actual_ni = _safe_float(row.get("netIncome"))
                    if revenue_more_than is not None and (actual_rev is None or actual_rev < revenue_more_than):
                        continue
                    if net_income_more_than is not None and (actual_ni is None or actual_ni < net_income_more_than):
                        continue
                except Exception:
                    continue

            u_row = universe_by_symbol.get(symbol, {})
            results.append({
                "symbol": symbol,
                "name": u_row.get("companyName"),
                "sector": u_row.get("sector"),
                "industry": u_row.get("industry"),
                "exchange": u_row.get("exchangeShortName") or u_row.get("exchange"),
                "price": _safe_float(u_row.get("price")),
                "market_cap": market_cap,
                "market_cap_formatted": _format_large_number(market_cap),
                "metrics": {
                    "cash_and_st_investments": cash,
                    "cash_formatted": _format_large_number(cash),
                    "total_debt": debt,
                    "total_debt_formatted": _format_large_number(debt),
                    "pe_ratio": pe,
                    "pb_ratio": pb,
                    "ps_ratio": ps,
                    "roe": roe,
                    "gross_margin": gross_margin,
                    "net_margin": net_margin,
                    "debt_to_equity": debt_to_equity,
                    "current_ratio": current_ratio,
                    "dividend_yield": dividend_yield,
                },
            })

            # Stop early once we have enough matches
            if len(results) >= self.valves.screener_result_limit:
                break

        applied_filters = {k: v for k, v in {
            "cash_more_than": cash_more_than,
            "cash_less_than": cash_less_than,
            "debt_less_than": debt_less_than,
            "revenue_more_than": revenue_more_than,
            "net_income_more_than": net_income_more_than,
            "pe_more_than": pe_more_than,
            "pe_less_than": pe_less_than,
            "pb_less_than": pb_less_than,
            "ps_less_than": ps_less_than,
            "roe_more_than": roe_more_than,
            "gross_margin_more_than": gross_margin_more_than,
            "net_margin_more_than": net_margin_more_than,
            "debt_to_equity_less_than": debt_to_equity_less_than,
            "current_ratio_more_than": current_ratio_more_than,
            "dividend_yield_more_than": dividend_yield_more_than,
            "market_cap_more_than": market_cap_more_than,
            "market_cap_less_than": market_cap_less_than,
            "sector": sector,
            "industry": industry,
            "country": country,
            "exchange": exchange,
        }.items() if v is not None}

        await self._emit(
            __event_emitter__,
            f"Found {len(results)} matches from {candidate_count} candidates",
            done=True,
            user=__user__,
        )

        return json.dumps({
            "filters_applied": applied_filters,
            "candidates_screened": candidate_count,
            "count": len(results),
            "results": results,
            "note": "Metrics shown are TTM (trailing twelve months). Margins/yields/ROE are decimals (0.15 = 15%).",
        }, default=str)

    # ===================================================================
    #                       PROVIDER: FINNHUB
    # ===================================================================

    def _finnhub_require_key(self) -> str:
        if not self.valves.finnhub_api_key:
            raise RuntimeError("Finnhub API key not configured.")
        return self.valves.finnhub_api_key

    def _finnhub_quote(self, symbol: str) -> Optional[dict]:
        token = self._finnhub_require_key()
        data = self._http_get_json(
            "https://finnhub.io/api/v1/quote",
            {"symbol": symbol, "token": token},
        )
        # finnhub returns 0s if symbol unknown
        if not data or all(v in (0, None) for v in (data.get("c"), data.get("o"), data.get("h"))):
            return None
        current = _safe_float(data.get("c"))
        prev_close = _safe_float(data.get("pc"))
        change = (current - prev_close) if (current is not None and prev_close is not None) else None
        change_pct = ((change / prev_close) * 100) if (change is not None and prev_close) else None
        return {
            "provider": "finnhub",
            "symbol": symbol,
            "price": current,
            "change": round(change, 4) if change is not None else None,
            "change_percent": round(change_pct, 4) if change_pct is not None else None,
            "open": _safe_float(data.get("o")),
            "high": _safe_float(data.get("h")),
            "low": _safe_float(data.get("l")),
            "previous_close": prev_close,
            "timestamp": _ts_to_iso(data.get("t")),
        }

    def _finnhub_profile(self, symbol: str) -> Optional[dict]:
        token = self._finnhub_require_key()
        profile = self._http_get_json(
            "https://finnhub.io/api/v1/stock/profile2",
            {"symbol": symbol, "token": token},
        )
        if not profile:
            return None

        metrics = {}
        try:
            metrics_data = self._http_get_json(
                "https://finnhub.io/api/v1/stock/metric",
                {"symbol": symbol, "metric": "all", "token": token},
            )
            metrics = (metrics_data or {}).get("metric") or {}
        except Exception:
            metrics = {}

        market_cap_m = _safe_float(profile.get("marketCapitalization"))
        market_cap = market_cap_m * 1_000_000 if market_cap_m else None

        return {
            "provider": "finnhub",
            "symbol": symbol,
            "name": profile.get("name"),
            "exchange": profile.get("exchange"),
            "country": profile.get("country"),
            "currency": profile.get("currency"),
            "industry": profile.get("finnhubIndustry"),
            "ipo_date": profile.get("ipo"),
            "logo": profile.get("logo"),
            "weburl": profile.get("weburl"),
            "phone": profile.get("phone"),
            "share_outstanding_millions": _safe_float(profile.get("shareOutstanding")),
            "market_cap": market_cap,
            "market_cap_formatted": _format_large_number(market_cap),
            "key_metrics": {
                "pe_ttm": _safe_float(metrics.get("peTTM")),
                "ps_ttm": _safe_float(metrics.get("psTTM")),
                "pb_ratio": _safe_float(metrics.get("pbAnnual")),
                "eps_ttm": _safe_float(metrics.get("epsTTM")),
                "dividend_yield_ttm_percent": _safe_float(metrics.get("dividendYieldIndicatedAnnual")),
                "beta": _safe_float(metrics.get("beta")),
                "52_week_high": _safe_float(metrics.get("52WeekHigh")),
                "52_week_low": _safe_float(metrics.get("52WeekLow")),
                "52_week_price_return_daily": _safe_float(metrics.get("52WeekPriceReturnDaily")),
                "roe_ttm": _safe_float(metrics.get("roeTTM")),
                "roa_ttm": _safe_float(metrics.get("roaTTM")),
                "current_ratio_annual": _safe_float(metrics.get("currentRatioAnnual")),
                "debt_to_equity_annual": _safe_float(metrics.get("totalDebt/totalEquityAnnual")),
                "gross_margin_ttm_percent": _safe_float(metrics.get("grossMarginTTM")),
                "operating_margin_ttm_percent": _safe_float(metrics.get("operatingMarginTTM")),
                "net_margin_ttm_percent": _safe_float(metrics.get("netProfitMarginTTM")),
            },
        }

    def _finnhub_financials(
        self, symbol: str, statement: str, period: str
    ) -> Optional[dict]:
        token = self._finnhub_require_key()
        # Finnhub's reported financials endpoint
        freq = "annual" if period == "annual" else "quarterly"
        data = self._http_get_json(
            "https://finnhub.io/api/v1/stock/financials-reported",
            {"symbol": symbol, "freq": freq, "token": token},
        )
        if not data or not data.get("data"):
            return None

        statement_key = {
            "income": "ic",
            "balance": "bs",
            "cashflow": "cf",
        }[statement]

        periods = []
        for entry in data["data"][: self.valves.max_financial_periods]:
            report = (entry.get("report") or {}).get(statement_key) or []
            simplified = {item.get("label") or item.get("concept"): item.get("value") for item in report if item}
            periods.append({
                "period_end": entry.get("endDate"),
                "year": entry.get("year"),
                "quarter": entry.get("quarter"),
                "form": entry.get("form"),
                "data": simplified,
            })

        return {
            "provider": "finnhub",
            "symbol": symbol,
            "statement": statement,
            "period": period,
            "periods": periods,
        }

    def _finnhub_earnings(self, symbol: str) -> Optional[dict]:
        token = self._finnhub_require_key()
        data = self._http_get_json(
            "https://finnhub.io/api/v1/stock/earnings",
            {"symbol": symbol, "token": token},
        )
        if not data:
            return None

        rows = []
        for row in data[:8]:  # last 8 quarters
            actual = _safe_float(row.get("actual"))
            estimate = _safe_float(row.get("estimate"))
            surprise = _safe_float(row.get("surprise"))
            surprise_pct = _safe_float(row.get("surprisePercent"))
            rows.append({
                "period": row.get("period"),
                "year": row.get("year"),
                "quarter": row.get("quarter"),
                "actual_eps": actual,
                "estimated_eps": estimate,
                "surprise": surprise,
                "surprise_percent": surprise_pct,
            })

        return {
            "provider": "finnhub",
            "symbol": symbol,
            "earnings": rows,
        }

    def _finnhub_news(self, symbol: str) -> Optional[dict]:
        token = self._finnhub_require_key()
        # last ~7 days of news
        from datetime import date, timedelta
        today = date.today()
        from_date = (today - timedelta(days=7)).isoformat()
        to_date = today.isoformat()
        data = self._http_get_json(
            "https://finnhub.io/api/v1/company-news",
            {"symbol": symbol, "from": from_date, "to": to_date, "token": token},
        )
        if not data:
            return None

        articles = []
        for item in data[: self.valves.max_news_items]:
            articles.append({
                "headline": item.get("headline"),
                "source": item.get("source"),
                "summary": (item.get("summary") or "")[:500],
                "url": item.get("url"),
                "published": _ts_to_iso(item.get("datetime")),
                "category": item.get("category"),
            })
        return {
            "provider": "finnhub",
            "symbol": symbol,
            "from_date": from_date,
            "to_date": to_date,
            "count": len(articles),
            "articles": articles,
        }

    def _finnhub_recommendations(self, symbol: str) -> Optional[dict]:
        token = self._finnhub_require_key()
        data = self._http_get_json(
            "https://finnhub.io/api/v1/stock/recommendation",
            {"symbol": symbol, "token": token},
        )
        if not data:
            return None
        rows = []
        for row in data[:6]:
            rows.append({
                "period": row.get("period"),
                "strong_buy": row.get("strongBuy"),
                "buy": row.get("buy"),
                "hold": row.get("hold"),
                "sell": row.get("sell"),
                "strong_sell": row.get("strongSell"),
            })
        return {
            "provider": "finnhub",
            "symbol": symbol,
            "recommendations": rows,
        }

    # ===================================================================
    #                       PROVIDER: YFINANCE
    # ===================================================================

    def _yfinance_ticker(self, symbol: str):
        import yfinance as yf
        return yf.Ticker(symbol)

    def _yfinance_quote(self, symbol: str) -> Optional[dict]:
        ticker = self._yfinance_ticker(symbol)
        try:
            fast = ticker.fast_info or {}
        except Exception:
            fast = {}
        try:
            info = ticker.info or {}
        except Exception:
            info = {}

        price = _safe_float(fast.get("last_price") or info.get("regularMarketPrice") or info.get("currentPrice"))
        prev_close = _safe_float(fast.get("previous_close") or info.get("regularMarketPreviousClose") or info.get("previousClose"))
        if price is None and prev_close is None:
            return None

        change = (price - prev_close) if (price is not None and prev_close is not None) else None
        change_pct = ((change / prev_close) * 100) if (change is not None and prev_close) else None

        return {
            "provider": "yfinance",
            "symbol": symbol,
            "price": price,
            "change": round(change, 4) if change is not None else None,
            "change_percent": round(change_pct, 4) if change_pct is not None else None,
            "open": _safe_float(fast.get("open") or info.get("regularMarketOpen") or info.get("open")),
            "high": _safe_float(fast.get("day_high") or info.get("regularMarketDayHigh") or info.get("dayHigh")),
            "low": _safe_float(fast.get("day_low") or info.get("regularMarketDayLow") or info.get("dayLow")),
            "previous_close": prev_close,
            "volume": _safe_int(fast.get("last_volume") or info.get("regularMarketVolume") or info.get("volume")),
            "currency": fast.get("currency") or info.get("currency"),
            "timestamp": datetime.now(tz=timezone.utc).isoformat(),
        }

    def _yfinance_profile(self, symbol: str) -> Optional[dict]:
        ticker = self._yfinance_ticker(symbol)
        try:
            info = ticker.info or {}
        except Exception:
            info = {}
        if not info or not (info.get("longName") or info.get("shortName") or info.get("symbol")):
            return None

        market_cap = _safe_float(info.get("marketCap"))
        return {
            "provider": "yfinance",
            "symbol": symbol,
            "name": info.get("longName") or info.get("shortName"),
            "exchange": info.get("exchange") or info.get("fullExchangeName"),
            "country": info.get("country"),
            "currency": info.get("currency") or info.get("financialCurrency"),
            "sector": info.get("sector"),
            "industry": info.get("industry"),
            "website": info.get("website"),
            "employees": _safe_int(info.get("fullTimeEmployees")),
            "summary": (info.get("longBusinessSummary") or "")[:1000] or None,
            "market_cap": market_cap,
            "market_cap_formatted": _format_large_number(market_cap),
            "key_metrics": {
                "pe_trailing": _safe_float(info.get("trailingPE")),
                "pe_forward": _safe_float(info.get("forwardPE")),
                "ps_ttm": _safe_float(info.get("priceToSalesTrailing12Months")),
                "pb_ratio": _safe_float(info.get("priceToBook")),
                "eps_trailing": _safe_float(info.get("trailingEps")),
                "eps_forward": _safe_float(info.get("forwardEps")),
                "dividend_yield_percent": (
                    _safe_float(info.get("dividendYield")) * 100
                    if _safe_float(info.get("dividendYield")) is not None and _safe_float(info.get("dividendYield")) < 1
                    else _safe_float(info.get("dividendYield"))
                ),
                "dividend_rate": _safe_float(info.get("dividendRate")),
                "beta": _safe_float(info.get("beta")),
                "52_week_high": _safe_float(info.get("fiftyTwoWeekHigh")),
                "52_week_low": _safe_float(info.get("fiftyTwoWeekLow")),
                "50_day_avg": _safe_float(info.get("fiftyDayAverage")),
                "200_day_avg": _safe_float(info.get("twoHundredDayAverage")),
                "profit_margin_percent": (
                    _safe_float(info.get("profitMargins")) * 100
                    if _safe_float(info.get("profitMargins")) is not None
                    else None
                ),
                "operating_margin_percent": (
                    _safe_float(info.get("operatingMargins")) * 100
                    if _safe_float(info.get("operatingMargins")) is not None
                    else None
                ),
                "return_on_equity_percent": (
                    _safe_float(info.get("returnOnEquity")) * 100
                    if _safe_float(info.get("returnOnEquity")) is not None
                    else None
                ),
                "debt_to_equity": _safe_float(info.get("debtToEquity")),
                "revenue_ttm": _safe_float(info.get("totalRevenue")),
                "ebitda": _safe_float(info.get("ebitda")),
                "shares_outstanding": _safe_int(info.get("sharesOutstanding")),
            },
        }

    def _yfinance_financials(
        self, symbol: str, statement: str, period: str
    ) -> Optional[dict]:
        ticker = self._yfinance_ticker(symbol)
        # Pick the right yfinance dataframe
        try:
            if statement == "income":
                df = ticker.quarterly_income_stmt if period == "quarterly" else ticker.income_stmt
            elif statement == "balance":
                df = ticker.quarterly_balance_sheet if period == "quarterly" else ticker.balance_sheet
            else:
                df = ticker.quarterly_cashflow if period == "quarterly" else ticker.cashflow
        except Exception:
            return None

        if df is None or df.empty:
            return None

        # Take the first N most recent columns (yfinance returns most-recent first)
        df = df.iloc[:, : self.valves.max_financial_periods]

        periods = []
        for col in df.columns:
            col_label = col.strftime("%Y-%m-%d") if hasattr(col, "strftime") else str(col)
            data_dict: dict[str, Any] = {}
            for row_label in df.index:
                val = df.at[row_label, col]
                # pandas NaN check without importing pandas
                if val is None:
                    continue
                try:
                    if val != val:  # NaN check
                        continue
                except Exception:
                    pass
                try:
                    data_dict[str(row_label)] = float(val)
                except (TypeError, ValueError):
                    data_dict[str(row_label)] = str(val)
            periods.append({"period_end": col_label, "data": data_dict})

        return {
            "provider": "yfinance",
            "symbol": symbol,
            "statement": statement,
            "period": period,
            "periods": periods,
        }

    def _yfinance_earnings(self, symbol: str) -> Optional[dict]:
        ticker = self._yfinance_ticker(symbol)
        rows = []
        try:
            df = ticker.earnings_history
            if df is not None and not df.empty:
                df = df.iloc[: self.valves.max_financial_periods * 2]
                for idx, row in df.iterrows():
                    period_label = idx.strftime("%Y-%m-%d") if hasattr(idx, "strftime") else str(idx)
                    rows.append({
                        "period": period_label,
                        "actual_eps": _safe_float(row.get("epsActual")),
                        "estimated_eps": _safe_float(row.get("epsEstimate")),
                        "surprise": _safe_float(row.get("epsDifference")),
                        "surprise_percent": _safe_float(row.get("surprisePercent")),
                    })
        except Exception:
            pass

        if not rows:
            # Fall back to .income_stmt EPS rows if earnings_history missing
            try:
                df = ticker.quarterly_income_stmt
                if df is not None and not df.empty and "Diluted EPS" in df.index:
                    for col in df.columns[: self.valves.max_financial_periods]:
                        rows.append({
                            "period": col.strftime("%Y-%m-%d") if hasattr(col, "strftime") else str(col),
                            "actual_eps": _safe_float(df.at["Diluted EPS", col]),
                            "estimated_eps": None,
                            "surprise": None,
                            "surprise_percent": None,
                        })
            except Exception:
                pass

        if not rows:
            return None

        return {
            "provider": "yfinance",
            "symbol": symbol,
            "earnings": rows,
        }

    def _yfinance_news(self, symbol: str) -> Optional[dict]:
        ticker = self._yfinance_ticker(symbol)
        try:
            news = ticker.news or []
        except Exception:
            return None
        if not news:
            return None

        articles = []
        for item in news[: self.valves.max_news_items]:
            # yfinance news shape varies; handle both old and new schemas
            content = item.get("content") if isinstance(item, dict) else None
            if content:
                pub_ts = content.get("pubDate") or content.get("displayTime")
                published = pub_ts if isinstance(pub_ts, str) else _ts_to_iso(pub_ts)
                articles.append({
                    "headline": content.get("title"),
                    "source": (content.get("provider") or {}).get("displayName"),
                    "summary": (content.get("summary") or "")[:500],
                    "url": (content.get("canonicalUrl") or {}).get("url") or (content.get("clickThroughUrl") or {}).get("url"),
                    "published": published,
                })
            else:
                articles.append({
                    "headline": item.get("title"),
                    "source": item.get("publisher"),
                    "summary": "",
                    "url": item.get("link"),
                    "published": _ts_to_iso(item.get("providerPublishTime")),
                })

        return {
            "provider": "yfinance",
            "symbol": symbol,
            "count": len(articles),
            "articles": articles,
        }

    def _yfinance_recommendations(self, symbol: str) -> Optional[dict]:
        ticker = self._yfinance_ticker(symbol)
        try:
            df = ticker.recommendations
        except Exception:
            return None
        if df is None or df.empty:
            return None

        rows = []
        for _, row in df.head(6).iterrows():
            rows.append({
                "period": row.get("period"),
                "strong_buy": _safe_int(row.get("strongBuy")),
                "buy": _safe_int(row.get("buy")),
                "hold": _safe_int(row.get("hold")),
                "sell": _safe_int(row.get("sell")),
                "strong_sell": _safe_int(row.get("strongSell")),
            })
        return {
            "provider": "yfinance",
            "symbol": symbol,
            "recommendations": rows,
        }

    # ===================================================================
    #                       PROVIDER: FMP
    # ===================================================================

    def _fmp_require_key(self) -> str:
        if not self.valves.fmp_api_key:
            raise RuntimeError("FMP API key not configured.")
        return self.valves.fmp_api_key

    def _fmp_quote(self, symbol: str) -> Optional[dict]:
        key = self._fmp_require_key()
        data = self._http_get_json(
            f"https://financialmodelingprep.com/api/v3/quote/{symbol}",
            {"apikey": key},
        )
        if not data or not isinstance(data, list):
            return None
        q = data[0]
        return {
            "provider": "fmp",
            "symbol": symbol,
            "name": q.get("name"),
            "price": _safe_float(q.get("price")),
            "change": _safe_float(q.get("change")),
            "change_percent": _safe_float(q.get("changesPercentage")),
            "open": _safe_float(q.get("open")),
            "high": _safe_float(q.get("dayHigh")),
            "low": _safe_float(q.get("dayLow")),
            "previous_close": _safe_float(q.get("previousClose")),
            "volume": _safe_int(q.get("volume")),
            "avg_volume": _safe_int(q.get("avgVolume")),
            "market_cap": _safe_float(q.get("marketCap")),
            "market_cap_formatted": _format_large_number(_safe_float(q.get("marketCap"))),
            "pe": _safe_float(q.get("pe")),
            "eps": _safe_float(q.get("eps")),
            "52_week_high": _safe_float(q.get("yearHigh")),
            "52_week_low": _safe_float(q.get("yearLow")),
            "exchange": q.get("exchange"),
            "timestamp": _ts_to_iso(q.get("timestamp")),
        }

    def _fmp_profile(self, symbol: str) -> Optional[dict]:
        key = self._fmp_require_key()
        data = self._http_get_json(
            f"https://financialmodelingprep.com/api/v3/profile/{symbol}",
            {"apikey": key},
        )
        if not data or not isinstance(data, list):
            return None
        p = data[0]
        market_cap = _safe_float(p.get("mktCap"))
        return {
            "provider": "fmp",
            "symbol": symbol,
            "name": p.get("companyName"),
            "exchange": p.get("exchangeShortName"),
            "country": p.get("country"),
            "currency": p.get("currency"),
            "sector": p.get("sector"),
            "industry": p.get("industry"),
            "website": p.get("website"),
            "employees": _safe_int(p.get("fullTimeEmployees")),
            "ipo_date": p.get("ipoDate"),
            "summary": (p.get("description") or "")[:1000] or None,
            "ceo": p.get("ceo"),
            "market_cap": market_cap,
            "market_cap_formatted": _format_large_number(market_cap),
            "key_metrics": {
                "price": _safe_float(p.get("price")),
                "beta": _safe_float(p.get("beta")),
                "volume_avg": _safe_int(p.get("volAvg")),
                "last_dividend": _safe_float(p.get("lastDiv")),
                "range": p.get("range"),
                "dcf": _safe_float(p.get("dcf")),
                "dcf_diff": _safe_float(p.get("dcfDiff")),
            },
        }

    def _fmp_financials(self, symbol: str, statement: str, period: str) -> Optional[dict]:
        key = self._fmp_require_key()
        endpoint = {
            "income": "income-statement",
            "balance": "balance-sheet-statement",
            "cashflow": "cash-flow-statement",
        }[statement]
        params = {"apikey": key, "limit": self.valves.max_financial_periods}
        if period == "quarterly":
            params["period"] = "quarter"
        data = self._http_get_json(
            f"https://financialmodelingprep.com/api/v3/{endpoint}/{symbol}",
            params,
        )
        if not data or not isinstance(data, list):
            return None

        periods = []
        for entry in data[: self.valves.max_financial_periods]:
            # Strip noisy meta fields, keep numerical items
            cleaned = {
                k: v
                for k, v in entry.items()
                if k not in ("symbol", "reportedCurrency", "cik", "fillingDate",
                             "acceptedDate", "calendarYear", "link", "finalLink")
            }
            periods.append({
                "period_end": entry.get("date"),
                "fiscal_year": entry.get("calendarYear"),
                "period_label": entry.get("period"),
                "currency": entry.get("reportedCurrency"),
                "data": cleaned,
            })

        return {
            "provider": "fmp",
            "symbol": symbol,
            "statement": statement,
            "period": period,
            "periods": periods,
        }

    def _fmp_earnings(self, symbol: str) -> Optional[dict]:
        key = self._fmp_require_key()
        data = self._http_get_json(
            f"https://financialmodelingprep.com/api/v3/historical/earning_calendar/{symbol}",
            {"apikey": key, "limit": 8},
        )
        if not data or not isinstance(data, list):
            return None

        rows = []
        for row in data[:8]:
            actual = _safe_float(row.get("eps"))
            estimate = _safe_float(row.get("epsEstimated"))
            surprise = (actual - estimate) if (actual is not None and estimate is not None) else None
            surprise_pct = (
                (surprise / estimate * 100)
                if (surprise is not None and estimate not in (None, 0))
                else None
            )
            rows.append({
                "period": row.get("date"),
                "actual_eps": actual,
                "estimated_eps": estimate,
                "surprise": round(surprise, 4) if surprise is not None else None,
                "surprise_percent": round(surprise_pct, 4) if surprise_pct is not None else None,
                "revenue_actual": _safe_float(row.get("revenue")),
                "revenue_estimated": _safe_float(row.get("revenueEstimated")),
            })

        return {
            "provider": "fmp",
            "symbol": symbol,
            "earnings": rows,
        }