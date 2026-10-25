# Deep Research Pipe for Open WebUI

A function pipe that performs multi-cycle, LLM-driven web research and produces a structured report with citations. It searches and reads the web through the companion MCP server's `search_web` and `fetch_page` tools https://github.com/madelponte/mcp-server, and uses your chosen model to plan, analyse, and write a final report grounded entirely in the sources it found.

## How It Works

The pipe runs in three phases:

**Phase 1 — Research Plan.** When you send a query, the pipe runs a few broad exploratory searches on your topic, then asks the LLM to generate a structured research plan with named sections and targeted search queries. The plan is presented to you in a confirmation dialog. Type "ok" to proceed, describe changes (e.g. "add a section on pricing" or "focus more on the European market") and it will adjust the plan before continuing, or press **Cancel** to stop. Set `SKIP_PLAN_CONFIRMATION` to `True` to skip this step and go straight to researching.

**Phase 2 — Research Loop.** The pipe researches one planned section at a time, prioritising whichever sections have the fewest sources so far (gap-driven), across multiple cycles. Each cycle runs its queries through `search_web`, reads the top results in full with `fetch_page`, and **extracts the passages most relevant to the query that surfaced each page** instead of keeping the page head. Each passage is stored with its section, the originating query, a relevance score, and the source's page date when one is available. Results beyond the per-cycle read budget, and any page `fetch_page` can't read, still contribute the query-relevant excerpts `search_web` already returned, so no result is thrown away.

The loop also does three things beyond running the planned queries:

- **Follows links (multi-hop).** It follows a few promising links found inside fetched pages to reach primary sources the search didn't rank. Citation markers such as `[12]` (which `fetch_page` resolves to the footnote's source URL) are ranked by the sentence they're attached to.
- **Pulls threads.** After each cycle, a cheap LLM pass over the new passages proposes follow-up searches for specific studies, datasets, people, organisations, or counter-claims worth their own query.
- **Fills gaps.** If a section stays short of sources, the LLM generates new queries aimed specifically at it.

The loop keeps going until every planned section meets its coverage target (`MIN_SOURCES_PER_SECTION`) or the cycle cap is reached. When the accumulated context gets too large, the **lowest-relevance** snippets are compressed into a summary while the highest-scored ones are kept verbatim.

On time-sensitive topics:

- The plan's recency hint is passed to `search_web`'s `time_range`.
- Equal-relevance sources are kept fresher-first when context is compressed.
- Each source's date is shown in the report's reference list, and clearly old sources are flagged *may be outdated*.

This phase runs fully autonomously; you just see status updates in the UI.

**What the MCP server handles.** All network access goes through the MCP server, so the pipe inherits everything tuned there:

- Brave LLM Context search with relevance-ranked excerpts.
- `fetch_page`'s SSRF guard, FlareSolverr-first rendering, and page quality gate.
- Optional Firecrawl fallback and Tika extraction for PDF/Office/OpenDocument/RTF/EPUB documents.
- Reddit handling and YouTube transcripts.
- Caching and rate-limit pacing.

**Phase 3 — Report.** By default (`REPORT_MODE="sectioned"`), each report section is drafted in its own LLM call against **only that section's gathered sources** (plus shared cross-cutting context). A final synthesis pass then writes the title, abstract, and conclusion from the drafted sections. Every source gets one **global `[n]` number** shared across all section prompts, so a citation always points at the source the model was actually shown. If the research exceeds `REPORT_CONTEXT_MAX_CHARS`, sources past the budget are dropped from both the data and the citation list together, so nothing is cited without being seen. Set `REPORT_MODE="single"` to fall back to the one-shot report, which is cheaper and makes fewer LLM calls. Citations are also emitted as clickable source chips below the message.

**Follow-up questions.** Messages you send after a report in the same chat are answered by `RESEARCH_MODEL` from the report and its sources, without running new research. Start a new chat for a new research topic.

## Compatibility

This version targets Open WebUI **0.12.4 or later**:

- It receives MCP tools through the pipe's `__tools__` parameter and can open its own connection to a configured MCP tool server.
- It reads the user's prompt from `base_user_prompt`.
- It uses the current event, citation, and input-dialog payloads, and the async per-key configuration API for optional embeddings.

## Installation

