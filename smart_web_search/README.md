# Smart Web Search — Open WebUI Tool

A tool that gives your LLM the ability to search the web *on its own terms*. Instead of blindly searching your raw prompt (like the built-in web search toggle does), the model decides **when** it needs more information and **crafts its own search queries** to find exactly what it needs.

## How It Works

When you attach this tool to a model, two functions become available to it:

**`search_web`** — The model writes a short, targeted query and gets back titles, URLs, and snippets from your search engine. It can call this multiple times with different queries to research a topic from several angles.

**`fetch_page`** — After searching, if a snippet isn't detailed enough, the model can read the full content of a result page. This is what lets it dig into documentation, changelogs, and troubleshooting guides — not just skim headlines.

The key difference from the built-in search toggle: the model tries to answer with its own knowledge first. If it needs to verify a fact, look up a config option, check current docs, or debug an error you've reported, it searches on its own. You don't need to tell it to search — it just does.

## Requirements

- A search engine with a JSON API. **SearXNG** (self-hosted) or **Tavily** (cloud API) are supported.
- Your model must support **native function/tool calling** (set Function Calling to "Native" in chat advanced params).
- **Optional:** A FlareSolverr instance for bypassing Cloudflare and CAPTCHA protections when fetching pages.

### SearXNG Note

Your SearXNG instance must have JSON output enabled. In your `settings.yml`:

```yaml
search:
  formats:
    - html
    - json
```

## Installation

1. Go to **Workspace → Tools** and click the **+** button.
2. Paste the contents of `smart_web_search.py` into the editor.
3. Give it a name (e.g. "Smart Web Search") and save.
4. Go to **Workspace → Models**, select your model, click the edit icon.
5. Scroll to the **Tools** section and check "Smart Web Search".
6. In your chat, open **Advanced Params** and set **Function Calling** to **Native**.

## Valve Reference

### Admin Valves

These are configured by the admin in the tool settings.

| Valve | Default | Description |
|---|---|---|
| `SEARCH_ENGINE_URL` | `http://searxng:8080/search` | The search API endpoint. For SearXNG, point to the `/search` path. For Tavily, use `https://api.tavily.com/search`. |
| `SEARCH_ENGINE_TYPE` | `searxng` | Which search backend to use: `searxng` or `tavily`. |
| `SEARCH_API_KEY` | *(empty)* | API key for the search engine. Required for Tavily. Optional for SearXNG unless you've enabled authentication. |
| `MAX_SEARCH_RESULTS` | `5` | How many results to return per search query. More results give the model more to work with but use more context. |
| `MAX_CONTENT_LENGTH` | `4000` | Maximum characters of page content returned by `fetch_page`. Increase if your model has a large context window and you want it to read more of each page. |
| `FETCH_TIMEOUT` | `15` | Timeout in seconds for all HTTP requests (search and fetch). |
| `SEARCH_CATEGORIES` | `general` | Comma-separated SearXNG categories (e.g. `general,it,science`). Only applies to SearXNG. |
| `FETCH_FULL_PAGE` | `true` | Whether the `fetch_page` tool is available. Disable if you only want the model to use search snippets. |
| `FLARESOLVERR_URL` | *(empty)* | FlareSolverr endpoint (e.g. `http://flaresolverr:8191/v1`). Leave empty to disable. When set, pages that fail to load directly are automatically retried through FlareSolverr. |
| `RETRY_WITH_FLARESOLVERR` | `true` | Whether to automatically fall back to FlareSolverr when a direct fetch is blocked. Only relevant if `FLARESOLVERR_URL` is set. |

### User Valves

These can be changed by individual users from the chat interface.

| Valve | Default | Description |
|---|---|---|
| `SHOW_STATUS_UPDATES` | `true` | Show "Searching…" and "Fetching…" status messages above the response while the tool is working. |

## FlareSolverr Setup (Optional)

Some websites block automated requests with Cloudflare challenges or CAPTCHAs. FlareSolverr runs a headless browser that solves these automatically.

The tool tries a direct fetch first (with realistic browser headers). If it detects a block — HTTP 403/503, Cloudflare challenge pages, CAPTCHA prompts — it retries through FlareSolverr. This means unprotected pages load fast, and protected pages still work.

Add this to your `docker-compose.yml`:

```yaml
flaresolverr:
  image: ghcr.io/flaresolverr/flaresolverr:latest
  container_name: flaresolverr
  environment:
    - LOG_LEVEL=info
    - LOG_HTML=false
    - TZ=America/New_York
  ports:
    - "8191:8191"
  restart: unless-stopped
```

Then set the `FLARESOLVERR_URL` valve to `http://flaresolverr:8191/v1` (adjust the hostname to match your Docker network).

## Tips

- **Search aggressiveness:** The tool is designed to search liberally — for verification, references, docs, version-specific details, and error debugging. If you find it searching too often or not often enough, the behavior is driven by the docstrings in the Python file. You can edit them to tune the model's judgment.
- **Multiple searches:** The model can call `search_web` several times in a single response with different queries. This is useful for complex questions that need information from multiple sources.
- **Context budget:** If your model has a smaller context window, lower `MAX_SEARCH_RESULTS` (e.g. 3) and `MAX_CONTENT_LENGTH` (e.g. 2000) to leave room for the model's own reasoning. For large-context models, you can increase both.
- **SearXNG categories:** Setting `SEARCH_CATEGORIES` to `it` or `science` can improve result quality for technical questions, but `general` is a safe default.
