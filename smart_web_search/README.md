# Smart Web Search for Open WebUI

An intelligent web search tool that gives your LLM the ability to autonomously decide when it needs external knowledge, formulate its own search queries, scrape full page content, and iterate until it has enough information to answer your question.

## The Problem

Open WebUI's built-in web search toggle searches using your exact prompt, which fails with complex or multi-part questions. The model has no control over what gets searched or when.

## How This Tool is Different

With native tool calling enabled, the model itself decides:

- **Whether** it needs to search at all (it won't search if it already knows the answer)
- **What** to search for (it formulates concise, targeted queries)
- **When** to search again (if the first results aren't sufficient, it refines and retries)
- **When** to deep-read a page (it can fetch the full content of any promising URL)

This mirrors how a human would research — start with what you know, search when you hit a gap, read deeper when a source looks useful, and keep going until you have what you need.

## Features

- **SearXNG integration** — privacy-respecting meta-search via your self-hosted instance
- **Full page scraping** — doesn't just return snippets; scrapes and extracts the actual page content
- **FlareSolverr fallback** — automatically retries blocked/captcha pages through FlareSolverr
- **PDF handling** — detects and extracts text from PDF documents encountered during search
- **Concurrent scraping** — fetches multiple pages in parallel for speed
- **Citations** — emits source citations into the Open WebUI chat interface
- **Configurable via Valves** — tune every aspect from the admin UI without touching code

## Requirements

- Open WebUI (with native function calling support)
- A model that supports tool calling (e.g. Qwen 3, Llama 3.3 70B+, GPT-4o, Claude, Gemini, etc.)
- SearXNG instance with JSON format enabled
- FlareSolverr instance (optional, for captcha bypass)

### SearXNG JSON Format

Your SearXNG instance must have JSON output enabled. In your SearXNG `settings.yml`:

```yaml
search:
  formats:
    - html
    - json
```

Restart SearXNG after making this change.

## Installation

1. In Open WebUI, go to **Workspace → Tools**
2. Click the **+** (plus) button to create a new tool
3. Paste the entire contents of `smart_web_search.py` into the editor
4. Click **Save**

## Configuration

After saving the tool, click the gear icon to configure the Valves:

### Required

| Valve | Default | Description |
|-------|---------|-------------|
| `SEARXNG_BASE_URL` | `http://searxng:8080` | Base URL of your SearXNG instance |

### FlareSolverr (Optional)

| Valve | Default | Description |
|-------|---------|-------------|
| `FLARESOLVERR_URL` | `http://flaresolverr:8191/v1` | FlareSolverr API endpoint. Clear this to disable. |
| `FLARESOLVERR_TIMEOUT` | `60` | Timeout in seconds for FlareSolverr requests |

### Search Behavior

| Valve | Default | Description |
|-------|---------|-------------|
| `SEARCH_RESULTS_COUNT` | `5` | Number of results to fetch from SearXNG |
| `PAGES_TO_SCRAPE` | `3` | How many top results to fully scrape |
| `SEARCH_CATEGORIES` | `general` | SearXNG categories (comma-separated, e.g. `general,it,science`) |
| `SEARCH_LANGUAGE` | `en` | Language code for results |
| `SEARCH_TIME_RANGE` | *(empty)* | Filter by time: `day`, `week`, `month`, or `year` |

### Content Processing

| Valve | Default | Description |
|-------|---------|-------------|
| `MAX_PAGE_CONTENT_LENGTH` | `20000` | Max characters extracted per page (protects context window) |
| `MIN_CONTENT_LENGTH` | `50` | Minimum chars for a page to count as valid |

### Network

| Valve | Default | Description |
|-------|---------|-------------|
| `REQUEST_TIMEOUT` | `15` | Timeout in seconds for direct HTTP requests |
| `USER_AGENT` | Chrome 120 string | User-Agent header sent with requests |
| `CONCURRENT_SCRAPE_WORKERS` | `3` | Parallel page scrape threads |
| `IGNORED_DOMAINS` | *(empty)* | Comma-separated domains to skip (e.g. `pinterest.com,facebook.com`) |

### User Valves (per-user)

Users can toggle these in the chat interface:

| Valve | Default | Description |
|-------|---------|-------------|
| `SHOW_STATUS_UPDATES` | `true` | Show progress messages during search |
| `INCLUDE_CITATIONS` | `true` | Attach source citations to results |

## Enabling the Tool

### Per-model (recommended)

1. Go to **Workspace → Models**
2. Select your model and click the edit (pencil) icon
3. Scroll to the **Tools** section
4. Check **Smart Web Search**
5. Click **Save**

### Per-chat

1. Open a chat
2. Click the **+** icon in the input area
3. Toggle **Smart Web Search** on

### Function Calling Mode

Go to **Advanced Params** in your chat and set **Function Calling** to **Native**. This is required for the model to autonomously decide when to use the tool.

## How It Works in Practice

**You ask:** "Help me set up Grafana Alloy with a Helm chart for collecting Kubernetes logs"

**The model:**
1. Writes an initial answer from its own knowledge
2. If you come back with an error, the model recognizes it doesn't know the fix
3. Calls `search_web("grafana alloy helm chart loki config")` autonomously
4. Reads the scraped documentation pages
5. Calls `fetch_page("https://grafana.com/docs/alloy/...")` for deeper reading
6. Provides an updated answer with the correct configuration

All of this happens automatically — you just have a normal conversation.

## Tips

- **Smaller context = better results.** If you're using a model with a limited context window, lower `PAGES_TO_SCRAPE` and `MAX_PAGE_CONTENT_LENGTH` to avoid overwhelming it.
- **Ignored domains** are useful for filtering out low-quality results (social media, aggregators, etc.).
- **Time range filtering** is great for fast-moving topics where you only want recent results.
- **FlareSolverr** is only needed if you frequently encounter sites with Cloudflare protection or similar. If you don't have it running, just clear the `FLARESOLVERR_URL` valve.

## License

MIT