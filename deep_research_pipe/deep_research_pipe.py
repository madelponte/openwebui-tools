"""
title: Deep Research
author: mdelponte
version: 1.2.0
license: MIT
description: >
    A deep research pipe that takes a user query, generates a research plan
    using an LLM, presents it for user confirmation, then iteratively
    searches the web via SearXNG, fetches and extracts page content, and
    synthesizes a final structured report.  Supports FlareSolverr for
    bypassing captchas.
required_open_webui_version: 0.5.0
"""

import asyncio
import json
import logging
import re
import time
from io import BytesIO
from typing import (
    Any,
    Awaitable,
    Callable,
    Dict,
    List,
    Optional,
    Tuple,
)

import aiohttp
from pydantic import BaseModel, Field

from open_webui.utils.chat import generate_chat_completion
from open_webui.utils.misc import pop_system_message, get_last_user_message
from open_webui.models.users import Users

# ---------------------------------------------------------------------------
# Logger
# ---------------------------------------------------------------------------
_PIPE_NAME = "deep_research"


def _setup_logger() -> logging.Logger:
    logger = logging.getLogger(_PIPE_NAME)
    if not logger.handlers:
        logger.setLevel(logging.DEBUG)
        handler = logging.StreamHandler()
        handler.setFormatter(
            logging.Formatter(
                "%(asctime)s - %(name)s - %(levelname)s - %(message)s"
            )
        )
        logger.addHandler(handler)
        logger.propagate = False
    return logger


log = _setup_logger()

# Marker we embed in the message body so we can detect that this
# conversation already contains a completed research report.  If the
# pipe is re-invoked (Open WebUI can do this after the first response
# is appended to the chat) we check for this marker in the existing
# assistant messages and bail out immediately rather than starting a
# second research run.
_DONE_MARKER = "<!-- deep-research-complete -->"


