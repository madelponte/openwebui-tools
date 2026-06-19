# Deep Research Pipe for Open WebUI

A function pipe that performs multi-cycle, LLM-driven web research and produces a structured report with citations. It searches the web via SearXNG, fetches and extracts page content, and uses your chosen model to plan, analyse, and write a final report grounded entirely in the sources it found.

## How It Works

The pipe runs in three phases:

**Phase 1 — Research Plan.** When you send a query, the pipe runs a few broad exploratory searches on your topic, then asks the LLM to generate a structured research plan with named sections and targeted search queries. The plan is presented to you in a confirmation dialog. You can type "ok" to proceed, or describe changes (e.g. "add a section on pricing" or "focus more on the European market") and it will adjust the plan before continuing. Set `SKIP_PLAN_CONFIRMATION` to `True` if you want it to skip this step and go straight to researching.

**Phase 2 — Research Loop.** The pipe researches one planned section at a time, prioritising whichever sections have the fewest sources so far (gap-driven), across multiple cycles. Each cycle searches SearXNG, fetches pages concurrently, and — instead of keeping the first few hundred words of each page — **extracts the passages most relevant to the query that surfaced it** and stores them tagged with their section, the originating query, a relevance score, and (when available) the source's publish date. It also **follows a few promising links found inside fetched pages** (multi-hop) to reach primary sources the search engine didn't rank. The loop keeps going until every planned section has met its coverage target (`MIN_SOURCES_PER_SECTION`) or the cycle cap is reached; if a section stays starved, the LLM generates new queries aimed specifically at it. When the accumulated context gets too large, the **lowest-relevance** snippets are compressed into a summary while the highest-scored ones are kept verbatim. On time-sensitive topics the plan biases searches toward recent results. This phase runs fully autonomously — you just see status updates in the UI.

**Resilient fetching.** Page downloads mirror the companion `fetch_page` MCP tool's fallback ladder so more of the URLs the model finds actually yield content:

- **Documents** — PDF, Word (`doc`/`docx`), Excel (`xls`/`xlsx`), PowerPoint (`ppt`/`pptx`), OpenDocument, RTF, and EPUB are routed to Apache Tika (auto-detected by content-type or URL extension), not just PDFs.
- **Bot walls & rate limits** — Cloudflare, PerimeterX/HUMAN, DataDome, Akamai, and any HTTP 429 are detected (instead of being saved as if they were page content) and retried through FlareSolverr, which renders in a real browser.
- **JavaScript SPAs** — a page that returns HTTP 200 but no readable text (its body loads via XHR) is re-rendered through FlareSolverr.
- **Wayback Machine** — if a page stays blocked or empty after the above, the pipe pulls the closest archived snapshot from the Internet Archive (needs no extra service). Recovered text is clearly flagged in the snippet as a possibly-stale archived copy.
- **YouTube** — a YouTube video URL returns the video's transcript instead of the scrapeable watch page (requires the optional `youtube-transcript-api` library — see Requirements).

Reddit links continue to be read through Reddit's public `.json` endpoint.

**Phase 3 — Report.** By default (`REPORT_MODE="sectioned"`) each report section is drafted in its own LLM call against **only that section's gathered sources** (plus shared cross-cutting context), then a final synthesis pass writes the title, abstract, and conclusion from the drafted sections. Writing sections independently produces more thorough, better-grounded coverage and scales past a single call's token ceiling. Every source is assigned one **global `[n]` number** that is shared across all section prompts, so a citation always points at the source the model was actually shown — even though sections are written separately. If the research exceeds `REPORT_CONTEXT_MAX_CHARS`, sources past the budget are dropped from both the data and the citation list together (never cited-but-unseen). Set `REPORT_MODE="single"` to fall back to the legacy one-shot report (cheaper, fewer LLM calls). Citations are also emitted as clickable source chips below the message in Open WebUI's interface.

## Installation

