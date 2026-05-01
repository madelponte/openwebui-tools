# Stock Data — Open WebUI Tool

A native [Open WebUI](https://openwebui.com/) tool that lets your local models query stock market data — quotes, fundamentals, financial statements, earnings, news, analyst recommendations, and stock screening.

The tool is built around a hybrid provider strategy that works out-of-the-box with no API keys (via `yfinance`) but unlocks more reliable, higher-rate-limit data when you provide free API keys for [Finnhub](https://finnhub.io/) and/or [Financial Modeling Prep](https://site.financialmodelingprep.com/).

---

## Features

- **Stock quotes** — current price, day's change, OHLC, volume, previous close
- **Company profiles** — name, sector, industry, market cap, employees, ~15 fundamental metrics (P/E, P/B, dividend yield, beta, 52-week range, margins, ROE, debt-to-equity, etc.)
- **Financial statements** — income statement, balance sheet, and cash flow (annual or quarterly)
- **Earnings history** — actual vs. estimated EPS with surprise %, plus revenue actuals/estimates where available
- **Company news** — recent articles with headline, source, summary, and URL
- **Analyst recommendations** — buy/hold/sell distribution over recent months
- **Symbol search** — find tickers by company name
- **Stock screening** — filter the market by basic criteria (market cap, price, volume, sector, country, etc.)
- **Fundamental screening** — filter by deeper metrics (cash on hand, P/E, ROE, margins, debt-to-equity, current ratio, etc.)

All public methods are async and emit progress status events to the chat UI while they work.

---

## Installation

1. Open your Open WebUI instance.
2. Go to **Workspace → Tools** and click **+** (Import / New Tool).
3. Paste the contents of `stock_data_tool.py` into the editor, or upload the file.
4. Save it.
5. Enable the tool on whichever model(s) you want to use it with: **Workspace → Models → [edit a model] → Tools section → check "Stock Data" → Save**.

The tool depends on the `requests` and `yfinance` Python packages, which are declared in its metadata header. Open WebUI will install them automatically on first use, but for production deployments it's better to bake them into your container image:

```dockerfile
FROM ghcr.io/open-webui/open-webui:main
RUN pip install --no-cache-dir requests yfinance
```

---

## Configuration

The tool exposes both **Valves** (admin-configurable, set once via the Tools panel) and **UserValves** (per-user, set from the chat settings panel).

### Valves (admin)

| Valve | Default | Purpose |
|-------|---------|---------|
| `finnhub_api_key` | *(empty)* | Finnhub API key. **Recommended.** Free at [finnhub.io](https://finnhub.io/) — gives 60 calls/min on the free tier and powers quotes, profiles, news, earnings, and recommendations. |
| `fmp_api_key` | *(empty)* | Financial Modeling Prep API key. **Required for stock screening.** Free at [financialmodelingprep.com](https://site.financialmodelingprep.com/) — ~250 calls/day on the free tier. Also used for deep financial statements when set. |
| `alpha_vantage_api_key` | *(empty)* | Reserved for future use. |
| `default_provider` | `auto` | Provider for quotes, profiles, news, earnings, recommendations. One of `auto`, `finnhub`, `yfinance`, `fmp`. `auto` picks Finnhub if configured, else yfinance. |
| `financials_provider` | `auto` | Provider for financial statements. One of `auto`, `fmp`, `yfinance`, `finnhub`. `auto` picks FMP if configured, else yfinance. |
| `prefer_yfinance_fallback` | `true` | If the primary provider fails or returns nothing, automatically retry with yfinance. |
| `request_timeout` | `15` | HTTP request timeout in seconds. |
| `cache_ttl_seconds` | `60` | In-memory cache TTL for API responses. Set to `0` to disable caching. |
| `max_news_items` | `5` | Maximum news articles to return per query. |
| `max_financial_periods` | `4` | Maximum number of historical periods (years/quarters) returned by financial statements. |
| `screener_result_limit` | `25` | Maximum stocks returned by screener tools. |
| `screener_universe_size` | `200` | When screening by fundamentals, how large an initial universe to pull from FMP before applying fundamental filters. Larger = more thorough, but uses more API quota. |

### UserValves (per-user)

| Valve | Default | Purpose |
|-------|---------|---------|
| `verbose_status` | `true` | Show progress status messages while the tool fetches data. |
| `include_raw_numbers` | `false` | Include raw numeric values alongside human-readable formatting. |

### Recommended setup

- **No keys, just want it to work:** leave everything default. The tool falls back to yfinance for everything except screening (which requires FMP).
- **Best free experience:** sign up for free Finnhub and FMP keys, paste them into the corresponding valves. Leave the providers on `auto`.
- **High-volume use:** consider FMP's paid plans, and increase `cache_ttl_seconds` to reduce duplicate calls.

---

## Provider strategy

The tool ships with three data providers, each with different strengths:

| Provider | Free tier | Strengths | Weaknesses |
|----------|-----------|-----------|------------|
| **Finnhub** | 60 calls/min | Real-time quotes, clean profiles, structured earnings, news, analyst recs | Free tier limits historical depth; financial statements are usable but not as clean as FMP |
| **yfinance** | Unlimited (no key) | Wide coverage, no signup, decent fundamentals | Unofficial Yahoo scraping — can break when Yahoo changes its site, not licensed for commercial use |
| **FMP** | ~250 calls/day | Cleanest financial statements, the only free screener endpoint | Daily limit is tight, paid plans start at ~$15/month |

The `auto` mode picks the best available provider based on which keys you've configured. If a provider call fails and `prefer_yfinance_fallback` is on (default), the tool transparently retries with yfinance.

---

## Available methods

The model invokes these as function calls. All return JSON strings.

### `get_stock_quote(symbol)`
Returns current price, change, OHLC, volume, previous close, and timestamp.

### `get_company_profile(symbol)`
Returns name, exchange, country, currency, sector, industry, market cap, employees, and a `key_metrics` block with P/E, P/B, P/S, EPS, dividend yield, beta, 52-week range, margins, ROE, ROA, debt-to-equity, etc.

### `get_financials(symbol, statement="income", period="annual")`
Returns the most recent N periods of the requested statement.
- `statement`: `"income"`, `"balance"`, or `"cashflow"`
- `period`: `"annual"` or `"quarterly"`

### `get_earnings(symbol)`
Returns the last 8 quarters of earnings — actual EPS, estimated EPS, surprise, surprise %, plus revenue figures when available.

### `get_company_news(symbol)`
Returns the most recent news articles with headline, source, summary, URL, and published date.

### `get_analyst_recommendations(symbol)`
Returns the recent breakdown of analyst ratings — strong buy, buy, hold, sell, strong sell — over the last several months.

### `search_symbol(query)`
Finds tickers by company name or partial symbol. Useful when the user names a company without giving the ticker. Requires a Finnhub API key.

### `screen_stocks(...)`
Filters the market by basic criteria. All parameters are optional — only specified ones are applied.

Supported parameters:
- `market_cap_more_than`, `market_cap_less_than` (raw dollars, e.g. `10_000_000_000` for $10B)
- `price_more_than`, `price_less_than`
- `volume_more_than`, `volume_less_than`
- `beta_more_than`, `beta_less_than`
- `dividend_more_than`, `dividend_less_than`
- `sector` (e.g. `"Technology"`, `"Healthcare"`)
- `industry` (e.g. `"Software"`, `"Banks"`)
- `country` (ISO-2 code, e.g. `"US"`, `"CA"`, `"JP"`)
- `exchange` (e.g. `"nasdaq"`, `"nyse"`, `"tsx"`)
- `is_etf`, `is_fund`, `is_actively_trading`

**Requires an FMP API key.** Costs 1 API call per invocation.

### `screen_by_fundamentals(...)`
Advanced screener that filters by balance-sheet and valuation metrics. Works in two passes: first narrows a candidate universe via the basic screener, then fetches TTM key metrics for each candidate and applies the fundamental filters.

Supported fundamental filters:
- `cash_more_than`, `cash_less_than` (cash and short-term investments, raw dollars)
- `debt_less_than` (total debt)
- `revenue_more_than` (TTM revenue)
- `net_income_more_than` (TTM net income)
- `pe_more_than`, `pe_less_than` (P/E ratio)
- `pb_less_than` (price-to-book)
- `ps_less_than` (price-to-sales)
- `roe_more_than` (return on equity, decimal — `0.15` = 15%)
- `gross_margin_more_than`, `net_margin_more_than` (decimals)
- `debt_to_equity_less_than`
- `current_ratio_more_than`
- `dividend_yield_more_than` (decimal)

Universe-narrowing filters (applied in pass 1):
- `market_cap_more_than`, `market_cap_less_than`
- `sector`, `industry`, `country`, `exchange`

**Requires an FMP API key.** Costs roughly N+1 API calls per invocation, where N is `screener_universe_size` (default 200). Tune `screener_universe_size` down or pre-narrow with sector/market-cap filters to conserve quota.

---

## Example queries

Once enabled on a model, the model will call the tool automatically when the user asks about stocks. Example prompts:

- "What's the current price of AAPL?"
- "Give me Microsoft's company profile and key metrics."
- "Show me Tesla's last 4 quarterly income statements."
- "How did NVIDIA's last earnings compare to estimates?"
- "What's the latest news on Apple?"
- "What do analysts think of GOOGL right now?"
- "Find me US technology stocks with a market cap over $50 billion."
- "Show me NASDAQ companies with more than $20B in cash and a P/E under 25."
- "What are the best dividend stocks in the consumer defensive sector?"

---

## Caching and rate limits

The tool caches all HTTP responses in memory for `cache_ttl_seconds` (default 60). This means repeated questions about the same ticker within a minute won't burn extra API calls. The cache is per-process — restarting Open WebUI clears it.

For high-traffic deployments, increase the cache TTL or upgrade your provider plans.

---

## Troubleshooting

**"Stock screening requires a Financial Modeling Prep (FMP) API key."**
You're trying to use `screen_stocks` or `screen_by_fundamentals` without an FMP key. Sign up for a free key at [financialmodelingprep.com](https://site.financialmodelingprep.com/) and paste it into the `fmp_api_key` valve.

**Quote/profile returns null fields or "Could not retrieve quote".**
Probably a network or rate-limit issue. The tool will report which provider was tried in `provider_errors`. If you see HTTP 429, you've hit the rate limit — wait, or upgrade. If `prefer_yfinance_fallback` is on (default), the tool will already have tried yfinance as a backup.

**yfinance suddenly stops working.**
Yahoo occasionally changes its site, breaking yfinance. Update yfinance to the latest version (`pip install -U yfinance`). If it's still broken, configure a Finnhub API key as a more stable primary.

**Screener returns 403 or "free tier may not include this endpoint".**
FMP occasionally moves endpoints between tiers. Check your plan dashboard. The basic `/stock-screener` endpoint is currently included in the free tier, but `key-metrics-ttm` (used by `screen_by_fundamentals`) may have stricter limits.

**Status events not showing up.**
Make sure `verbose_status` is enabled in your UserValves, and that your Open WebUI version supports event emitters (≥ 0.4.0).

---

## Privacy and data

- The tool sends ticker symbols and basic query parameters to the configured providers. No chat content or user identifiers are sent.
- Cached responses live in memory only; they're never written to disk.
- API keys are stored encrypted by Open WebUI and never exposed to the model.

---

## License

MIT.

This tool is not affiliated with Anthropic, Open WebUI, Finnhub, Yahoo, or Financial Modeling Prep. Stock data is provided as-is for informational purposes only and should not be construed as investment advice.