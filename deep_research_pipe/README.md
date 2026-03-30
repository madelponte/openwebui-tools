# Deep Research Pipe for Open WebUI

A function pipe that performs multi-cycle, LLM-driven web research and produces a structured report with citations. It searches the web via SearXNG, fetches and extracts page content, and uses your chosen model to plan, analyse, and write a final report grounded entirely in the sources it found.

## How It Works

The pipe runs in three phases:

**Phase 1 — Research Plan.** When you send a query, the pipe runs a few broad exploratory searches on your topic, then asks the LLM to generate a structured research plan with named sections and targeted search queries. The plan is presented to you in a confirmation dialog. You can type "ok" to proceed, or describe changes (e.g. "add a section on pricing" or "focus more on the European market") and it will adjust the plan before continuing. Set `SKIP_PLAN_CONFIRMATION` to `True` if you want it to skip this step and go straight to researching.

**Phase 2 — Research Loop.** The pipe works through the planned search queries across multiple cycles. Each cycle searches SearXNG, fetches the actual page content (with concurrent downloads), and accumulates text snippets. After reaching the minimum number of cycles, the LLM evaluates whether coverage is sufficient or if more searching is needed. If the accumulated context gets too large, older snippets are compressed into summaries. This phase runs fully autonomously with no user interaction — you just see status updates in the UI.

**Phase 3 — Report.** All collected research is passed to the LLM with instructions to write a report using only the gathered sources. The report includes an abstract, table of contents, detailed sections with `[n]` citations, a conclusion, and a numbered source list. Citations also appear as clickable chips below the message in Open WebUI's interface.

## Installation

1. Go to **Admin Panel → Functions** in Open WebUI.
2. Click **Add New Function** and paste the contents of `deep_research_pipe.py`.
3. Save and enable the function.
4. Start a new chat and select **Deep Research** from the model dropdown.
5. Configure the valves (see below) from the function settings — at minimum set your `SEARXNG_URL` and `RESEARCH_MODEL`.

## Requirements

- **SearXNG** instance with JSON API enabled. The pipe queries `/search?format=json`.
- A model that handles long contexts well (the report generation prompt can be large).
- **Optional:** FlareSolverr instance for bypassing Cloudflare/captcha-protected pages.
- **Optional:** `pypdf` installed in your Open WebUI environment for PDF text extraction.

## Adding Flaresolverr to docker compose

If you deploy with docker compose, adding this will give you access to a basic flaresolverr instance.

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

## Valves Reference

### Model Configuration

| Valve | Default | Description |
|---|---|---|
| `RESEARCH_MODEL` | *(empty)* | Model ID for all LLM calls (planning, analysis, report writing). Leave blank to use the system default. Should be a capable model with good instruction-following. |
| `EMBEDDING_MODEL` | *(empty)* | Embedding model for PDF parsing. Reserved for future use. |

### Search Engine

| Valve | Default | Description |
|---|---|---|
| `SEARXNG_URL` | `http://searxng:8080` | Base URL of your SearXNG instance. |
| `SEARCH_RESULTS_PER_QUERY` | `10` | How many results SearXNG returns per query. |
| `SEARCH_ENGINES` | *(empty)* | Comma-separated engine list (e.g. `google,bing,duckduckgo`). Blank uses SearXNG's defaults. |

### FlareSolverr

| Valve | Default | Description |
|---|---|---|
| `FLARESOLVERR_URL` | *(empty)* | FlareSolverr endpoint (e.g. `http://flaresolverr:8191/v1`). When set, pages that fail direct fetch are retried through FlareSolverr. Leave blank to disable. |

### Context Control

| Valve | Default | Description |
|---|---|---|
| `SNIPPET_MAX_WORDS` | `300` | Max words kept per page snippet. Lower values keep context small; higher values capture more detail per source. |
| `MAX_TOTAL_CONTEXT_WORDS` | `30000` | Soft cap on total accumulated research text. When exceeded, older snippets are compressed into summaries by the LLM. |

### Research Cycles

| Valve | Default | Description |
|---|---|---|
| `MIN_RESEARCH_CYCLES` | `2` | Minimum search-fetch-analyse cycles before the LLM is allowed to stop early. |
| `MAX_RESEARCH_CYCLES` | `5` | Hard cap on cycles. More cycles = more sources but longer runtime. |
| `QUERIES_PER_CYCLE` | `3` | Number of search queries executed per cycle. |

### Report Length

| Valve | Default | Description |
|---|---|---|
| `SECTION_MIN_WORDS` | `200` | Target minimum words per report section. Increase for more detailed writing. |
| `SECTION_MAX_WORDS` | `500` | Target maximum words per report section. Set to 800–1000 for thorough deep-dives. |
| `REPORT_MAX_TOKENS` | `16384` | Max tokens the LLM can generate for the final report. Increase if the report is being cut off. |

### Fetch Settings

| Valve | Default | Description |
|---|---|---|
| `PAGE_FETCH_TIMEOUT` | `15` | Timeout in seconds for fetching individual web pages. |
| `MAX_CONCURRENT_FETCHES` | `5` | How many pages to download at the same time. Higher values are faster but may trigger rate limits. |

### Behaviour

| Valve | Default | Description |
|---|---|---|
| `SKIP_PLAN_CONFIRMATION` | `False` | When `True`, the pipe skips the plan review dialog and starts researching immediately. |

## Tips

- **Longer reports:** Increase `SECTION_MAX_WORDS` to 800 or 1000, and make sure `REPORT_MAX_TOKENS` is high enough (16384+) so the model doesn't run out of generation budget.
- **More thorough research:** Increase `MAX_RESEARCH_CYCLES` to 7–10 and `QUERIES_PER_CYCLE` to 4–5. This will take longer but finds more sources.
- **Faster runs:** Lower `MAX_RESEARCH_CYCLES` to 2–3, reduce `SEARCH_RESULTS_PER_QUERY` to 5, and set `SKIP_PLAN_CONFIRMATION` to `True`.
- **Rate limiting issues:** If SearXNG or source sites return errors, reduce `MAX_CONCURRENT_FETCHES` and `SEARCH_RESULTS_PER_QUERY`. Adding a delay between cycles would require a code change.
- **Page refresh during research:** The pipe writes a progress snapshot into the message body early on, so if you refresh mid-research you'll see a summary of what's been collected so far rather than a blank message. The final report replaces this snapshot when complete.