# ---------------------------------------------------------------------------
# Pipe class
# ---------------------------------------------------------------------------
class Pipe:
    """Deep Research pipe for Open WebUI."""

    # -----------------------------------------------------------------------
    # Valves
    # -----------------------------------------------------------------------
    class Valves(BaseModel):
        # --- Model configuration ---
        RESEARCH_MODEL: str = Field(
            default="",
            description=(
                "Model ID used for research planning, query generation, "
                "snippet analysis, and final report writing. "
                "Leave blank to use the default model."
            ),
        )
        EMBEDDING_MODEL: str = Field(
            default="",
            description=(
                "Embedding model ID for parsing PDFs or long documents. "
                "Leave blank to skip PDF embedding."
            ),
        )

        # --- Search engine ---
        SEARXNG_URL: str = Field(
            default="http://searxng:8080",
            description="Base URL of your SearXNG instance.",
        )
        SEARCH_RESULTS_PER_QUERY: int = Field(
            default=10,
            description="Number of search results per query.",
        )
        SEARCH_ENGINES: str = Field(
            default="",
            description=(
                "Comma-separated SearXNG engines "
                "(e.g. 'google,bing,duckduckgo'). "
                "Blank = SearXNG defaults."
            ),
        )

        # --- FlareSolverr (optional) ---
        FLARESOLVERR_URL: str = Field(
            default="",
            description=(
                "FlareSolverr URL (e.g. http://flaresolverr:8191/v1). "
                "Blank = disabled."
            ),
        )

        # --- Snippet / context control ---
        SNIPPET_MAX_WORDS: int = Field(
            default=300,
            description="Max words kept per fetched page snippet.",
        )
        MAX_TOTAL_CONTEXT_WORDS: int = Field(
            default=30000,
            description=(
                "Soft cap on accumulated research context (words). "
                "Older snippets are compressed when exceeded."
            ),
        )

        # --- Research cycle control ---
        MIN_RESEARCH_CYCLES: int = Field(
            default=2,
            description="Minimum search-analyse cycles.",
        )
        MAX_RESEARCH_CYCLES: int = Field(
            default=5,
            description="Maximum search-analyse cycles.",
        )
        QUERIES_PER_CYCLE: int = Field(
            default=3,
            description="Search queries per cycle.",
        )

        # --- Report length control ---
        SECTION_MIN_WORDS: int = Field(
            default=200,
            description=(
                "Target minimum words per report section. "
                "Increase for more detailed reports."
            ),
        )
        SECTION_MAX_WORDS: int = Field(
            default=500,
            description=(
                "Target maximum words per report section. "
                "Increase for longer, more thorough sections."
            ),
        )
        REPORT_MAX_TOKENS: int = Field(
            default=16384,
            description=(
                "Max tokens the LLM may generate for the final report. "
                "Increase if the report is being truncated."
            ),
        )

        # --- Fetch settings ---
        PAGE_FETCH_TIMEOUT: int = Field(
            default=15,
            description="Timeout (seconds) for fetching web pages.",
        )
        MAX_CONCURRENT_FETCHES: int = Field(
            default=5,
            description="Max pages to fetch concurrently.",
        )

        # --- Behaviour ---
        SKIP_PLAN_CONFIRMATION: bool = Field(
            default=False,
            description="Skip the user-confirmation step for the plan.",
        )

    # -----------------------------------------------------------------------
    # Init
    # -----------------------------------------------------------------------
    def __init__(self):
        self.type = "pipe"
        self.id = "deep_research"
        self.name = "Deep Research"
        self.valves = self.Valves()

    # -----------------------------------------------------------------------
    # LLM helper (internal, non-streaming)
    # -----------------------------------------------------------------------
    async def _llm_call(
        self,
        messages: List[Dict[str, str]],
        request: Any,
        user: Any,
        *,
        temperature: float = 0.3,
        max_tokens: int = 4096,
    ) -> str:
        model = self.valves.RESEARCH_MODEL or "default"
        payload: Dict[str, Any] = {
            "model": model,
            "messages": messages,
            "stream": False,
            "temperature": temperature,
            "max_tokens": max_tokens,
        }
        try:
            response = await generate_chat_completion(
                request, payload, user=user
            )
            if isinstance(response, dict):
                return (
                    response.get("choices", [{}])[0]
                    .get("message", {})
                    .get("content", "")
                    .strip()
                )
            if hasattr(response, "body_iterator"):
                full = ""
                async for chunk in response.body_iterator:
                    if isinstance(chunk, bytes):
                        chunk = chunk.decode("utf-8")
                    for line in chunk.strip().split("\n"):
                        line = line.strip()
                        if not line:
                            continue
                        if line.startswith("data: "):
                            if line == "data: [DONE]":
                                continue
                            try:
                                data = json.loads(line[6:])
                                delta = (
                                    data.get("choices", [{}])[0]
                                    .get("delta", {})
                                    .get("content", "")
                                )
                                full += delta
                            except json.JSONDecodeError:
                                pass
                        else:
                            try:
                                data = json.loads(line)
                                c = (
                                    data.get("choices", [{}])[0]
                                    .get("message", {})
                                    .get("content", "")
                                )
                                if c:
                                    full += c
                            except json.JSONDecodeError:
                                pass
                return full.strip()
            return str(response).strip()
        except Exception as e:
            log.error(f"LLM call failed: {e}")
            raise

    # -----------------------------------------------------------------------
    # Event helpers
    # -----------------------------------------------------------------------
    async def _emit_status(
        self, emitter: Optional[Callable], desc: str, done: bool = False
    ) -> None:
        if emitter:
            await emitter(
                {"type": "status", "data": {"description": desc, "done": done}}
            )

    async def _emit_message(
        self, emitter: Optional[Callable], content: str
    ) -> None:
        """Append to the assistant message (persisted in chat DB)."""
        if emitter:
            await emitter(
                {"type": "message", "data": {"content": content}}
            )

    async def _emit_replace(
        self, emitter: Optional[Callable], content: str
    ) -> None:
        """Replace the full assistant message (persisted in chat DB)."""
        if emitter:
            await emitter(
                {"type": "replace", "data": {"content": content}}
            )

    async def _emit_citation(
        self,
        emitter: Optional[Callable],
        url: str,
        title: str,
        snippet: str,
    ) -> None:
        if emitter:
            await emitter(
                {
                    "type": "citation",
                    "data": {
                        "document": [snippet[:500]],
                        "metadata": [
                            {
                                "date_accessed": time.strftime(
                                    "%Y-%m-%d %H:%M:%S"
                                ),
                                "source": {
                                    "name": title or url,
                                    "url": url,
                                },
                            }
                        ],
                        "source": {"name": title or url, "url": url},
                    },
                }
            )

    # -----------------------------------------------------------------------
    # Web fetching
    # -----------------------------------------------------------------------
    async def _fetch_page(
        self, session: aiohttp.ClientSession, url: str
    ) -> Tuple[str, str]:
        text = ""
        try:
            async with session.get(
                url,
                timeout=aiohttp.ClientTimeout(
                    total=self.valves.PAGE_FETCH_TIMEOUT
                ),
                headers={
                    "User-Agent": (
                        "Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
                        "AppleWebKit/537.36 (KHTML, like Gecko) "
                        "Chrome/125.0.0.0 Safari/537.36"
                    ),
                    "Accept": (
                        "text/html,application/xhtml+xml,"
                        "application/xml;q=0.9,*/*;q=0.8"
                    ),
                },
                allow_redirects=True,
                ssl=False,
            ) as resp:
                if resp.status == 200:
                    ct = resp.headers.get("Content-Type", "")
                    if "pdf" in ct.lower():
                        text = await self._extract_pdf(await resp.read())
                    else:
                        text = self._html_to_text(
                            await resp.text(errors="replace")
                        )
        except Exception as e:
            log.debug(f"Direct fetch failed for {url}: {e}")

        if not text.strip() and self.valves.FLARESOLVERR_URL:
            text = await self._flaresolverr(session, url)

        words = text.split()
        if len(words) > self.valves.SNIPPET_MAX_WORDS:
            text = (
                " ".join(words[: self.valves.SNIPPET_MAX_WORDS])
                + " [...]"
            )
        return url, text

    async def _flaresolverr(
        self, session: aiohttp.ClientSession, url: str
    ) -> str:
        try:
            async with session.post(
                self.valves.FLARESOLVERR_URL,
                json={
                    "cmd": "request.get",
                    "url": url,
                    "maxTimeout": self.valves.PAGE_FETCH_TIMEOUT * 1000,
                },
                timeout=aiohttp.ClientTimeout(
                    total=self.valves.PAGE_FETCH_TIMEOUT + 30
                ),
            ) as resp:
                if resp.status == 200:
                    data = await resp.json()
                    html = data.get("solution", {}).get("response", "")
                    if html:
                        return self._html_to_text(html)
        except Exception as e:
            log.debug(f"FlareSolverr failed for {url}: {e}")
        return ""

    @staticmethod
    def _html_to_text(html: str) -> str:
        text = re.sub(
            r"<(script|style|noscript)[^>]*>.*?</\1>",
            "", html, flags=re.DOTALL | re.IGNORECASE,
        )
        text = re.sub(r"<[^>]+>", " ", text)
        for ent, ch in [
            ("&amp;", "&"), ("&lt;", "<"), ("&gt;", ">"),
            ("&quot;", '"'), ("&#39;", "'"), ("&nbsp;", " "),
        ]:
            text = text.replace(ent, ch)
        return re.sub(r"\s+", " ", text).strip()

    async def _extract_pdf(self, data: bytes) -> str:
        try:
            import pypdf
            reader = pypdf.PdfReader(BytesIO(data))
            return "\n".join(
                p.extract_text() for p in reader.pages
                if p.extract_text()
            )
        except ImportError:
            return "[PDF — pypdf not installed]"
        except Exception:
            return "[PDF — extraction failed]"

    # -----------------------------------------------------------------------
    # SearXNG
    # -----------------------------------------------------------------------
    async def _search(
        self, session: aiohttp.ClientSession, query: str
    ) -> List[Dict[str, str]]:
        params: Dict[str, Any] = {
            "q": query,
            "format": "json",
            "number_of_results": self.valves.SEARCH_RESULTS_PER_QUERY,
        }
        if self.valves.SEARCH_ENGINES:
            params["engines"] = self.valves.SEARCH_ENGINES
        try:
            async with session.get(
                f"{self.valves.SEARXNG_URL.rstrip('/')}/search",
                params=params,
                timeout=aiohttp.ClientTimeout(total=15),
            ) as resp:
                if resp.status == 200:
                    data = await resp.json()
                    return [
                        {
                            "title": r.get("title", ""),
                            "url": r.get("url", ""),
                            "snippet": r.get("content", ""),
                        }
                        for r in data.get("results", [])
                    ]
                log.warning(f"SearXNG {resp.status} for: {query}")
        except Exception as e:
            log.error(f"SearXNG error: {e}")
        return []

    # -----------------------------------------------------------------------
    # Phase 1 — research plan
    # -----------------------------------------------------------------------
    async def _generate_plan(
        self,
        query: str,
        request: Any,
        user: Any,
        session: aiohttp.ClientSession,
        emitter: Optional[Callable],
    ) -> Tuple[str, List[str], Dict]:
        await self._emit_status(emitter, "🔍 Exploratory searches…")

        exploratory = [query]
        words = query.split()
        if len(words) > 3:
            exploratory.append(" ".join(words[:4]) + " overview")
        exploratory.append(query + " recent developments")

        snippets: List[str] = []
        for eq in exploratory[:3]:
            for r in (await self._search(session, eq))[:5]:
                if r["snippet"]:
                    snippets.append(
                        f"- [{r['title']}]({r['url']}): "
                        f"{r['snippet'][:200]}"
                    )

        ctx = "\n".join(snippets[:20])
        await self._emit_status(emitter, "🧠 Generating research plan…")

        prompt = f"""You are a research planning assistant. The user wants to deeply research:

**User Query:** {query}

Initial search snippets for context:
{ctx}

Create a detailed research plan as valid JSON:
{{
    "plan_summary": "2-3 sentence summary of the approach",
    "sections": [
        {{
            "title": "Section title",
            "description": "What this section covers",
            "search_queries": ["query1", "query2"]
        }}
    ],
    "initial_queries": ["first 3-5 search queries"]
}}

Guidelines:
- 4-8 comprehensive sections with 2-3 search queries each
- Include diverse perspectives, data, expert opinions
- Respond with ONLY JSON — no markdown fences, no extra text."""

        raw = await self._llm_call(
            [{"role": "user", "content": prompt}],
            request, user, temperature=0.4,
        )
        raw = re.sub(r"```(?:json)?\s*", "", raw)
        raw = re.sub(r"```\s*$", "", raw).strip()

        try:
            plan = json.loads(raw)
        except json.JSONDecodeError:
            plan = {
                "plan_summary": f"Research plan for: {query}",
                "sections": [
                    {
                        "title": "General Overview",
                        "description": "Broad overview",
                        "search_queries": [query, f"{query} overview"],
                    }
                ],
                "initial_queries": [
                    query, f"{query} latest", f"{query} analysis"
                ],
            }

        text = "## 📋 Research Plan\n\n"
        text += f"**Summary:** {plan.get('plan_summary', '')}\n\n"
        text += "### Planned Sections:\n\n"
        for i, s in enumerate(plan.get("sections", []), 1):
            text += f"**{i}. {s['title']}**\n"
            text += f"   {s.get('description', '')}\n\n"

        return text, plan.get("initial_queries", [query]), plan

    # -----------------------------------------------------------------------
    # Phase 2 — research loop (fully autonomous)
    # -----------------------------------------------------------------------
    async def _research_loop(
        self,
        query: str,
        plan: Dict,
        request: Any,
        user: Any,
        session: aiohttp.ClientSession,
        emitter: Optional[Callable],
    ) -> Tuple[List[Dict[str, str]], List[str]]:
        collected: List[Dict[str, str]] = []
        seen_urls: set = set()
        source_urls: List[str] = []

        # Build de-duped query queue
        queue: List[str] = []
        seen_q: set = set()
        for q in (
            plan.get("initial_queries", [query])
            + [
                sq
                for sec in plan.get("sections", [])
                for sq in sec.get("search_queries", [])
            ]
        ):
            ql = q.strip().lower()
            if ql and ql not in seen_q:
                seen_q.add(ql)
                queue.append(q.strip())

        cycle = 0
        qi = 0  # queue index

        while cycle < self.valves.MAX_RESEARCH_CYCLES:
            cycle += 1
            await self._emit_status(
                emitter,
                f"🔄 Cycle {cycle}/{self.valves.MAX_RESEARCH_CYCLES} · "
                f"{len(collected)} snippets · "
                f"{len(source_urls)} sources",
            )

            # Pick queries
            cqs: List[str] = []
            for _ in range(self.valves.QUERIES_PER_CYCLE):
                if qi < len(queue):
                    cqs.append(queue[qi])
                    qi += 1

            if not cqs and cycle <= self.valves.MIN_RESEARCH_CYCLES:
                await self._emit_status(
                    emitter, "🧠 Generating follow-up queries…"
                )
                cqs = await self._followup_queries(
                    query, collected, request, user
                )
            elif not cqs:
                break

            # Search
            results: List[Dict[str, str]] = []
            for cq in cqs:
                await self._emit_status(
                    emitter, f"🔍 Searching: {cq[:80]}…"
                )
                for r in await self._search(session, cq):
                    if r["url"] and r["url"] not in seen_urls:
                        results.append(r)
                        seen_urls.add(r["url"])

            if not results:
                if cycle >= self.valves.MIN_RESEARCH_CYCLES:
                    break
                continue

            # Fetch
            cap = self.valves.MAX_CONCURRENT_FETCHES * 2
            await self._emit_status(
                emitter,
                f"📄 Fetching {min(len(results), cap)} pages…",
            )
            sem = asyncio.Semaphore(self.valves.MAX_CONCURRENT_FETCHES)

            async def _bf(u: str) -> Tuple[str, str]:
                async with sem:
                    return await self._fetch_page(session, u)

            fetched = await asyncio.gather(
                *[_bf(r["url"]) for r in results[:cap]],
                return_exceptions=True,
            )
            new = 0
            for item in fetched:
                if isinstance(item, Exception):
                    continue
                url, text = item
                if text and len(text.split()) > 20:
                    title = url
                    for r in results:
                        if r["url"] == url:
                            title = r.get("title", url)
                            break
                    collected.append(
                        {"url": url, "title": title, "content": text}
                    )
                    if url not in source_urls:
                        source_urls.append(url)
                    new += 1

            await self._emit_status(
                emitter,
                f"✅ Cycle {cycle}: +{new} snippets "
                f"(total {len(collected)} · {len(source_urls)} sources)",
            )

            # Compress if too large
            tw = sum(len(s["content"].split()) for s in collected)
            if tw > self.valves.MAX_TOTAL_CONTEXT_WORDS:
                await self._emit_status(
                    emitter, "📦 Compressing older notes…"
                )
                collected = await self._compress(
                    query, collected, request, user
                )

            # LLM decides whether to continue
            if cycle >= self.valves.MIN_RESEARCH_CYCLES:
                if not await self._should_continue(
                    query, collected, plan, cycle, request, user
                ):
                    await self._emit_status(
                        emitter,
                        f"🏁 Sufficient coverage after {cycle} cycles",
                    )
                    break

        return collected, source_urls

    async def _followup_queries(
        self, query: str, collected: List[Dict], request: Any, user: Any
    ) -> List[str]:
        summary = "\n".join(
            f"- {s['title']}: {' '.join(s['content'].split()[:50])}…"
            for s in collected[-10:]
        )
        raw = await self._llm_call(
            [
                {
                    "role": "user",
                    "content": (
                        f"Generate {self.valves.QUERIES_PER_CYCLE} new "
                        f"search queries to fill gaps.\n\n"
                        f"Original: {query}\n\nRecent:\n{summary}\n\n"
                        f"Respond with ONLY a JSON array of strings."
                    ),
                }
            ],
            request, user, temperature=0.5,
        )
        raw = re.sub(r"```(?:json)?\s*", "", raw)
        raw = re.sub(r"```\s*$", "", raw).strip()
        try:
            qs = json.loads(raw)
            if isinstance(qs, list):
                return [str(q) for q in qs[: self.valves.QUERIES_PER_CYCLE]]
        except json.JSONDecodeError:
            pass
        return [f"{query} additional information"]

    async def _should_continue(
        self,
        query: str,
        collected: List[Dict],
        plan: Dict,
        cycle: int,
        request: Any,
        user: Any,
    ) -> bool:
        r = await self._llm_call(
            [
                {
                    "role": "user",
                    "content": (
                        f"Original query: {query}\n"
                        f"Planned sections: "
                        f"{json.dumps([s['title'] for s in plan.get('sections',[])])}\n"
                        f"Cycle: {cycle}/{self.valves.MAX_RESEARCH_CYCLES}\n"
                        f"Sources: {len(collected)}\n"
                        f"Titles: {json.dumps([s['title'] for s in collected[-15:]])}\n\n"
                        f'Respond ONLY "CONTINUE" or "STOP" + brief reason.'
                    ),
                }
            ],
            request, user, temperature=0.2, max_tokens=100,
        )
        return r.strip().upper().startswith("CONTINUE")

    async def _compress(
        self, query: str, collected: List[Dict], request: Any, user: Any
    ) -> List[Dict[str, str]]:
        sp = len(collected) // 2
        old, recent = collected[:sp], collected[sp:]
        old_text = "\n\n".join(
            f"Source: {s['title']} ({s['url']})\n{s['content']}"
            for s in old
        )
        summary = await self._llm_call(
            [
                {
                    "role": "user",
                    "content": (
                        f"Summarise these research notes. Keep all key "
                        f"facts/data.\nTopic: {query}\n\n"
                        f"{old_text[:15000]}"
                    ),
                }
            ],
            request, user, temperature=0.2, max_tokens=3000,
        )
        return [
            {
                "url": "compressed_summary",
                "title": f"Summary of {len(old)} earlier sources",
                "content": summary,
            }
        ] + recent

    # -----------------------------------------------------------------------
    # Phase 3 — final report
    # -----------------------------------------------------------------------
    async def _generate_report(
        self,
        query: str,
        plan: Dict,
        collected: List[Dict[str, str]],
        source_urls: List[str],
        request: Any,
        user: Any,
        emitter: Optional[Callable],
    ) -> str:
        await self._emit_status(emitter, "📝 Writing final report…")

        ctx = ""
        for i, s in enumerate(collected):
            ctx += (
                f"\n--- SOURCE {i+1}: {s['title']} "
                f"({s['url']}) ---\n"
                f"{s['content'][:1500]}\n"
            )

        secs = "\n".join(
            f"- {s['title']}: {s.get('description','')}"
            for s in plan.get("sections", [])
        )
        refs = "\n".join(
            f"[{i+1}] {u}" for i, u in enumerate(source_urls)
        )

        smin = self.valves.SECTION_MIN_WORDS
        smax = self.valves.SECTION_MAX_WORDS

        prompt = f"""You are a research report writer. Write a comprehensive, well-structured research report based ONLY on the provided research data. Do NOT rely on your own knowledge — use only the sources below.

**Research Topic:** {query}

**Planned Sections:**
{secs}

**Research Data:**
{ctx[:30000]}

**Available Sources (for citation):**
{refs}

REQUIRED FORMAT:

# [Report Title]

## Abstract
A concise summary of key findings (200-300 words).

## Table of Contents
List all sections.

## [Section 1 Title]
Detailed content based on research data. Cite sources using [n] notation.

## [Section 2 Title]
...continue for all planned sections...

## Conclusion
Synthesise findings, key takeaways, limitations, areas for further research.

## Sources
Numbered list of all sources with URLs matching [n] citations.

GUIDELINES:
- Each section MUST be {smin}-{smax} words. Do not write short paragraphs — develop each section thoroughly with analysis, examples, and data from the sources.
- Write in a clear, professional, analytical tone.
- Base ALL claims on the provided research data and cite with [n].
- Include specific data, statistics, and findings from sources.
- Cover ALL planned sections — do not skip or merge sections.
- If data is thin for a section, note the gap but still write what you can.
- Write the COMPLETE report — do not truncate, summarise prematurely, or say "continued below"."""

        return await self._llm_call(
            [{"role": "user", "content": prompt}],
            request, user,
            temperature=0.3,
            max_tokens=self.valves.REPORT_MAX_TOKENS,
        )

    # -----------------------------------------------------------------------
    # Progress snapshot (survives page refresh)
    # -----------------------------------------------------------------------
    def _progress_msg(
        self,
        phase: str,
        plan: Optional[Dict] = None,
        cycle: int = 0,
        max_cycles: int = 0,
        sources: int = 0,
        snippets: int = 0,
        urls: Optional[List[str]] = None,
    ) -> str:
        p = [f"## 🔬 Deep Research — {phase}\n"]
        if plan:
            p.append(f"**Topic:** {plan.get('plan_summary','')}\n")
            ss = plan.get("sections", [])
            if ss:
                p.append(
                    f"**Sections:** "
                    f"{', '.join(s['title'] for s in ss)}\n"
                )
        if max_cycles:
            p.append(
                f"**Progress:** cycle {cycle}/{max_cycles} · "
                f"{snippets} snippets · {sources} sources\n"
            )
        if urls:
            p.append(
                "\n<details><summary>Sources found so far</summary>\n"
            )
            for i, u in enumerate(urls, 1):
                p.append(f"{i}. {u}")
            p.append("\n</details>\n")
        p.append(
            "\n*Research in progress — this updates automatically…*\n"
        )
        return "\n".join(p)

    # -----------------------------------------------------------------------
    # Main entry point
    # -----------------------------------------------------------------------
    async def pipe(
        self,
        body: dict,
        __user__: Optional[dict] = None,
        __event_emitter__: Optional[
            Callable[[dict], Awaitable[None]]
        ] = None,
        __event_call__: Optional[
            Callable[[dict], Awaitable[Any]]
        ] = None,
        __request__: Optional[Any] = None,
        __task__: Optional[str] = None,
        __metadata__: Optional[dict] = None,
    ) -> str:
        """
        Orchestrate the deep-research workflow.

        Returns an empty string because the final report is written
        directly into the message via ``_emit_replace``.  This prevents
        Open WebUI from appending a duplicate or re-invoking the pipe.
        """
        # ==============================================================
        # TASK GUARD: Open WebUI re-invokes the pipe for background
        # tasks like title_generation, tags_generation, emoji_generation
        # and autocomplete_generation AFTER the main response.
        # These must NOT trigger a full research run.  Instead, forward
        # the request to the underlying model for a quick answer.
        # ==============================================================
        if __task__:
            log.info(f"Task call received: {__task__} — forwarding to model")
            task_model = self.valves.RESEARCH_MODEL or "default"
            task_messages = body.get("messages", [])
            try:
                user_obj = None
                if __user__:
                    user_obj = Users.get_user_by_id(__user__["id"])
                payload = {
                    "model": task_model,
                    "messages": task_messages,
                    "stream": False,
                    "temperature": 0.5,
                    "max_tokens": 200,
                }
                response = await generate_chat_completion(
                    __request__, payload, user=user_obj
                )
                if isinstance(response, dict):
                    return (
                        response.get("choices", [{}])[0]
                        .get("message", {})
                        .get("content", "Deep Research")
                    )
                if hasattr(response, "body_iterator"):
                    full = ""
                    async for chunk in response.body_iterator:
                        if isinstance(chunk, bytes):
                            chunk = chunk.decode("utf-8")
                        for line in chunk.strip().split("\n"):
                            line = line.strip()
                            if line.startswith("data: ") and line != "data: [DONE]":
                                try:
                                    data = json.loads(line[6:])
                                    delta = (
                                        data.get("choices", [{}])[0]
                                        .get("delta", {})
                                        .get("content", "")
                                    )
                                    full += delta
                                except json.JSONDecodeError:
                                    pass
                    return full.strip() or "Deep Research"
                return str(response).strip() or "Deep Research"
            except Exception as e:
                log.warning(f"Task forwarding failed: {e}")
                return "Deep Research"

        log.info("Deep Research pipe invoked — main research flow")

        # ==============================================================
        # RE-ENTRY GUARD: if the conversation already has a completed
        # report, don't run again.
        # ==============================================================
        all_messages = body.get("messages", [])
        for msg in all_messages:
            if msg.get("role") == "assistant":
                content = msg.get("content", "")
                if isinstance(content, str) and _DONE_MARKER in content:
                    log.info("Re-entry guard: report exists, skipping")
                    return ""

        # Extract user query
        _, messages = pop_system_message(all_messages)
        user_query = get_last_user_message(messages)
        if not user_query:
            return "Please provide a research topic or question."

        user_obj = None
        if __user__:
            user_obj = Users.get_user_by_id(__user__["id"])

        try:
            async with aiohttp.ClientSession() as session:

                # ======================================================
                # PHASE 1 — plan
                # ======================================================
                await self._emit_status(
                    __event_emitter__, "🚀 Starting deep research…"
                )
                plan_text, _, plan = await self._generate_plan(
                    user_query, __request__, user_obj,
                    session, __event_emitter__,
                )

                # ======================================================
                # PLAN CONFIRMATION (only user interaction point)
                # ======================================================
                if (
                    not self.valves.SKIP_PLAN_CONFIRMATION
                    and __event_call__
                ):
                    await self._emit_status(
                        __event_emitter__,
                        "⏳ Waiting for plan confirmation…",
                    )
                    confirmation = await __event_call__(
                        {
                            "type": "input",
                            "data": {
                                "title": "📋 Research Plan Review",
                                "message": (
                                    f"{plan_text}\n\n"
                                    "Type **ok** or **yes** to proceed, "
                                    "or describe changes you'd like."
                                ),
                                "placeholder": (
                                    "ok / yes / your modifications…"
                                ),
                            },
                        }
                    )

                    resp = ""
                    if isinstance(confirmation, dict):
                        resp = str(
                            confirmation.get("value", "")
                        ).strip().lower()
                    elif isinstance(confirmation, str):
                        resp = confirmation.strip().lower()

                    if resp not in {
                        "ok", "yes", "y", "continue", "proceed",
                        "go", "looks good", "lgtm", "approve",
                        "confirmed", "",
                    }:
                        await self._emit_status(
                            __event_emitter__,
                            "🔄 Adjusting plan…",
                        )
                        mod = await self._llm_call(
                            [
                                {
                                    "role": "user",
                                    "content": (
                                        "Modify this research plan.\n\n"
                                        f"Query: {user_query}\n"
                                        f"Plan: {json.dumps(plan)}\n"
                                        f"Changes: {resp}\n\n"
                                        "Return updated JSON only."
                                    ),
                                }
                            ],
                            __request__, user_obj, temperature=0.4,
                        )
                        mod = re.sub(r"```(?:json)?\s*", "", mod)
                        mod = re.sub(r"```\s*$", "", mod).strip()
                        try:
                            plan = json.loads(mod)
                        except json.JSONDecodeError:
                            log.warning("Modified plan parse failed")

                # ======================================================
                # Write initial progress into message body
                # ======================================================
                await self._emit_message(
                    __event_emitter__,
                    self._progress_msg(
                        "Starting research…", plan=plan
                    ),
                )

                # ======================================================
                # PHASE 2 — research loop (autonomous, no user prompts)
                # ======================================================
                await self._emit_status(
                    __event_emitter__, "🔬 Researching…"
                )
                collected, source_urls = await self._research_loop(
                    user_query, plan, __request__, user_obj,
                    session, __event_emitter__,
                )

                if not collected:
                    msg = (
                        "Unable to gather sufficient research data. "
                        "Check SearXNG availability or refine the query."
                    )
                    await self._emit_replace(__event_emitter__, msg)
                    await self._emit_status(
                        __event_emitter__,
                        "⚠️ No data collected", done=True,
                    )
                    return ""

                # Update progress
                await self._emit_replace(
                    __event_emitter__,
                    self._progress_msg(
                        "Writing report…",
                        plan=plan,
                        cycle=self.valves.MAX_RESEARCH_CYCLES,
                        max_cycles=self.valves.MAX_RESEARCH_CYCLES,
                        sources=len(source_urls),
                        snippets=len(collected),
                        urls=source_urls,
                    ),
                )

                # ======================================================
                # PHASE 3 — report
                # ======================================================
                report = await self._generate_report(
                    user_query, plan, collected, source_urls,
                    __request__, user_obj, __event_emitter__,
                )

                # Append the done-marker so the re-entry guard works
                report_with_marker = report + "\n\n" + _DONE_MARKER

                await self._emit_replace(
                    __event_emitter__, report_with_marker
                )

                # Citations
                for s in collected:
                    if s["url"] and s["url"] != "compressed_summary":
                        await self._emit_citation(
                            __event_emitter__,
                            s["url"], s["title"],
                            s["content"][:300],
                        )

                await self._emit_status(
                    __event_emitter__,
                    f"✅ Done — {len(source_urls)} sources, "
                    f"{len(plan.get('sections',[]))} sections",
                    done=True,
                )

                # Return empty — report is already in the message
                # body via _emit_replace.  Returning content here
                # would cause Open WebUI to append it as a second
                # response or re-trigger the pipe.
                return ""

        except Exception as e:
            log.error(f"Deep Research error: {e}", exc_info=True)
            await self._emit_status(
                __event_emitter__,
                f"❌ Failed: {e}", done=True,
            )
            return f"Research error: {e}"
