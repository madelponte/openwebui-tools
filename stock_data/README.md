# Stock Data — Open WebUI Tool

A Workspace Tool that lets models query stock quotes, company fundamentals, financial statements, earnings, news, analyst recommendations, and ticker symbols.

## Compatibility

- Open WebUI **0.11.0 or later**
- A model with native tool calling
- Python dependencies: `requests` and `yfinance` (declared in frontmatter)

## Providers

| Provider | Key required | Best for |
|---|---:|---|
| [Finnhub](https://finnhub.io/) | Yes | Quotes, profiles, earnings, news, analyst recommendations, symbol search |
| [yfinance](https://github.com/ranaroussi/yfinance) | No | No-key fallback for most methods |
| [Financial Modeling Prep](https://site.financialmodelingprep.com/) | Yes | Clean financial statements and optional quote/profile/earnings data |

`auto` mode chooses Finnhub when its key is configured and otherwise uses yfinance. Financial statements prefer FMP when configured. If enabled, `prefer_yfinance_fallback` retries failed provider calls through yfinance.

## Installation

1. Go to **Workspace → Tools**.
2. Open **Create** and create or import a tool.
3. Paste or upload `stock_data_tool.py`, then save it.
4. Configure the tool valves.
5. In **Workspace → Models**, edit a model and add **Stock Data** in its **Tools** section. You can also enable it per chat from the composer’s **Integrations** menu.

Native function calling is the default in Open WebUI 0.11. If the model has an override, use **Native**, not Legacy.

Open WebUI normally installs frontmatter requirements automatically. For production or multi-worker deployments, bake them into the image instead:

```dockerfile
FROM ghcr.io/open-webui/open-webui:main
RUN pip install --no-cache-dir requests yfinance
```

## Configuration

### Admin Valves

| Valve | Default | Purpose |
|---|---|---|
| `finnhub_api_key` | *(empty)* | Finnhub API key. Recommended for reliable quotes, profiles, earnings, news, recommendations, and symbol search. |
| `fmp_api_key` | *(empty)* | Financial Modeling Prep API key. Optional; preferred for financial statements. |
| `alpha_vantage_api_key` | *(empty)* | Reserved for future use. |
| `default_provider` | `auto` | `auto`, `finnhub`, `yfinance`, or `fmp`. |
| `financials_provider` | `auto` | `auto`, `fmp`, `yfinance`, or `finnhub`. |
| `prefer_yfinance_fallback` | `true` | Retry failed non-yfinance requests with yfinance. |
| `request_timeout` | `15` | HTTP timeout in seconds. |
| `cache_ttl_seconds` | `60` | Per-process in-memory cache TTL; `0` disables caching. |
| `max_news_items` | `5` | Maximum news articles returned. |
| `max_financial_periods` | `4` | Maximum annual or quarterly periods returned. |

### User Valves

| Valve | Default | Purpose |
|---|---|---|
| `verbose_status` | `true` | Show progress events in chat. |
| `include_raw_numbers` | `false` | Reserved user preference for raw-number output. |

Open WebUI 0.11 can optionally encrypt valve values at rest when the administrator enables valve encryption. Password-style fields are masked in the settings UI.

## Available Methods

All methods are async and return JSON strings.

- `get_stock_quote(symbol)` — latest price, change, OHLC, volume, previous close, and timestamp.
- `get_company_profile(symbol)` — company details, market cap, and key valuation/fundamental metrics.
- `get_financials(symbol, statement="income", period="annual")` — income, balance-sheet, or cash-flow data, annually or quarterly.
- `get_earnings(symbol)` — recent actual/estimated EPS and surprises.
- `get_company_news(symbol)` — recent company articles.
- `get_analyst_recommendations(symbol)` — strong-buy through strong-sell counts.
- `search_symbol(query)` — search tickers by company name; requires Finnhub.

## Example Prompts

- “What’s the latest AAPL quote?”
- “Show Microsoft’s company profile and key metrics.”
- “Get Tesla’s last four quarterly income statements.”
- “How did NVIDIA’s recent earnings compare with estimates?”
- “What is the latest news about Apple?”
- “Find the ticker for Toyota Motor.”

## Notes

- Market data can be delayed, incomplete, or revised. Do not treat it as investment advice.
- yfinance uses unofficial Yahoo Finance interfaces and may break when upstream behavior changes. Keep it updated.
- The cache is local to each Open WebUI process and is cleared on restart.
- API keys and ticker/query parameters are sent only to the selected provider; chat text and Open WebUI user identifiers are not forwarded.

## Troubleshooting

**Symbol search says a Finnhub key is required:** configure `finnhub_api_key`; yfinance does not provide the search path used by this tool.

**A request returns provider errors:** inspect the `provider_errors` array. HTTP 401/403 normally indicates a key or plan issue; HTTP 429 indicates a rate limit.

**yfinance stops returning data:** update to the latest yfinance release or configure Finnhub as the primary provider.

## License

MIT. This tool is not affiliated with Open WebUI, Finnhub, Yahoo, or Financial Modeling Prep.
