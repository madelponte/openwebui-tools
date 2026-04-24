# Agentic Web Search for Open WebUI

A web search tool for [Open WebUI](https://github.com/open-webui/open-webui) that lets the model decide *when* to search and *what* to search for, rather than blindly searching the user's entire prompt.

## Why this exists

Open WebUI's built-in web search works ok, but doesn't give the model much context as to what it is on the page before it attempts to fetch the whole page. 
This tool gives the model both the page snippet and the table of contents from the structured data if it has one. 
The extra information from the table of contents allows the model to get a better idea of what is on the page before downloading the whole thing saving context. 
flaresolverr is also integrated to bypass captchas which the built in search does not do. 


## Features

- **Self-hosted SearXNG** for the actual search backend — your queries don't leave your network.
- **FlareSolverr fallback** — if a page returns a Cloudflare challenge, the tool automatically retries through your FlareSolverr instance.
- **Structured-data preview** — the top N search results come back with not just title/snippet but also a heading outline and JSON-LD table of contents (recipes, how-tos, articles), so the model can decide what's worth opening before spending tokens on a full page fetch.
- **Two fetch modes** — plain readable text or structured metadata, model's choice based on what it needs.
- **Reddit handling** — Reddit URLs are automatically rewritten to the `.json` endpoint and the response is compacted to just `{post, comments}` with depth info.
- **PDF support** — PDFs are detected by content type or extension and extracted to plain text via `pypdf`.
- **Citations** — fetched pages show up as proper chat sources you can click through.
- **Status updates** — works in both Default and Native function-calling modes.

## Requirements

- Open WebUI (any reasonably recent version)
- A running [SearXNG](https://github.com/searxng/searxng) instance with JSON output enabled
- *(Optional but recommended)* A running [FlareSolverr](https://github.com/FlareSolverr/FlareSolverr) instance for Cloudflare bypass
- A model with native tool calling for best results (works in Default mode too)

### SearXNG configuration

SearXNG ships with JSON output disabled. Edit your `settings.yml` and make sure it includes:

```yaml
search:
  formats:
    - html
    - json
```

Restart SearXNG after editing. If you skip this step, the tool will return a clear error telling you exactly what's wrong.

### Python packages

The tool's frontmatter declares `httpx`, `beautifulsoup4`, `lxml`, and `pypdf` as requirements. Open WebUI will pip-install these on save automatically.

> **Production note:** if you run Open WebUI with `UVICORN_WORKERS > 1` or in a multi-replica setup, runtime pip installs cause race conditions. Set `ENABLE_PIP_INSTALL_FRONTMATTER_REQUIREMENTS=False` and bake the dependencies into your image:
> ```dockerfile
> FROM ghcr.io/open-webui/open-webui:main
> RUN pip install --no-cache-dir httpx beautifulsoup4 lxml pypdf
> ```

## Installation

1. Open Open WebUI and go to **Workspace → Tools**.
2. Click **+** (Create New Tool) or the **Import** button.
3. Paste the contents of `agentic_web_search.py`, or import the file directly.
4. Click **Save**. Open WebUI will install the required packages at this point.
5. Open the tool's settings (gear icon) and configure the **Valves** — at minimum, set `SEARXNG_URL` and `FLARESOLVERR_URL` to point at your instances.
6. Enable the tool for the models you want to use it with: go to **Workspace → Models**, edit your model, and toggle this tool on. Or enable it per-chat using the **+** button next to the chat input.

### Recommended model settings

For best results, set the model's **Function Calling** mode to **Native** (Admin Panel → Settings → Models → your model → Advanced Params → Function Calling). This gives the model autonomous control over when to invoke the tools.

## Valves reference

Valves are configured per-tool in Open WebUI. Admins can edit them; users see the tool with whatever values you've set.

### SearXNG

| Valve | Default | Description |
|---|---|---|
| `SEARXNG_URL` | `http://searxng:8080` | Base URL of your SearXNG instance. **No trailing `/search`** — just the host. Use the Docker service name if SearXNG is on the same network as Open WebUI. |
| `NUM_RESULTS` | `5` | How many results `search_web` returns to the model. |
| `ENRICH_TOP_N` | `3` | For this many top results, the tool also fetches the page in parallel and attaches structured metadata (title, description, heading outline, JSON-LD table of contents). Set to `0` to disable enrichment if you want faster searches at the cost of less context for the model. |
| `SEARXNG_CATEGORIES` | `general` | Comma-separated SearXNG categories. Examples: `general`, `news`, `it`, `general,news`. |
| `SEARXNG_LANGUAGE` | `en` | Language code. `all` to disable filtering. |
| `SEARXNG_TIME_RANGE` | *(empty)* | Filter results by recency. One of: `""`, `day`, `week`, `month`, `year`. |
| `SEARXNG_SAFESEARCH` | `0` | `0` = off, `1` = moderate, `2` = strict. |

### FlareSolverr

| Valve | Default | Description |
|---|---|---|
| `FLARESOLVERR_URL` | `http://flaresolverr:8191` | Base URL of FlareSolverr. **No trailing `/v1`**. Leave **empty** to disable the Cloudflare fallback entirely. |
| `FLARESOLVERR_TIMEOUT_MS` | `60000` | The `maxTimeout` value passed to FlareSolverr in milliseconds. Increase for heavily protected sites that take longer to solve. |

### HTTP behavior

| Valve | Default | Description |
|---|---|---|
| `HTTP_TIMEOUT_SECONDS` | `25.0` | Timeout for direct page fetches and SearXNG queries, in seconds. |
| `VERIFY_SSL` | `true` | Whether to verify TLS certificates. Set to `false` only if your SearXNG / FlareSolverr instance uses a self-signed cert. |
| `USER_AGENT` | *(modern Chrome UA)* | User-Agent header sent with direct fetches. The default mimics a current Chrome browser; some sites require this. |

### Output sizing

These keep the tool's responses from blowing out the model's context window.

| Valve | Default | Description |
|---|---|---|
| `MAX_PAGE_CHARS` | `25000` | Maximum characters of page content returned by `fetch_page` before truncation. Roughly 6,000 tokens. |
| `MAX_ENRICH_HEADINGS` | `25` | Max headings included per enriched search result. |
| `MAX_SNIPPET_CHARS` | `400` | Max length of each result's SearXNG snippet. |

### Misc

| Valve | Default | Description |
|---|---|---|
| `EMIT_CITATIONS` | `true` | Emit citation events when `fetch_page` is called, so retrieved pages appear as clickable sources in the chat. |
| `LOG_REQUESTS` | `false` | Print extra debug info to the Open WebUI logs. |

## How the model uses it

Two functions are exposed:

### `search_web(query: str)`

The model crafts a focused query (usually 2–6 keywords) and calls this. It gets back a JSON object with `NUM_RESULTS` items, each containing:

- `url`, `title`, `snippet`, `engine` — the basics
- For the top `ENRICH_TOP_N` results: `page_title`, `page_description`, `page_headings`, and `page_toc` (when the page exposes JSON-LD structured data)

The docstring explicitly tells the model to refine and re-search if the first pass isn't useful.

### `fetch_page(url: str, mode: "text" | "structured")`

The model passes either a URL from a previous search result or one the user provided directly. It picks the mode based on need:

- `"text"` — plain readable text, with scripts/styles/nav stripped. Best for actually reading an article.
- `"structured"` — only the page metadata (title, description, headings, JSON-LD). Useful when the model just wants to know *what's on* a page.

PDFs always come back as plain text since they have no structured data to extract.

If the page is Cloudflare-blocked and `FLARESOLVERR_URL` is set, the tool retries through FlareSolverr automatically. Reddit URLs are rewritten to `.json` and the response is compacted.

## Troubleshooting

**`SearXNG returned 403`** — JSON output isn't enabled in your SearXNG `settings.yml`. See the SearXNG configuration section above.

**`Both direct and FlareSolverr fetches failed`** — Either FlareSolverr isn't running, the URL in `FLARESOLVERR_URL` is wrong, or the page is failing the Cloudflare challenge for a different reason (rate limiting, IP block, etc.). Check the FlareSolverr container logs.

**Tool isn't being called by the model** — Make sure (1) the tool is enabled for the current model or chat, (2) the model supports tool calling, and (3) Function Calling is set to Native or Default in the model's advanced params.

**Page content looks garbled** — The site may use heavy client-side JavaScript that the tool can't execute. FlareSolverr will help in some cases (it uses a real browser) but not for SPA-rendered pages that require user interaction.

**Reddit returns nothing useful** — Reddit aggressively rate-limits the `.json` endpoints from cloud IPs. If your Open WebUI runs on a VPS, you may get throttled. Running from a residential IP or behind a reasonable rate limit usually resolves this.

## License

MIT