1. Go to **Admin Panel → Functions** in Open WebUI.
2. Click **Add New Function** and paste the contents of `deep_research_pipe.py`.
3. Save and enable the function.
4. Start a new chat and select **Deep Research** from the model dropdown.
5. Configure the valves (see below) from the function settings — at minimum set your `SEARXNG_URL` and `RESEARCH_MODEL`.

## Requirements

- **SearXNG** instance with JSON API enabled. The pipe queries `/search?format=json`.
- A model that handles long contexts well (the report generation prompt can be large).
- **Optional:** FlareSolverr instance for bypassing Cloudflare/captcha-protected pages and rendering JavaScript SPAs.
- **Optional:** Apache Tika server (`TIKA_URL`) for extracting text from PDF/Word/Excel/PowerPoint/OpenDocument/RTF/EPUB documents.
- **Optional:** `youtube-transcript-api` (>= 1.0) installed in your Open WebUI environment to pull transcripts from YouTube video URLs. If absent, the pipe falls back to a normal HTML fetch for those URLs — no error.

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
| `EMBEDDING_MODEL` | *(empty)* | Optional. Reserved hook for embedding-based relevance ranking of page passages/sources; when set the pipe uses it if the Open WebUI embeddings endpoint resolves and otherwise falls back to the built-in lexical (keyword-overlap) scorer. Blank = lexical-only (the default; no setup needed). |

### Search Engine

| Valve | Default | Description |
|---|---|---|
| `SEARXNG_URL` | `http://searxng:8080` | Base URL of your SearXNG instance. |
| `SEARCH_RESULTS_PER_QUERY` | `10` | How many results SearXNG returns per query. |
| `SEARCH_ENGINES` | *(empty)* | Comma-separated engine list (e.g. `google,bing,duckduckgo`). Blank uses SearXNG's defaults. |
| `SEARCH_TIME_RANGE` | *(empty)* | Recency filter on every search: blank, `day`, `week`, `month`, or `year`. The research plan's own recency hint (for time-sensitive topics) overrides this per run. |

### FlareSolverr

| Valve | Default | Description |
|---|---|---|
| `FLARESOLVERR_URL` | *(empty)* | FlareSolverr endpoint (e.g. `http://flaresolverr:8191/v1`). When set, pages that are bot-walled/rate-limited or that render empty (JavaScript SPAs) are retried through FlareSolverr's real browser. Leave blank to disable. |

### Apache Tika (document extraction)

| Valve | Default | Description |
|---|---|---|
| `TIKA_URL` | `http://tika:9998` | Base URL of an Apache Tika server. Used to extract text from PDF/Word/Excel/PowerPoint/OpenDocument/RTF/EPUB documents the research turns up. |

### Context Control

| Valve | Default | Description |
|---|---|---|
| `SNIPPET_MAX_WORDS` | `300` | Word budget kept per source **after relevance extraction** — the passages most relevant to the query are selected up to this many words (not the first N words of the page). |
| `MAX_TOTAL_CONTEXT_WORDS` | `30000` | Soft cap on total accumulated research text. When exceeded, the lowest-relevance snippets are compressed into a summary while the highest-scored ones are kept verbatim. |

### Research Cycles

| Valve | Default | Description |
|---|---|---|
| `MIN_RESEARCH_CYCLES` | `2` | Minimum cycles before the loop is allowed to stop early on coverage. |
| `MAX_RESEARCH_CYCLES` | `5` | Hard cap on cycles. More cycles = more sources but longer runtime. |
| `QUERIES_PER_CYCLE` | `3` | Number of search queries executed per cycle. |
| `MIN_SOURCES_PER_SECTION` | `2` | Coverage target per planned section. The loop prioritises under-covered sections and keeps researching until every section has at least this many good sources (or cycles run out). |
| `MAX_LINK_HOPS_PER_CYCLE` | `3` | Multi-hop budget: how many promising links found *inside* fetched pages to follow each cycle (depth 1 only) to reach primary sources beyond the search results. `0` disables link-following. |
| `CYCLE_DELAY_SECONDS` | `0` | Seconds to pause between research cycles. Raise (e.g. 2–5) if SearXNG or source sites rate-limit you. `0` = no delay. |