1. **Add the MCP server to Open WebUI.** Go to **Admin Settings → External Tools**, add a connection of type **MCP (Streamable HTTP)** pointing at `http://<host>:8000/mcp`, use Bearer auth with one of the server's `auth_tokens`, and give it an **ID** (for example `mcp`). Make sure the connection is enabled and its tool filter (if any) allows `search_web` and `fetch_page`.
2. Go to **Admin Settings → Functions**, create a function, and paste the contents of `deep_research_pipe.py`.
3. Save and enable the function.
4. Configure the function valves (see below). You must set `RESEARCH_MODEL`. Set `MCP_SERVER_ID` to the connection's ID if you have more than one MCP server that exposes these tools.
5. Start a new chat and select **Deep Research** from the model picker.

You don't need to enable the MCP tools on each chat. If the server's tools are enabled on the chat (or attached to the Deep Research model), the pipe uses those. Otherwise it connects to the server itself, using the connection's own auth and access settings, and closes that connection when the run ends. The user running the research needs access to that connection.

Leave the chat's function calling on **Native** (the default). In Legacy mode, Open WebUI pre-runs attached tools with its task model before the pipe starts.

## Requirements

- This repository's **MCP server**, with `search_web` (needs `web_search.brave_api_key`) and `fetch_page` enabled. For full page reading, run it with its support services (`docker compose up` starts FlareSolverr and Tika).
- A model that handles long contexts well (the report prompts can be large).
- **Optional:** an embedding model configured in Open WebUI (see `EMBEDDING_MODEL`).

## Valves Reference

Valves from earlier versions that no longer exist are ignored when the function loads, so an existing install upgrades in place: `SEARXNG_URL`, `SEARCH_ENGINES`, `FLARESOLVERR_URL`, `TIKA_URL`, `PAGE_FETCH_TIMEOUT`, `MAX_DOWNLOAD_BYTES`, `SSRF_ALLOWLIST`, `VERIFY_SSL`, `WAYBACK_FALLBACK`, and `CYCLE_DELAY_SECONDS`.

### Model Configuration

| Valve | Default | Description |
|---|---|---|
| `RESEARCH_MODEL` | *(empty)* | **Required.** Accessible Open WebUI model ID for all LLM calls (planning, analysis, report writing, follow-ups). It cannot be the Deep Research pipe itself. |
| `EMBEDDING_MODEL` | *(empty)* | Optional. Enables **semantic** relevance ranking of page passages. The query and each candidate segment are embedded through Open WebUI's configured embeddings endpoint and ranked by cosine similarity, which catches synonym and paraphrase matches the lexical scorer misses. For the `openai`/`ollama`/`azure_openai` RAG engines this exact model ID is used; for the local engine, Open WebUI's configured embedding model is used. If the endpoint isn't configured or a request fails, the built-in lexical (keyword-overlap) scorer is used instead. Blank = lexical-only. |

### MCP Server

| Valve | Default | Description |
|---|---|---|
| `MCP_SERVER_ID` | *(empty)* | ID of the MCP tool-server connection that provides `search_web` and `fetch_page`. Blank = auto-detect: use the tools enabled on the chat, otherwise connect to the first enabled MCP connection that exposes both. |
| `SEARCH_RESULTS_PER_QUERY` | `5` | Max results per `search_web` query (its `num_results`). The server clamps it to its own configured cap. `0` = the server's cap. |
| `SEARCH_TIME_RANGE` | *(empty)* | Freshness filter on every search: blank, `day`, `week`, `month`, `year`, `all`, or `YYYY-MM-DD to YYYY-MM-DD`. Blank = the server's default. The research plan's recency hint overrides this per run. |
| `MAX_PAGE_FETCHES_PER_CYCLE` | `10` | How many new search results per cycle are read in full with `fetch_page`. The rest keep their search excerpts. `0` = excerpts only (much faster, shallower). |
| `MAX_CONCURRENT_FETCHES` | `5` | Max `fetch_page` calls in flight at once. |
| `TOOL_CALL_TIMEOUT` | `180` | Seconds to wait for one `search_web`/`fetch_page` call before giving up on it (the source then falls back to its excerpts). `0` = rely on the server's own deadlines. |

### Context Control

| Valve | Default | Description |
|---|---|---|
| `SNIPPET_MAX_WORDS` | `300` | Word budget kept per source **after relevance extraction**: the passages most relevant to the query are kept up to this many words (not the first N words of the page). |
| `MAX_TOTAL_CONTEXT_WORDS` | `30000` | Soft cap on total accumulated research text. When it's exceeded, the lowest-relevance snippets are compressed into a summary while the highest-scored ones are kept verbatim. |

### Research Cycles

| Valve | Default | Description |
|---|---|---|
| `MIN_RESEARCH_CYCLES` | `2` | Minimum cycles before the loop is allowed to stop early on coverage. |
| `MAX_RESEARCH_CYCLES` | `5` | Hard cap on cycles. More cycles = more sources but longer runtime. |
| `QUERIES_PER_CYCLE` | `3` | Number of search queries run per cycle. |
| `MIN_SOURCES_PER_SECTION` | `2` | Coverage target per planned section. The loop prioritises under-covered sections and keeps researching until every section has at least this many good sources (or cycles run out). |
| `MAX_LINK_HOPS_PER_CYCLE` | `3` | Multi-hop budget: how many promising links found *inside* fetched pages to follow each cycle (depth 1 only). `0` disables link-following. |
| `FOLLOWUP_QUERIES_PER_CYCLE` | `2` | Thread-pulling: after each cycle, a cheap LLM pass proposes up to this many follow-up searches drawn from the passages just gathered. `0` disables it (plan + gap-filling only). |

### Report Length

| Valve | Default | Description |
|---|---|---|
| `REPORT_MODE` | `sectioned` | `sectioned` drafts each section in its own LLM call against only that section's sources, then a synthesis pass writes the abstract and conclusion (more thorough; more LLM calls). `single` writes the whole report in one call (cheaper). |
| `SECTION_MIN_WORDS` | `200` | Target minimum words per report section. |
| `SECTION_MAX_WORDS` | `500` | Target maximum words per report section. Set to 800–1000 for thorough deep-dives. |
| `REPORT_MAX_TOKENS` | `16384` | Max tokens the LLM can generate per section (sectioned mode) or for the whole report (single mode). Increase it if output is being cut off. |
| `REPORT_SOURCE_MAX_CHARS` | `2000` | Max characters of each source's text included in the report prompt. `0` = no per-source cap. |
| `REPORT_CONTEXT_MAX_CHARS` | `60000` | Total character budget for all source text in the report prompt. Sources past the budget are dropped from both the data and the citation list. `0` = unbounded. |

### Behaviour

| Valve | Default | Description |
|---|---|---|
| `SKIP_PLAN_CONFIRMATION` | `False` | When `True`, the pipe skips the plan review dialog and starts researching immediately. |

## Tips

- **Longer reports:** Increase `SECTION_MAX_WORDS` to 800 or 1000, and make sure `REPORT_MAX_TOKENS` is high enough (16384+). In `sectioned` mode the token budget applies per section.
- **More thorough research:** Increase `MAX_RESEARCH_CYCLES` to 7–10, `QUERIES_PER_CYCLE` to 4–5, and `MIN_SOURCES_PER_SECTION` to 3–4. Raise `MAX_LINK_HOPS_PER_CYCLE` to follow more primary-source links.
- **Faster / cheaper runs:** Lower `MAX_RESEARCH_CYCLES` to 2–3 and `MIN_SOURCES_PER_SECTION` to 1, set `MAX_PAGE_FETCHES_PER_CYCLE` to 0–3 (search excerpts are often enough), set `MAX_LINK_HOPS_PER_CYCLE` and `FOLLOWUP_QUERIES_PER_CYCLE` to 0, set `REPORT_MODE` to `single`, and set `SKIP_PLAN_CONFIRMATION` to `True`.
- **Rate limiting:** The MCP server paces Brave requests itself (`web_search.brave_request_delay_seconds`). If source sites push back, lower `MAX_CONCURRENT_FETCHES`.
- **"Needs the MCP server's search_web and fetch_page tools":** Check that the External Tools connection is enabled, that its tool filter allows both tools, and that the user has access to it. If you have several MCP connections, set `MCP_SERVER_ID`. The Open WebUI server log shows the connection error.
- **Page refresh during research:** The pipe writes a progress snapshot into the message body early on, so if you refresh mid-research you'll see a summary of what's been collected so far rather than a blank message. The final report replaces this snapshot when complete.