### Report Length

| Valve | Default | Description |
|---|---|---|
| `REPORT_MODE` | `sectioned` | `sectioned` drafts each section in its own LLM call against only that section's sources, then a synthesis pass writes the abstract/conclusion (more thorough; more LLM calls). `single` writes the whole report in one call (legacy, cheaper). |
| `SECTION_MIN_WORDS` | `200` | Target minimum words per report section. Increase for more detailed writing. |
| `SECTION_MAX_WORDS` | `500` | Target maximum words per report section. Set to 800–1000 for thorough deep-dives. |
| `REPORT_MAX_TOKENS` | `16384` | Max tokens the LLM can generate per section (sectioned mode) or for the whole report (single mode). Increase if output is being cut off. |
| `REPORT_SOURCE_MAX_CHARS` | `2000` | Max characters of each source's text included in the report prompt. `0` = no per-source cap. |
| `REPORT_CONTEXT_MAX_CHARS` | `60000` | Total character budget for all source text in the report prompt. Sources past the budget are dropped from both the data and the citation list. Raise for fuller reports on long-context models; `0` = unbounded. |

### Fetch Settings

| Valve | Default | Description |
|---|---|---|
| `PAGE_FETCH_TIMEOUT` | `15` | Timeout in seconds for fetching individual web pages. |
| `MAX_CONCURRENT_FETCHES` | `5` | How many pages to download at the same time. Higher values are faster but may trigger rate limits. |
| `VERIFY_SSL` | `True` | Verify TLS certificates when fetching pages. Leave on; disable only if you must fetch sources with broken/self-signed certs. |

### Behaviour

| Valve | Default | Description |
|---|---|---|
| `SKIP_PLAN_CONFIRMATION` | `False` | When `True`, the pipe skips the plan review dialog and starts researching immediately. |
| `WAYBACK_FALLBACK` | `True` | When `True`, a page that stays blocked (bot wall / 429) or renders empty after the direct fetch and FlareSolverr is retried from the Internet Archive's Wayback Machine. Recovered text is flagged as a possibly-stale archived snapshot. Needs no extra service. |

## Tips

- **Longer reports:** Increase `SECTION_MAX_WORDS` to 800 or 1000, and make sure `REPORT_MAX_TOKENS` is high enough (16384+) so the model doesn't run out of generation budget. In `sectioned` mode the token budget applies per section, so longer sections are easier to get than in `single` mode.
- **More thorough research:** Increase `MAX_RESEARCH_CYCLES` to 7–10, `QUERIES_PER_CYCLE` to 4–5, and `MIN_SOURCES_PER_SECTION` to 3–4 so the loop keeps digging until each section is well covered. Raise `MAX_LINK_HOPS_PER_CYCLE` to follow more primary-source links.
- **Faster / cheaper runs:** Lower `MAX_RESEARCH_CYCLES` to 2–3 and `MIN_SOURCES_PER_SECTION` to 1, reduce `SEARCH_RESULTS_PER_QUERY` to 5, set `MAX_LINK_HOPS_PER_CYCLE` to 0, set `REPORT_MODE` to `single`, and set `SKIP_PLAN_CONFIRMATION` to `True`. Section-by-section reports and multi-hop add LLM calls and fetches, so these cut cost the most.
- **Rate limiting issues:** If SearXNG or source sites return errors, reduce `MAX_CONCURRENT_FETCHES` and `SEARCH_RESULTS_PER_QUERY`, and raise `CYCLE_DELAY_SECONDS` to pause between cycles.
- **Page refresh during research:** The pipe writes a progress snapshot into the message body early on, so if you refresh mid-research you'll see a summary of what's been collected so far rather than a blank message. The final report replaces this snapshot when complete.
