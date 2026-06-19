"""
title: Deep Research
author: mdelponte
version: 2.4.0
license: MIT
description: >
    A deep research pipe that takes a user query, generates a research plan,
    presents it for confirmation, then runs a section-attributed, gap-driven
    research loop and writes a structured, citation-grounded report. Fetched
    pages are relevance-extracted (the passages matching each query, not the
    page head), de-duplicated, and the loop follows promising in-page links
    (multi-hop) and biases toward recent sources when the topic is time-
    sensitive. The report is written section by section against each section's
    own sources under one global citation index. Fetching mirrors the companion
    fetch_page MCP tool's resilient ladder: Apache Tika for PDF/Office/
    OpenDocument/RTF/EPUB documents, bot-wall/CAPTCHA/429 detection that
    re-renders through FlareSolverr, a Wayback Machine fallback for pages
    that stay blocked or render empty, and YouTube transcript extraction.
required_open_webui_version: 0.9.0
"""

import asyncio
import json
import logging
import math
import re
import time
from datetime import datetime, timezone
from urllib.parse import (
    urlparse,
    urlunparse,
    urljoin,
    parse_qs,
    parse_qsl,
    urlencode,
)
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

# YouTube transcript support is optional: a research run may surface a YouTube
# video URL whose real content is the spoken transcript, not the scrapeable
# watch page. If the open-source `youtube-transcript-api` library (>= 1.0) is
# installed in the Open WebUI environment we use it; if not, the pipe logs once
# and falls back to a normal HTML fetch, so this stays a zero-config optional.
try:
    from youtube_transcript_api import YouTubeTranscriptApi

    _YT_AVAILABLE = True
except Exception:  # pragma: no cover - import guard
    YouTubeTranscriptApi = None  # type: ignore
    _YT_AVAILABLE = False

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
            logging.Formatter("%(asctime)s - %(name)s - %(levelname)s - %(message)s")
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
_DONE_MARKER = "\n\n---DEEP-RESEARCH-COMPLETE---"


# ---------------------------------------------------------------------------
# Fetch infrastructure constants (ported from the fetch_page MCP tool)
# ---------------------------------------------------------------------------

# Bot-wall / CAPTCHA detection. A challenge page is what should route a fetch to
# FlareSolverr (a real browser) instead of being returned as if it were content.
# Cloudflare is the common case but not the only one: PerimeterX/HUMAN, DataDome,
# and Akamai Bot Manager all serve a 403 (sometimes a 200) whose body is a
# JS/CAPTCHA challenge with none of the Cloudflare markers, so they're matched
# explicitly or the fallback never fires.
BLOCK_STATUS_CODES = {403, 503, 520, 521, 522, 523, 524, 525, 526, 527}
CLOUDFLARE_MARKERS = (
    "cf-ray",
    "cf-chl",
    "just a moment",
    "attention required",
    "cf-browser-verification",
    "cf_chl_opt",
    "challenge-platform",
    "please enable cookies",
    "/cdn-cgi/challenge-platform",
)
CHALLENGE_MARKERS = (
    "px-cloud.net",
    "/captcha/captcha.js",
    "px-captcha",
    "perimeterx",
    "_pxhd",
    "datadome",
    "geo.captcha-delivery.com",
    "ak_bmsc",
    "/_sec/cp_challenge",
    "access to this page has been denied",
    "confirm you are a human",
    "and not a bot",
)
ALL_BLOCK_MARKERS = CLOUDFLARE_MARKERS + CHALLENGE_MARKERS

# Document types Apache Tika can extract that are NOT served as text/html. Tika
# auto-detects the format from the bytes, so any of these is routed to it rather
# than treated as HTML.
TIKA_DOCUMENT_CTYPES = (
    "application/pdf",
    "application/msword",
    "application/vnd.openxmlformats-officedocument",  # docx/xlsx/pptx (prefix)
    "application/vnd.ms-excel",
    "application/vnd.ms-powerpoint",
    "application/vnd.oasis.opendocument",  # odt/ods/odp (prefix)
    "application/rtf",
    "text/rtf",
    "application/epub+zip",
)
TIKA_DOCUMENT_EXTENSIONS = (
    ".pdf",
    ".doc",
    ".docx",
    ".xls",
    ".xlsx",
    ".ppt",
    ".pptx",
    ".odt",
    ".ods",
    ".odp",
    ".rtf",
    ".epub",
)

# Wayback Machine availability endpoint (no API key / local service needed).
WAYBACK_AVAILABILITY_API = "https://archive.org/wayback/available"

# A run of two or more letters — i.e. an actual word. A rendered body without
# even one has no readable text and is an empty shell, even when not strictly
# whitespace: a partly-rendered JS page can leave stray punctuation like "; ;"
# that defeats a bare `.strip()` test.
_WORD_RE = re.compile(r"[^\W\d_]{2,}")

# An 11-character YouTube video ID.
_VIDEO_ID_RE = re.compile(r"^[A-Za-z0-9_-]{11}$")

# Query-string parameters that are tracking/analytics noise rather than content
# selectors. Stripped when building a URL's dedup key so the same article linked
# with different campaign tags collapses to one. Anything starting with "utm_"
# is also dropped (handled in code).
_TRACKING_PARAMS = frozenset(
    {
        "fbclid",
        "gclid",
        "dclid",
        "gbraid",
        "wbraid",
        "msclkid",
        "mc_cid",
        "mc_eid",
        "igshid",
        "ref",
        "ref_src",
        "ref_url",
        "spm",
        "yclid",
        "_hsenc",
        "_hsmi",
    }
)


def _extract_json(raw: str) -> Any:
    """Best-effort parse of a JSON object/array from an LLM reply.

    Models often wrap JSON in ``` fences or surround it with a sentence of
    prose, which breaks a bare ``json.loads``. This strips fences, tries a
    direct parse, then falls back to the outermost ``{...}`` / ``[...]`` span.
    Returns the parsed value, or ``None`` when nothing parseable is found.
    """
    if not raw:
        return None
    s = raw.strip()
    s = re.sub(r"^```(?:json)?\s*", "", s)
    s = re.sub(r"\s*```$", "", s).strip()
    try:
        return json.loads(s)
    except Exception:
        pass
    for open_ch, close_ch in (("{", "}"), ("[", "]")):
        start = s.find(open_ch)
        end = s.rfind(close_ch)
        if start != -1 and end > start:
            try:
                return json.loads(s[start : end + 1])
            except Exception:
                continue
    return None


def _dedup_key(url: str) -> str:
    """Normalize `url` into a key for de-duplication (NOT for fetching).

    Collapses scheme (http/https treated alike), a leading ``www.``, the
    default port, a trailing slash, and tracking query params so the same
    article reached via cosmetically different URLs is only fetched/stored once.
    Falls back to the stripped original if parsing fails.
    """
    raw = (url or "").strip()
    try:
        p = urlparse(raw)
    except Exception:
        return raw
    if not p.scheme or not p.hostname:
        return raw.lower()
    host = p.hostname.lower()
    if host.startswith("www."):
        host = host[4:]
    netloc = host
    if p.port and p.port not in (80, 443):
        netloc = f"{host}:{p.port}"
    path = p.path or "/"
    if len(path) > 1 and path.endswith("/"):
        path = path[:-1]
    kept = [
        (k, v)
        for k, v in parse_qsl(p.query, keep_blank_values=True)
        if k.lower() not in _TRACKING_PARAMS and not k.lower().startswith("utm_")
    ]
    query = urlencode(sorted(kept))
    # Scheme normalized to https so http/https variants share a key.
    return urlunparse(("https", netloc, path, "", query, ""))


# ---------------------------------------------------------------------------
# Relevance extraction (improvement #1) and multi-hop link selection (#8)
#
# Instead of keeping the first N words of every page, we score the page's
# segments against the query that surfaced it and keep the most relevant ones up
# to the word budget. The same lexical scorer ranks candidate in-page links for
# multi-hop following. Pure, dependency-free, and unit-testable; an optional
# embedding ranker (when EMBEDDING_MODEL is set) layers on top in the Pipe class.
# ---------------------------------------------------------------------------

# How much of a fetched page we scan for relevant passages. Bounds memory/CPU on
# huge pages while still giving the extractor far more to choose from than the
# final SNIPPET_MAX_WORDS budget.
_RELEVANCE_SCAN_MAX_WORDS = 4000

# Roughly one "segment" (the unit we score) is at most this many words; longer
# paragraphs are split into sentence-ish chunks.
_SEGMENT_MAX_WORDS = 60

# Common words carry no topical signal, so they're dropped before scoring.
_STOPWORDS = frozenset(
    """a an and are as at be by for from has have how in into is it its of on or
    that the their them they this to was were what when where which who why will
    with about over under more most other some such than then these those you your
    we our us i""".split()
)

_WORD_TOKEN_RE = re.compile(r"[a-z0-9]+")

# Hrefs that are never worth following as a research source.
_LINK_EXCLUDE_RE = re.compile(
    r"(?:^mailto:|^tel:|^javascript:|^#)"
    r"|(?:facebook|twitter|x|instagram|linkedin|pinterest|reddit|youtube|tiktok|"
    r"t)\.(?:com|co)/(?:share|intent|sharer)"
    r"|/(?:login|signin|sign-in|signup|sign-up|register|subscribe|cart|account|"
    r"privacy|terms|cookie|advertise|contact)\b",
    re.IGNORECASE,
)


def _tokenize(text: str) -> List[str]:
    """Lowercase word tokens with stopwords removed."""
    return [
        t for t in _WORD_TOKEN_RE.findall((text or "").lower()) if t not in _STOPWORDS
    ]


def _segments(text: str) -> List[str]:
    """Split text into scoring units (~sentences / short paragraphs).

    Robust to ``_html_to_text`` collapsing a page into one long line (it splits
    such a blob into sentences) and to documents/transcripts that keep newlines
    (split per line first). Blank fragments are dropped.
    """
    out: List[str] = []
    for para in re.split(r"\n+", text or ""):
        para = para.strip()
        if not para:
            continue
        if len(para.split()) <= _SEGMENT_MAX_WORDS:
            out.append(para)
            continue
        buf = ""
        for sent in re.split(r"(?<=[.!?])\s+", para):
            if len((buf + " " + sent).split()) > _SEGMENT_MAX_WORDS:
                if buf:
                    out.append(buf.strip())
                buf = sent
            else:
                buf = (buf + " " + sent).strip()
        if buf:
            out.append(buf.strip())
    return out


def _score_segment(seg_tokens: set, query_tokens: set) -> float:
    """Fraction of distinct query tokens present in a segment (0..1)."""
    if not query_tokens:
        return 0.0
    return len(seg_tokens & query_tokens) / len(query_tokens)


def _cosine(a: List[float], b: List[float]) -> float:
    """Cosine similarity of two equal-length vectors (0.0 on any degeneracy).

    Pure-Python so the embedding reranker needs no numpy; the vectors are short
    (one query + a page's segments, each a few hundred dims) so this is cheap.
    """
    if not a or not b or len(a) != len(b):
        return 0.0
    dot = 0.0
    na = 0.0
    nb = 0.0
    for x, y in zip(a, b):
        dot += x * y
        na += x * x
        nb += y * y
    if na <= 0.0 or nb <= 0.0:
        return 0.0
    return dot / (math.sqrt(na) * math.sqrt(nb))


# ---------------------------------------------------------------------------
# Publish-date handling
#
# SearXNG reports a source's publish date inconsistently (ISO 8601, RFC 822, a
# bare year, …) and only for some engines. We parse it best-effort to: surface
# it in the report's reference list, break relevance ties toward fresher sources
# when the plan flagged the topic as time-sensitive, and flag clearly stale
# sources. Anything unparseable is simply treated as "no date" — never an error.
# ---------------------------------------------------------------------------

# How old (in days) a source may be before it's flagged "may be outdated",
# keyed by the plan's recency level. Tied to the topic's own time-sensitivity:
# an evergreen topic (no recency flag) never flags a source as stale.
_STALE_AFTER_DAYS = {"day": 7, "week": 30, "month": 180, "year": 730}

# strptime formats tried after ISO 8601, covering the common engine outputs.
_DATE_FORMATS = (
    "%a, %d %b %Y %H:%M:%S %z",  # RFC 822 (e.g. Mon, 15 Jan 2024 10:00:00 +0000)
    "%a, %d %b %Y %H:%M:%S %Z",
    "%Y-%m-%d %H:%M:%S",
    "%Y/%m/%d",
    "%d %B %Y",
    "%B %d, %Y",
    "%d %b %Y",
)


def _to_naive_utc(dt: datetime) -> datetime:
    """Normalize a datetime to naive UTC so all dates compare on one timeline."""
    if dt.tzinfo is not None:
        dt = dt.astimezone(timezone.utc).replace(tzinfo=None)
    return dt


def _parse_date(raw: Optional[str]) -> Optional[datetime]:
    """Best-effort parse of a SearXNG publish date into a naive-UTC datetime.

    Tries ISO 8601 (tolerating a trailing ``Z``), then a handful of common
    formats, then a leading ``YYYY-MM-DD`` or bare ``YYYY``. Returns ``None`` for
    anything unparseable/blank — the caller treats that as "no date".
    """
    if not raw or not isinstance(raw, str):
        return None
    s = raw.strip()
    if not s:
        return None
    try:
        return _to_naive_utc(datetime.fromisoformat(s.replace("Z", "+00:00")))
    except Exception:
        pass
    for fmt in _DATE_FORMATS:
        try:
            return _to_naive_utc(datetime.strptime(s, fmt))
        except Exception:
            continue
    m = re.match(r"(\d{4})-(\d{2})-(\d{2})", s)
    if m:
        try:
            return datetime(int(m.group(1)), int(m.group(2)), int(m.group(3)))
        except ValueError:
            return None
    m = re.match(r"(\d{4})\b", s)
    if m:
        try:
            return datetime(int(m.group(1)), 1, 1)
        except ValueError:
            return None
    return None


def _is_stale(dt: Optional[datetime], recency: str, now: datetime) -> bool:
    """True if `dt` is older than the staleness window for `recency`.

    Only flags when the plan set a recency level (time-sensitive topic); for an
    evergreen topic (recency "") or an unparseable date, returns False.
    """
    days = _STALE_AFTER_DAYS.get(recency)
    if not days or dt is None:
        return False
    return (now - dt).days > days


def _extract_relevant(
    text: str, query: str, max_words: int, *, scan_max: int = _RELEVANCE_SCAN_MAX_WORDS
) -> Tuple[str, float]:
    """Return the passages of `text` most relevant to `query`, plus a 0..1 score.

    Segments are scored by query-token overlap; the highest-scoring ones are
    reassembled in document order up to `max_words`. The score is the distinct
    fraction of query tokens covered by the kept passages — comparable across
    sources, so it drives retention/compression and report ordering. When nothing
    lexically matches (e.g. a conceptual query), falls back to the head of the
    text with score 0.0 rather than dropping a possibly-relevant page.
    """
    words = (text or "").split()
    if not words:
        return "", 0.0
    if scan_max > 0 and len(words) > scan_max:
        text = " ".join(words[:scan_max])

    query_tokens = set(_tokenize(query))
    segs = _segments(text)
    if not segs or not query_tokens:
        head = " ".join((text or "").split()[:max_words])
        return head, 0.0

    scored = [(_score_segment(set(_tokenize(s)), query_tokens), i, s) for i, s in enumerate(segs)]
    matching = [t for t in scored if t[0] > 0]
    if not matching:
        head = " ".join(text.split()[:max_words])
        return head, 0.0

    # Take the best segments by score, then restore document order for readability.
    matching.sort(key=lambda t: t[0], reverse=True)
    chosen: List[Tuple[int, str]] = []
    budget = 0
    covered: set = set()
    for score, idx, seg in matching:
        wc = len(seg.split())
        if max_words > 0 and budget + wc > max_words and chosen:
            break
        chosen.append((idx, seg))
        budget += wc
        covered |= set(_tokenize(seg)) & query_tokens
        if max_words > 0 and budget >= max_words:
            break

    chosen.sort(key=lambda t: t[0])
    passages = "\n".join(seg for _, seg in chosen)
    score = len(covered) / len(query_tokens)
    return passages, score


def _select_links(
    links: List[Tuple[str, str]], query: str, seen_keys: set, limit: int
) -> List[str]:
    """Pick up to `limit` in-page links worth following for multi-hop research.

    Ranks candidate links by how well their anchor text matches the query,
    skipping junk/nav/social/auth hrefs and anything already seen (by dedup key).
    Returns absolute hrefs, best first; only links with some query overlap.
    """
    if limit <= 0 or not links:
        return []
    query_tokens = set(_tokenize(query))
    if not query_tokens:
        return []
    ranked: List[Tuple[float, str]] = []
    seen_here: set = set()
    for anchor, href in links:
        if not href or _LINK_EXCLUDE_RE.search(href):
            continue
        if not href.lower().startswith(("http://", "https://")):
            continue
        key = _dedup_key(href)
        if key in seen_keys or key in seen_here:
            continue
        score = _score_segment(set(_tokenize(anchor)), query_tokens)
        if score <= 0:
            continue
        seen_here.add(key)
        ranked.append((score, href))
    ranked.sort(key=lambda t: t[0], reverse=True)
    return [href for _, href in ranked[:limit]]


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
                "Optional embedding model ID for semantic relevance ranking of "
                "page passages. When set, the query and each candidate segment "
                "are embedded via Open WebUI's configured embeddings endpoint and "
                "cosine-ranked, catching synonym/paraphrase matches the lexical "
                "(keyword-overlap) scorer misses. For the 'openai'/'ollama'/"
                "'azure_openai' RAG engines this exact model ID is used; for the "
                "local engine Open WebUI's configured embedding model is used. "
                "Falls back to the lexical scorer when the endpoint isn't "
                "configured or a request fails. Blank = lexical-only (the "
                "default, no setup needed)."
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
        SEARCH_TIME_RANGE: str = Field(
            default="",
            description=(
                "Recency filter applied to every search: '', 'day', 'week', "
                "'month', or 'year'. Blank = no restriction. The research plan's "
                "own recency hint overrides this per run."
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

        # --- Apache Tika (PDF extraction) ---
        TIKA_URL: str = Field(
            default="http://tika:9998",
            description="Base URL of your Apache Tika server used for PDF text extraction.",
        )

        # --- Snippet / context control ---
        SNIPPET_MAX_WORDS: int = Field(
            default=300,
            description=(
                "Max words kept per source after relevance extraction — the "
                "passages most relevant to the query are selected up to this "
                "budget (not the first N words of the page)."
            ),
        )
        MAX_TOTAL_CONTEXT_WORDS: int = Field(
            default=30000,
            description=(
                "Soft cap on accumulated research context (words). When "
                "exceeded, the lowest-relevance snippets are compressed into a "
                "summary while the highest-scored ones are kept verbatim."
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
        MIN_SOURCES_PER_SECTION: int = Field(
            default=2,
            description=(
                "Coverage target per planned section. The loop prioritises "
                "under-covered sections and keeps researching until every "
                "section has at least this many good sources (or cycles run out)."
            ),
        )
        MAX_LINK_HOPS_PER_CYCLE: int = Field(
            default=3,
            description=(
                "Multi-hop budget: how many promising links found INSIDE fetched "
                "pages to follow each cycle (depth 1 only) to reach primary "
                "sources beyond the search results. 0 disables link-following."
            ),
        )
        FOLLOWUP_QUERIES_PER_CYCLE: int = Field(
            default=2,
            description=(
                "Proactive thread-pulling: after each cycle a cheap LLM pass over "
                "the passages just gathered proposes up to this many follow-up "
                "searches for specific studies, datasets, people, organisations, "
                "or counter-claims that warrant their own query. They join the "
                "queue alongside planned and gap-filling queries, so the loop "
                "follows threads it discovers instead of only executing the fixed "
                "plan. 0 disables (plan + reactive gap-filling only)."
            ),
        )
        CYCLE_DELAY_SECONDS: float = Field(
            default=0.0,
            description=(
                "Seconds to pause between research cycles. Raise (e.g. 2-5) if "
                "SearXNG or source sites rate-limit you. 0 = no delay."
            ),
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
                "Max tokens the LLM may generate for the final report (per "
                "section in sectioned mode, or for the whole report in single "
                "mode). Increase if output is being truncated."
            ),
        )
        REPORT_MODE: str = Field(
            default="sectioned",
            description=(
                "'sectioned' (default) drafts each report section in its own LLM "
                "call against only that section's sources, then a synthesis pass "
                "writes the abstract/conclusion — more thorough, more LLM calls. "
                "'single' writes the whole report in one call (legacy, cheaper)."
            ),
        )
        REPORT_SOURCE_MAX_CHARS: int = Field(
            default=2000,
            description=(
                "Max characters of each source's text included in the report "
                "prompt. 0 = no per-source cap."
            ),
        )
        REPORT_CONTEXT_MAX_CHARS: int = Field(
            default=60000,
            description=(
                "Total character budget for all source text in the report "
                "prompt. Sources past the budget are dropped from BOTH the data "
                "and the citation list (so the model never cites a source it "
                "couldn't see). Raise for fuller reports on long-context models; "
                "0 = unbounded."
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
        VERIFY_SSL: bool = Field(
            default=True,
            description=(
                "Verify TLS certificates when fetching pages. Leave on; disable "
                "only if you must fetch sources with broken/self-signed certs."
            ),
        )

        # --- Behaviour ---
        SKIP_PLAN_CONFIRMATION: bool = Field(
            default=False,
            description="Skip the user-confirmation step for the plan.",
        )
        WAYBACK_FALLBACK: bool = Field(
            default=True,
            description=(
                "When True, a page that stays blocked (bot wall / 429) or "
                "renders empty after the direct fetch and FlareSolverr is "
                "retried from the Internet Archive's Wayback Machine. "
                "Recovered text is clearly flagged as a possibly-stale archived "
                "snapshot. Needs no extra service (public archive.org API)."
            ),
        )

    # -----------------------------------------------------------------------
    # Init
    # -----------------------------------------------------------------------
    def __init__(self):
        self.type = "pipe"
        self.id = "deep_research"
        self.name = "Deep Research"
        self.valves = self.Valves()
        # One-time log guard so a missing/failing embeddings endpoint reports the
        # lexical fallback once per process instead of on every page.
        self._embed_fallback_logged = False

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
            response = await generate_chat_completion(request, payload, user=user)
            return await self._read_completion(response)
        except Exception as e:
            log.error(f"LLM call failed: {e}")
            raise

    @staticmethod
    async def _read_completion(response: Any) -> str:
        """Extract the assistant text from an Open WebUI chat-completion response.

        Handles all three shapes ``generate_chat_completion`` can return — a
        plain dict, a streamed ``body_iterator`` (SSE ``data:`` deltas or raw
        JSON lines), or a stringifiable object — and returns the joined content,
        stripped. Shared by ``_llm_call`` and the background-task branch of
        ``pipe`` so the streaming parse lives in one place.
        """
        if isinstance(response, dict):
            return (
                response.get("choices", [{}])[0]
                .get("message", {})
                .get("content", "")
                or ""
            ).strip()
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
                        except json.JSONDecodeError:
                            continue
                        full += (
                            data.get("choices", [{}])[0]
                            .get("delta", {})
                            .get("content", "")
                            or ""
                        )
                    else:
                        try:
                            data = json.loads(line)
                        except json.JSONDecodeError:
                            continue
                        c = (
                            data.get("choices", [{}])[0]
                            .get("message", {})
                            .get("content", "")
                        )
                        if c:
                            full += c
            return full.strip()
        return str(response).strip()

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

    async def _emit_replace(self, emitter: Optional[Callable], content: str) -> None:
        """Replace the full assistant message (persisted in chat DB)."""
        if emitter:
            await emitter({"type": "replace", "data": {"content": content}})

    async def _emit_citation(self, emitter, url: str, title: str, snippet: str):
        """Emit a native Open WebUI citation event so the source renders as a
        clickable chip beneath the message (in addition to the inline ``[n]``
        list in the report text)."""
        if not emitter or not url:
            return
        await emitter(
            {
                "type": "citation",
                "data": {
                    "document": [snippet or ""],
                    "metadata": [{"source": url}],
                    "source": {"name": title or url, "url": url},
                },
            }
        )

    # -----------------------------------------------------------------------
    # Web fetching
    # -----------------------------------------------------------------------
    _BROWSER_HEADERS = {
        "User-Agent": (
            "Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
            "AppleWebKit/537.36 (KHTML, like Gecko) "
            "Chrome/125.0.0.0 Safari/537.36"
        ),
        "Accept": (
            "text/html,application/xhtml+xml,"
            "application/xml;q=0.9,*/*;q=0.8"
        ),
    }

    @staticmethod
    def _cap_words(text: str, max_words: int) -> str:
        """Hard-cap `text` to `max_words` words (a memory/CPU bound, not the
        relevance budget — that's applied later by `_extract_relevant`)."""
        words = (text or "").split()
        if max_words > 0 and len(words) > max_words:
            return " ".join(words[:max_words])
        return text

    _A_TAG_RE = re.compile(
        r'<a\b[^>]*?href=["\']([^"\']+)["\'][^>]*>(.*?)</a>',
        re.IGNORECASE | re.DOTALL,
    )

    @classmethod
    def _extract_links(cls, html: str, base_url: str) -> List[Tuple[str, str]]:
        """Pull ``(anchor_text, absolute_href)`` pairs from page HTML.

        Best-effort regex harvest (the pipe flattens HTML to text elsewhere, so
        links would otherwise be lost). Relative hrefs are resolved against
        `base_url`; fragments/mailto/js are skipped. Feeds multi-hop following.
        """
        out: List[Tuple[str, str]] = []
        for href, inner in cls._A_TAG_RE.findall(html or ""):
            href = href.strip()
            if not href or href.startswith(("#", "javascript:", "mailto:", "tel:", "data:")):
                continue
            anchor = re.sub(r"<[^>]+>", " ", inner)
            anchor = re.sub(r"\s+", " ", anchor).strip()
            try:
                abs_href = urljoin(base_url, href)
            except Exception:
                continue
            if abs_href.lower().startswith(("http://", "https://")):
                out.append((anchor, abs_href))
        return out

    @staticmethod
    def _page(
        url: str,
        text: str,
        *,
        links: Optional[List[Tuple[str, str]]] = None,
        kind: str = "html",
    ) -> Dict[str, Any]:
        """Build the raw-fetch result the research loop consumes. ``text`` is the
        fuller readable content (relevance extraction happens later); ``links``
        are in-page links for multi-hop; ``kind`` records how it was fetched."""
        return {"url": url, "text": text or "", "links": links or [], "kind": kind}

    @staticmethod
    def _is_tika_document(ctype: str, url: str) -> bool:
        """True if the response looks like a binary document Tika should extract."""
        ctype = (ctype or "").lower()
        if any(ctype.startswith(c) for c in TIKA_DOCUMENT_CTYPES):
            return True
        # Fall back on the URL path when the server sends a generic content-type
        # (e.g. application/octet-stream) but the extension is telling.
        try:
            path = (urlparse(url).path or "").lower()
        except Exception:
            return False
        return path.endswith(TIKA_DOCUMENT_EXTENSIONS)

    @staticmethod
    def _is_contentless(body: str) -> bool:
        """True if `body` has no readable text (not one word of 2+ letters)."""
        return not _WORD_RE.search(body or "")

    @staticmethod
    def _is_blocked_response(status: int, text: str, headers: Dict) -> bool:
        """Best-effort detection that a response is a bot wall / CAPTCHA / throttle.

        Covers Cloudflare plus PerimeterX/HUMAN, DataDome, and Akamai Bot Manager
        — each of which serves a challenge under one of ``BLOCK_STATUS_CODES`` (or
        a bare 200) carrying its own marker rather than the page's real content —
        and any HTTP 429, which is definitionally a throttle and never real data.
        """
        # 429 is always a rate-limit/throttle and never carries real content; a
        # real browser (FlareSolverr) often clears it, so treat it as a block.
        if status == 429:
            return True
        hdr_lower = {k.lower(): str(v).lower() for k, v in (headers or {}).items()}
        server = hdr_lower.get("server", "")
        set_cookie = hdr_lower.get("set-cookie", "")
        if "cloudflare" in server and status in BLOCK_STATUS_CODES:
            return True
        if status in BLOCK_STATUS_CODES:
            if (
                "x-datadome" in hdr_lower
                or "datadome" in set_cookie
                or "_px" in set_cookie
            ):
                return True
            t = (text or "")[:8000].lower()
            if any(m in t for m in ALL_BLOCK_MARKERS):
                return True
        if status == 200 and text:
            t = text[:4000].lower()
            if sum(1 for m in ALL_BLOCK_MARKERS if m in t) >= 2:
                return True
        return False

    async def _fetch_page(
        self, session: aiohttp.ClientSession, url: str
    ) -> Dict[str, Any]:
        """Fetch one URL through a resilient fallback ladder.

        Returns a page dict ``{url, text, links, kind}`` (see `_page`); ``text``
        is the fuller readable content (relevance extraction is applied later by
        the loop) and is empty when nothing usable could be recovered. The
        ladder: Reddit JSON → YouTube transcript → direct GET (document→Tika),
        and on a bot wall / 429 / empty JS shell, FlareSolverr and then the
        Wayback Machine. A challenge page we can't get past comes back with empty
        text rather than as if it were content. ``links`` carries the page's
        in-page links (HTML fetches only) for multi-hop following.
        """
        cap = _RELEVANCE_SCAN_MAX_WORDS

        # Reddit serves anti-bot HTML; route through its public .json endpoint.
        if self._is_reddit_url(url):
            text = await self._fetch_reddit(session, url)
            return self._page(url, self._cap_words(text, cap), kind="reddit")

        # A YouTube video's real content is its transcript, not the watch page.
        if _YT_AVAILABLE and self._is_youtube_video_url(url):
            transcript = await self._fetch_youtube_transcript(url)
            if transcript:
                return self._page(url, self._cap_words(transcript, cap), kind="youtube")
            # No captions / blocked IP — fall through to a normal HTML fetch.

        status = 0
        headers: Dict[str, str] = {}
        raw_html = ""
        is_doc = False
        body_bytes = b""
        try:
            async with session.get(
                url,
                timeout=aiohttp.ClientTimeout(total=self.valves.PAGE_FETCH_TIMEOUT),
                headers=self._BROWSER_HEADERS,
                allow_redirects=True,
                ssl=self.valves.VERIFY_SSL,
            ) as resp:
                status = resp.status
                headers = dict(resp.headers)
                ct = resp.headers.get("Content-Type", "")
                # Route binary documents (PDF/Office/OpenDocument/RTF/EPUB) to
                # Tika regardless of status code; everything else is read as text
                # so block detection can inspect the body.
                if self._is_tika_document(ct, url):
                    body_bytes = await resp.read()
                    is_doc = True
                else:
                    raw_html = await resp.text(errors="replace")
        except Exception as e:
            log.debug(f"Direct fetch failed for {url}: {e}")

        if is_doc:
            extracted = await self._extract_document(body_bytes)
            return self._page(url, self._cap_words(extracted, cap), kind="document")

        blocked = self._is_blocked_response(status, raw_html, headers) if status else False
        rendered_text = self._html_to_text(raw_html) if raw_html else ""
        # A bot wall, a throttle, or a 200 that rendered to no readable text (a
        # client-side-rendered SPA whose body loads via XHR) all need a real
        # browser or an archive to recover.
        need_fallback = blocked or self._is_contentless(rendered_text)

        # Tier 1: FlareSolverr renders the page's JS in a real browser.
        if need_fallback and self.valves.FLARESOLVERR_URL:
            fs_html, fs_status, fs_headers = await self._flaresolverr(session, url)
            if fs_html:
                fs_blocked = self._is_blocked_response(
                    fs_status or 200, fs_html, fs_headers
                )
                fs_text = self._html_to_text(fs_html)
                if fs_text and not fs_blocked and not self._is_contentless(fs_text):
                    return self._page(
                        url,
                        self._cap_words(fs_text, cap),
                        links=self._extract_links(fs_html, url),
                        kind="flaresolverr",
                    )
                # FlareSolverr returning a page isn't a bypass — an interactive
                # wall (PerimeterX "Press & Hold", a CAPTCHA) renders as an
                # ordinary page it can't solve. Keep the block flag and try the
                # archive next.
                blocked = blocked or fs_blocked

        # Tier 2: a Wayback snapshot may hold text the live page now hides/blocks.
        if need_fallback and self.valves.WAYBACK_FALLBACK:
            archived = await self._fetch_from_wayback(session, url)
            if archived:
                wb_text, wb_date = archived
                note = (
                    f"[Archived snapshot from {wb_date} via the Wayback Machine; "
                    "the live page was unavailable, so this may be out of date.]\n"
                )
                return self._page(
                    url, note + self._cap_words(wb_text, cap), kind="archive"
                )

        # Don't surface an unbypassed challenge page as if it were content.
        if blocked:
            return self._page(url, "", kind="blocked")
        return self._page(
            url,
            self._cap_words(rendered_text, cap),
            links=self._extract_links(raw_html, url),
            kind="html",
        )

    # -----------------------------------------------------------------------
    # Reddit JSON endpoint handling
    # -----------------------------------------------------------------------
    @staticmethod
    def _is_reddit_url(url: str) -> bool:
        """Detect reddit.com URLs (including subdomains like old./www./np.)."""
        try:
            lower = url.lower()
            # Match http(s)://...reddit.com/ — catches www, old, np, i, etc.
            return bool(
                re.match(r"^https?://([a-z0-9-]+\.)?reddit\.com(/|$)", lower)
            )
        except Exception:
            return False

    @staticmethod
    def _reddit_json_url(url: str) -> str:
        """Convert a Reddit URL into its .json equivalent.

        - Strips query string and fragment (Reddit's JSON endpoint ignores
          most query params and some actively break it).
        - Removes a trailing slash.
        - Appends .json if not already present.
        """
        # Drop fragment
        u = url.split("#", 1)[0]
        # Drop query string
        u = u.split("?", 1)[0]
        # Drop trailing slash
        if u.endswith("/"):
            u = u[:-1]
        if not u.endswith(".json"):
            u = u + ".json"
        return u

    async def _fetch_reddit(
        self, session: aiohttp.ClientSession, url: str
    ) -> str:
        """Fetch a Reddit post/comments/listing via the public .json endpoint
        and render it into plain text suitable for the research pipeline."""
        json_url = self._reddit_json_url(url)
        try:
            async with session.get(
                json_url,
                timeout=aiohttp.ClientTimeout(total=self.valves.PAGE_FETCH_TIMEOUT),
                headers={
                    # A descriptive, non-browser UA works best with Reddit's
                    # JSON endpoint — browser UAs are more likely to get
                    # rate-limited / blocked.
                    "User-Agent": (
                        "deep-research-pipe/1.2 "
                        "(+https://github.com/open-webui)"
                    ),
                    "Accept": "application/json",
                },
                allow_redirects=True,
                ssl=self.valves.VERIFY_SSL,
            ) as resp:
                if resp.status != 200:
                    log.debug(
                        f"Reddit JSON fetch returned {resp.status} for {json_url}"
                    )
                    return ""
                try:
                    data = await resp.json(content_type=None)
                except Exception as e:
                    log.debug(f"Reddit JSON parse failed for {json_url}: {e}")
                    return ""
        except Exception as e:
            log.debug(f"Reddit JSON fetch failed for {json_url}: {e}")
            return ""

        return self._render_reddit_json(data)

    @staticmethod
    def _render_reddit_json(data: Any) -> str:
        """Render Reddit's JSON response into readable plain text.

        Handles both the single-post format (list of two Listings: post +
        comments) and the subreddit/listing format (one Listing of posts).
        """
        def _strip_html(s: str) -> str:
            if not s:
                return ""
            s = re.sub(r"<[^>]+>", " ", s)
            for ent, ch in [
                ("&amp;", "&"),
                ("&lt;", "<"),
                ("&gt;", ">"),
                ("&quot;", '"'),
                ("&#39;", "'"),
                ("&nbsp;", " "),
            ]:
                s = s.replace(ent, ch)
            return re.sub(r"\s+", " ", s).strip()

        def _walk_comments(children: List[Dict], depth: int = 0) -> List[str]:
            out: List[str] = []
            for child in children:
                kind = child.get("kind")
                cdata = child.get("data", {}) or {}
                if kind != "t1":
                    # Skip "more" stubs and anything non-comment
                    continue
                body = (cdata.get("body") or "").strip()
                if not body or body in ("[deleted]", "[removed]"):
                    continue
                author = cdata.get("author") or "unknown"
                score = cdata.get("score")
                score_str = f" ({score} pts)" if score is not None else ""
                indent = "  " * min(depth, 4)
                out.append(f"{indent}- {author}{score_str}: {body}")
                # Recurse into replies
                replies = cdata.get("replies")
                if isinstance(replies, dict):
                    reply_children = (
                        replies.get("data", {}).get("children", []) or []
                    )
                    out.extend(_walk_comments(reply_children, depth + 1))
            return out

        parts: List[str] = []

        # Single post + comments: [post_listing, comments_listing]
        if isinstance(data, list) and len(data) >= 1:
            post_listing = data[0]
            post_children = (
                post_listing.get("data", {}).get("children", []) or []
            )
            if post_children:
                post = post_children[0].get("data", {}) or {}
                title = (post.get("title") or "").strip()
                author = post.get("author") or "unknown"
                subreddit = post.get("subreddit") or ""
                score = post.get("score")
                selftext = _strip_html(post.get("selftext") or "")
                link_url = post.get("url") or ""

                if title:
                    parts.append(f"Title: {title}")
                if subreddit:
                    parts.append(f"Subreddit: r/{subreddit}")
                parts.append(f"Author: u/{author}")
                if score is not None:
                    parts.append(f"Score: {score}")
                if selftext:
                    parts.append(f"\nPost body:\n{selftext}")
                elif link_url and link_url not in (
                    post.get("permalink", ""),
                    "",
                ):
                    parts.append(f"Linked URL: {link_url}")

            # Comments
            if len(data) >= 2:
                comment_listing = data[1]
                comment_children = (
                    comment_listing.get("data", {}).get("children", []) or []
                )
                rendered = _walk_comments(comment_children)
                if rendered:
                    parts.append("\nTop comments:")
                    parts.extend(rendered)

        # Listing of posts (e.g. /r/subreddit.json, /r/subreddit/top.json)
        elif isinstance(data, dict) and data.get("kind") == "Listing":
            children = data.get("data", {}).get("children", []) or []
            for child in children:
                cdata = child.get("data", {}) or {}
                title = (cdata.get("title") or "").strip()
                if not title:
                    continue
                author = cdata.get("author") or "unknown"
                subreddit = cdata.get("subreddit") or ""
                score = cdata.get("score")
                selftext = _strip_html(cdata.get("selftext") or "")
                header = f"- [{score} pts] r/{subreddit} — {title} (by u/{author})"
                parts.append(header)
                if selftext:
                    # Keep listing entries compact
                    snippet = selftext[:400]
                    if len(selftext) > 400:
                        snippet += "…"
                    parts.append(f"  {snippet}")

        return "\n".join(parts).strip()

    async def _flaresolverr(
        self, session: aiohttp.ClientSession, url: str
    ) -> Tuple[str, int, Dict]:
        """Render `url` through FlareSolverr (a real browser that runs the page's
        JS and clears Cloudflare-style walls).

        Returns ``(html, status, headers)`` from the solved page so the caller
        can re-run block detection on what FlareSolverr actually got — a page it
        returns may still be an interactive challenge it couldn't solve.
        ``("", 0, {})`` on any failure.
        """
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
                    sol = data.get("solution", {}) or {}
                    html = sol.get("response", "") or ""
                    fs_status = int(sol.get("status") or 0)
                    fs_headers = sol.get("headers") or {}
                    return html, fs_status, fs_headers
        except Exception as e:
            log.debug(f"FlareSolverr failed for {url}: {e}")
        return "", 0, {}

    @staticmethod
    def _html_to_text(html: str) -> str:
        text = re.sub(
            r"<(script|style|noscript)[^>]*>.*?</\1>",
            "",
            html,
            flags=re.DOTALL | re.IGNORECASE,
        )
        text = re.sub(r"<[^>]+>", " ", text)
        for ent, ch in [
            ("&amp;", "&"),
            ("&lt;", "<"),
            ("&gt;", ">"),
            ("&quot;", '"'),
            ("&#39;", "'"),
            ("&nbsp;", " "),
        ]:
            text = text.replace(ent, ch)
        return re.sub(r"\s+", " ", text).strip()

    async def _extract_document(self, data: bytes) -> str:
        """Extract plain text from a document byte stream via Apache Tika.

        No Content-Type is sent: Tika auto-detects the format from the bytes, so
        this one path handles PDF, Office (doc/docx/xls/xlsx/ppt/pptx),
        OpenDocument, RTF and EPUB. ``X-Tika-PDFOcrStrategy: no_ocr`` keeps it to
        embedded text (fast; avoids OCR of image-heavy PDFs blowing the timeout).
        """
        if not data:
            return "[Document returned no content]"
        try:
            tika_url = f"{self.valves.TIKA_URL.rstrip('/')}/tika"
            async with aiohttp.ClientSession() as session:
                async with session.put(
                    tika_url,
                    data=data,
                    headers={
                        "Accept": "text/plain",
                        "X-Tika-PDFOcrStrategy": "no_ocr",
                    },
                    timeout=aiohttp.ClientTimeout(total=60),
                ) as resp:
                    resp.raise_for_status()
                    text = (await resp.text()).strip()
                    return text if text else "[Document contained no extractable text]"
        except Exception as e:
            return f"[Document extraction failed: {e}]"

    # -----------------------------------------------------------------------
    # Wayback Machine (archive.org) fallback
    # -----------------------------------------------------------------------
    async def _fetch_from_wayback(
        self, session: aiohttp.ClientSession, url: str
    ) -> Optional[Tuple[str, str]]:
        """Recover readable text for `url` from the Internet Archive, or None.

        A last resort when the live page (even after a FlareSolverr render) is
        blocked or yields no readable text: a prior snapshot may have captured
        text the live SPA now hides behind JS, or the page may since have
        changed/disappeared. Returns ``(text, YYYY-MM-DD)`` for the closest
        available 200 snapshot, or ``None``. Best-effort — any error returns
        ``None`` since this only runs after the live attempts already failed.

        The snapshot is requested in ``id_`` (identity) mode, which returns the
        original archived HTML without the Wayback toolbar/URL-rewriting, so the
        existing text extraction handles it like a live page.
        """
        now = time.strftime("%Y%m%d%H%M%S", time.gmtime())
        try:
            async with session.get(
                WAYBACK_AVAILABILITY_API,
                params={"url": url, "timestamp": now},
                timeout=aiohttp.ClientTimeout(total=self.valves.PAGE_FETCH_TIMEOUT),
            ) as resp:
                if resp.status != 200:
                    return None
                data = await resp.json(content_type=None)
        except Exception as e:
            log.debug(f"Wayback availability lookup failed for {url}: {e}")
            return None

        snap = ((data or {}).get("archived_snapshots") or {}).get("closest") or {}
        ts = snap.get("timestamp") or ""
        # Accept only an available snapshot that was a 200 capture; some omit
        # `status`, which we tolerate.
        if not snap.get("available") or not ts:
            return None
        if str(snap.get("status") or "200") != "200":
            return None

        snapshot_url = f"https://web.archive.org/web/{ts}id_/{url}"
        try:
            async with session.get(
                snapshot_url,
                timeout=aiohttp.ClientTimeout(total=self.valves.PAGE_FETCH_TIMEOUT),
                headers=self._BROWSER_HEADERS,
                allow_redirects=True,
                ssl=self.valves.VERIFY_SSL,
            ) as resp:
                if resp.status != 200:
                    return None
                ct = resp.headers.get("Content-Type", "")
                if self._is_tika_document(ct, url):
                    text = await self._extract_document(await resp.read())
                else:
                    text = self._html_to_text(await resp.text(errors="replace"))
        except Exception as e:
            log.debug(f"Wayback snapshot fetch failed for {snapshot_url}: {e}")
            return None

        if self._is_contentless(text):
            return None
        date = f"{ts[0:4]}-{ts[4:6]}-{ts[6:8]}" if len(ts) >= 8 and ts[:8].isdigit() else ts
        return text, date

    # -----------------------------------------------------------------------
    # YouTube transcript handling (optional dependency)
    # -----------------------------------------------------------------------
    @staticmethod
    def _extract_video_id(url_or_id: str) -> str:
        """Extract an 11-char YouTube video ID from a URL, or pass a bare ID through."""
        s = (url_or_id or "").strip()
        if not s:
            raise ValueError("No URL or video ID provided.")
        if _VIDEO_ID_RE.match(s):
            return s
        if not s.startswith(("http://", "https://")):
            s = "https://" + s
        parsed = urlparse(s)
        host = (parsed.hostname or "").lower()
        if host.startswith("www."):
            host = host[4:]
        path = parsed.path or ""
        if host == "youtu.be":
            candidate = path.lstrip("/").split("/")[0]
            if _VIDEO_ID_RE.match(candidate):
                return candidate
        if host.endswith("youtube.com") or host == "youtube-nocookie.com":
            qs = parse_qs(parsed.query)
            if qs.get("v"):
                candidate = qs["v"][0]
                if _VIDEO_ID_RE.match(candidate):
                    return candidate
            m = re.match(r"^/(?:shorts|embed|live|v)/([A-Za-z0-9_-]{11})", path)
            if m:
                return m.group(1)
        raise ValueError(f"Could not extract a YouTube video ID from: {url_or_id!r}")

    @classmethod
    def _is_youtube_video_url(cls, url: str) -> bool:
        """True only for a YouTube URL we can pull a video ID out of (watch,
        youtu.be, /shorts/, /embed/, /live/) — not channels/playlists/home."""
        s = (url or "").strip()
        if not s.startswith(("http://", "https://")):
            return False
        try:
            cls._extract_video_id(s)
            return True
        except ValueError:
            return False

    async def _fetch_youtube_transcript(self, url: str) -> str:
        """Return a YouTube video's transcript as plain text, or "" on any failure.

        Uses the optional `youtube-transcript-api` (>= 1.0); the blocking call is
        offloaded to a thread. Empty string on no captions, a blocked IP, an
        unavailable video, etc., so the caller falls back to a normal HTML fetch.
        """
        if not _YT_AVAILABLE:
            return ""
        try:
            video_id = self._extract_video_id(url)
        except ValueError:
            return ""

        def _work() -> str:
            snippets = list(YouTubeTranscriptApi().fetch(video_id))
            lines = [
                (snip.text or "").replace("\n", " ").strip() for snip in snippets
            ]
            body = " ".join(ln for ln in lines if ln)
            return f"YouTube transcript ({video_id}): {body}" if body else ""

        try:
            return await asyncio.to_thread(_work)
        except Exception as e:
            log.debug(f"YouTube transcript failed for {url}: {e}")
            return ""

    # -----------------------------------------------------------------------
    # SearXNG
    # -----------------------------------------------------------------------
    async def _search(
        self,
        session: aiohttp.ClientSession,
        query: str,
        time_range: str = "",
    ) -> List[Dict[str, str]]:
        params: Dict[str, Any] = {
            "q": query,
            "format": "json",
            "number_of_results": self.valves.SEARCH_RESULTS_PER_QUERY,
        }
        if self.valves.SEARCH_ENGINES:
            params["engines"] = self.valves.SEARCH_ENGINES
        # Recency filter (improvement #9): the plan's hint, else the valve.
        tr = (time_range or self.valves.SEARCH_TIME_RANGE or "").strip().lower()
        if tr in ("day", "week", "month", "year"):
            params["time_range"] = tr
        try:
            async with session.get(
                f"{self.valves.SEARXNG_URL.rstrip('/')}/search",
                params=params,
                timeout=aiohttp.ClientTimeout(total=15),
            ) as resp:
                if resp.status == 200:
                    data = await resp.json()
                    out: List[Dict[str, str]] = []
                    for r in data.get("results", []):
                        # SearXNG exposes a publish date inconsistently as
                        # "publishedDate" or "pubdate" depending on the engine.
                        published = r.get("publishedDate") or r.get("pubdate")
                        out.append(
                            {
                                "title": r.get("title", ""),
                                "url": r.get("url", ""),
                                "snippet": r.get("content", ""),
                                "published_date": published,
                            }
                        )
                    return out
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
                        f"- [{r['title']}]({r['url']}): " f"{r['snippet'][:200]}"
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
    "recency": "day|week|month|year|any",
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
- "recency": set to day/week/month/year ONLY for time-sensitive topics (current
  events, fast-moving fields) to bias searches toward fresh sources; otherwise "any"
- Respond with ONLY JSON — no markdown fences, no extra text."""

        raw = await self._llm_call(
            [{"role": "user", "content": prompt}],
            request,
            user,
            temperature=0.4,
        )
        plan = _extract_json(raw)
        if not isinstance(plan, dict):
            plan = {
                "plan_summary": f"Research plan for: {query}",
                "sections": [
                    {
                        "title": "General Overview",
                        "description": "Broad overview",
                        "search_queries": [query, f"{query} overview"],
                    }
                ],
                "initial_queries": [query, f"{query} latest", f"{query} analysis"],
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
    @staticmethod
    def _plan_recency(plan: Dict) -> str:
        """The plan's normalized recency level (``day``/``week``/``month``/
        ``year``), or ``""`` when the topic isn't time-sensitive."""
        r = str((plan or {}).get("recency", "") or "").strip().lower()
        return r if r in ("day", "week", "month", "year") else ""

    @staticmethod
    def _section_coverage(
        collected: List[Dict], section_titles: List[str]
    ) -> Dict[str, int]:
        """Count good (verbatim, non-summary) sources gathered per planned section.

        Compressed summaries are excluded so the count can only *under*-state
        coverage — biasing the loop toward a little extra research rather than
        stopping a section early."""
        cov = {t: 0 for t in section_titles}
        for s in collected:
            if s.get("url") == "compressed_summary":
                continue
            sec = s.get("section")
            if sec in cov:
                cov[sec] += 1
        return cov

    def _build_work(self, query: str, plan: Dict) -> List[Dict[str, str]]:
        """Build the de-duped ``(section, description, query)`` work items.

        `initial_queries` form a cross-cutting "General" bucket; each planned
        section contributes its own queries, attributed to that section (#2)."""
        work: List[Dict[str, str]] = []
        seen_q: set = set()

        def add(section: str, desc: str, q: str) -> None:
            q = (q or "").strip()
            if not q:
                return
            k = (section.lower(), q.lower())
            if k in seen_q:
                return
            seen_q.add(k)
            work.append({"section": section, "desc": desc, "query": q, "done": False})

        for q in plan.get("initial_queries", [query]) or [query]:
            add("General", "Overview and cross-cutting context", q)
        for sec in plan.get("sections", []) or []:
            title = (sec.get("title") or "").strip() or "General"
            desc = sec.get("description", "") or ""
            for q in sec.get("search_queries", []) or []:
                add(title, desc, q)
        return work

    async def _research_loop(
        self,
        query: str,
        plan: Dict,
        request: Any,
        user: Any,
        session: aiohttp.ClientSession,
        emitter: Optional[Callable],
    ) -> Tuple[List[Dict[str, str]], List[str]]:
        collected: List[Dict[str, Any]] = []
        seen_urls: set = set()        # dedup keys (search results + followed links)
        source_urls: List[str] = []   # running unique URLs for the progress snapshot

        # Recency: the plan's hint overrides the SEARCH_TIME_RANGE valve (#9).
        recency = self._plan_recency(plan)

        section_titles = [
            (s.get("title") or "").strip()
            for s in plan.get("sections", []) or []
            if (s.get("title") or "").strip()
        ]
        target = max(1, self.valves.MIN_SOURCES_PER_SECTION)
        work = self._build_work(query, plan)
        sem = asyncio.Semaphore(self.valves.MAX_CONCURRENT_FETCHES)

        async def fetch(u: str) -> Dict[str, Any]:
            async with sem:
                return await self._fetch_page(session, u)

        cycle = 0
        while cycle < self.valves.MAX_RESEARCH_CYCLES:
            cycle += 1
            cov = self._section_coverage(collected, section_titles)
            await self._emit_status(
                emitter,
                f"🔄 Cycle {cycle}/{self.valves.MAX_RESEARCH_CYCLES} · "
                f"{len(collected)} snippets · {len(source_urls)} sources",
            )

            # --- Pick this cycle's queries, gap-driven (#4) ---
            pending = [w for w in work if not w["done"]]
            if not pending:
                # Nothing planned left: ask for targeted queries for the
                # still-starved sections, then stop if there's truly nothing.
                starved = [t for t in section_titles if cov.get(t, 0) < target]
                if starved:
                    await self._emit_status(emitter, "🧠 Generating gap-filling queries…")
                    for w in await self._gap_queries(
                        query, starved, plan, request, user
                    ):
                        work.append({**w, "done": False})
                    pending = [w for w in work if not w["done"]]
                if not pending:
                    break

            # Under-covered sections first; "General" sorts as already-covered.
            pending.sort(key=lambda w: cov.get(w["section"], 10 ** 6))
            batch = pending[: self.valves.QUERIES_PER_CYCLE]
            for w in batch:
                w["done"] = True

            # --- Search (#9 recency, normalized de-dup) ---
            results: List[Tuple[Dict, Dict]] = []  # (work_item, search_result)
            for w in batch:
                await self._emit_status(emitter, f"🔍 [{w['section']}] {w['query'][:70]}…")
                for r in await self._search(session, w["query"], time_range=recency):
                    if not r.get("url"):
                        continue
                    key = _dedup_key(r["url"])
                    if key in seen_urls:
                        continue
                    seen_urls.add(key)
                    results.append((w, r))

            if not results:
                if cycle >= self.valves.MIN_RESEARCH_CYCLES:
                    break
                continue

            # --- Fetch + relevance-extract (#1) ---
            cap = self.valves.MAX_CONCURRENT_FETCHES * 2
            batch_results = results[:cap]
            await self._emit_status(emitter, f"📄 Fetching {len(batch_results)} pages…")
            fetched = await asyncio.gather(
                *[fetch(r["url"]) for _, r in batch_results],
                return_exceptions=True,
            )

            new = 0
            cycle_start = len(collected)  # index of this cycle's first new passage
            hop_pool: List[Tuple[Dict, List[Tuple[str, str]]]] = []
            for (w, r), page in zip(batch_results, fetched):
                if isinstance(page, Exception) or not page:
                    continue
                if await self._absorb(
                    collected, source_urls, w, r, page, depth=0,
                    request=request, user=user,
                ):
                    new += 1
                if page.get("links"):
                    hop_pool.append((w, page["links"]))

            # --- Multi-hop: follow promising in-page links (#8) ---
            hops = self.valves.MAX_LINK_HOPS_PER_CYCLE
            if hops > 0 and hop_pool:
                # Rank each fetched page's candidate links independently, then
                # round-robin across pages (best link of every page first, then
                # each page's second, …) up to the global `hops` budget. A greedy
                # first-page-wins fill would let one link-heavy page consume the
                # whole budget and starve links from pages 2..N; round-robin
                # spreads the hops across this cycle's sources.
                ranked_per_page = [
                    (w, _select_links(links, w["query"], seen_urls, hops))
                    for w, links in hop_pool
                ]
                chosen: List[Tuple[Dict, str]] = []
                picked: set = set()
                depth_cols = max((len(r) for _, r in ranked_per_page), default=0)
                for col in range(depth_cols):
                    for w, ranked in ranked_per_page:
                        if col >= len(ranked):
                            continue
                        href = ranked[col]
                        key = _dedup_key(href)
                        # Skip cross-page duplicates and anything already seen;
                        # `_select_links` only de-duped within its own page.
                        if key in seen_urls or key in picked:
                            continue
                        picked.add(key)
                        seen_urls.add(key)
                        chosen.append((w, href))
                        if len(chosen) >= hops:
                            break
                    if len(chosen) >= hops:
                        break
                if chosen:
                    await self._emit_status(
                        emitter, f"🔗 Following {len(chosen)} in-page links…"
                    )
                    hopped = await asyncio.gather(
                        *[fetch(href) for _, href in chosen],
                        return_exceptions=True,
                    )
                    for (w, href), page in zip(chosen, hopped):
                        if isinstance(page, Exception) or not page:
                            continue
                        link_r = {"title": "", "url": page.get("url", href), "published_date": None}
                        if await self._absorb(
                            collected, source_urls, w, link_r, page, depth=1,
                            request=request, user=user,
                        ):
                            new += 1

            await self._emit_status(
                emitter,
                f"✅ Cycle {cycle}: +{new} snippets "
                f"(total {len(collected)} · {len(source_urls)} sources)",
            )

            # --- Proactive thread-pulling: spawn follow-ups from new findings ---
            # Generated before the structural stop so threads can run; skipped on
            # the final cycle (they'd never execute). new_items is sliced before
            # compression rewrites `collected`.
            new_items = collected[cycle_start:]
            if (
                self.valves.FOLLOWUP_QUERIES_PER_CYCLE > 0
                and new_items
                and cycle < self.valves.MAX_RESEARCH_CYCLES
            ):
                await self._emit_status(
                    emitter, "🧵 Pulling threads from new findings…"
                )
                existing = {
                    (w["section"].lower(), w["query"].lower()) for w in work
                }
                added = 0
                for f in await self._followup_queries(
                    query, new_items, plan, request, user
                ):
                    k = (f["section"].lower(), f["query"].lower())
                    if k in existing:
                        continue
                    existing.add(k)
                    work.append({**f, "done": False})
                    added += 1
                if added:
                    await self._emit_status(
                        emitter, f"🧵 +{added} follow-up queries from sources"
                    )

            # --- Relevance-aware compression (#5) ---
            tw = sum(len(s["content"].split()) for s in collected)
            if tw > self.valves.MAX_TOTAL_CONTEXT_WORDS:
                await self._emit_status(emitter, "📦 Compressing low-relevance notes…")
                collected = await self._compress(
                    query, collected, request, user, recency=recency
                )

            # --- Structural stop: every section meets its coverage target (#4) ---
            cov = self._section_coverage(collected, section_titles)
            if cycle >= self.valves.MIN_RESEARCH_CYCLES and section_titles:
                if all(cov.get(t, 0) >= target for t in section_titles):
                    await self._emit_status(
                        emitter,
                        f"🏁 All {len(section_titles)} sections covered after "
                        f"{cycle} cycles",
                    )
                    break

            if (
                self.valves.CYCLE_DELAY_SECONDS > 0
                and cycle < self.valves.MAX_RESEARCH_CYCLES
            ):
                await asyncio.sleep(self.valves.CYCLE_DELAY_SECONDS)

        return collected, source_urls

    # -----------------------------------------------------------------------
    # Relevance extraction: embedding rerank with lexical fallback
    # -----------------------------------------------------------------------
    async def _relevance(
        self, text: str, query: str, request: Any, user: Any
    ) -> Tuple[str, float]:
        """Return the passages of `text` most relevant to `query`, plus a 0..1 score.

        When ``EMBEDDING_MODEL`` is set and Open WebUI's embeddings endpoint is
        reachable, segments are semantically cosine-ranked against the query —
        this catches synonyms/paraphrases the lexical scorer misses and removes
        the need for the lexical scorer's head-of-page fallback. On any failure
        (endpoint unconfigured, request error, malformed vectors) it transparently
        falls back to the built-in lexical (keyword-overlap) scorer.
        """
        if self.valves.EMBEDDING_MODEL:
            embedded = await self._extract_relevant_embed(
                text, query, self.valves.SNIPPET_MAX_WORDS, request, user
            )
            if embedded is not None:
                return embedded
        return _extract_relevant(text, query, self.valves.SNIPPET_MAX_WORDS)

    async def _extract_relevant_embed(
        self,
        text: str,
        query: str,
        max_words: int,
        request: Any,
        user: Any,
        *,
        scan_max: int = _RELEVANCE_SCAN_MAX_WORDS,
    ) -> Optional[Tuple[str, float]]:
        """Embedding-ranked counterpart of ``_extract_relevant`` (or ``None``).

        Segments are scored by cosine similarity between their embedding and the
        query's, the most-similar ones are reassembled in document order up to
        `max_words`, and the score is the best segment's similarity clamped to
        0..1 (comparable across sources, like the lexical coverage score, so it
        drives retention/compression/ordering the same way). Returns ``None`` to
        signal the caller to fall back to lexical when the text yields no
        segments or the embeddings endpoint can't be used.
        """
        words = (text or "").split()
        if not words:
            return "", 0.0
        if scan_max > 0 and len(words) > scan_max:
            text = " ".join(words[:scan_max])

        segs = _segments(text)
        if not segs or not (query or "").strip():
            return None

        vectors = await self._embed([query] + segs, request, user)
        if not vectors or len(vectors) != len(segs) + 1:
            return None

        qv = vectors[0]
        scored = [
            (_cosine(qv, sv), i, seg)
            for i, (sv, seg) in enumerate(zip(vectors[1:], segs))
        ]
        # Best segments by similarity, then restore document order for readability.
        scored.sort(key=lambda t: t[0], reverse=True)
        top_sim = scored[0][0] if scored else 0.0
        chosen: List[Tuple[int, str]] = []
        budget = 0
        for sim, idx, seg in scored:
            wc = len(seg.split())
            if max_words > 0 and budget + wc > max_words and chosen:
                break
            chosen.append((idx, seg))
            budget += wc
            if max_words > 0 and budget >= max_words:
                break

        chosen.sort(key=lambda t: t[0])
        passages = "\n".join(seg for _, seg in chosen)
        score = max(0.0, min(1.0, top_sim))
        return passages, score

    async def _embed(
        self, texts: List[str], request: Any, user: Any
    ) -> Optional[List[List[float]]]:
        """Embed `texts` via Open WebUI's configured embeddings endpoint.

        Returns a list of vectors (one per input) aligned with `texts`, or
        ``None`` if embeddings are disabled/unconfigured or anything fails — the
        caller then uses the lexical scorer. For the API-backed RAG engines
        (openai/ollama/azure_openai) the ``EMBEDDING_MODEL`` valve's model ID is
        used directly; for the local engine Open WebUI's already-built embedding
        function (its configured model) is used.
        """
        if not self.valves.EMBEDDING_MODEL or request is None or not texts:
            return None
        try:
            cfg = request.app.state.config
            engine = (getattr(cfg, "RAG_EMBEDDING_ENGINE", "") or "").lower()
            if engine in ("openai", "ollama", "azure_openai"):
                from open_webui.retrieval.utils import generate_embeddings

                if engine == "openai":
                    url = getattr(cfg, "RAG_OPENAI_API_BASE_URL", "")
                    key = getattr(cfg, "RAG_OPENAI_API_KEY", "")
                elif engine == "ollama":
                    url = getattr(cfg, "RAG_OLLAMA_BASE_URL", "")
                    key = getattr(cfg, "RAG_OLLAMA_API_KEY", "")
                else:
                    url = getattr(cfg, "RAG_AZURE_OPENAI_BASE_URL", "")
                    key = getattr(cfg, "RAG_AZURE_OPENAI_API_KEY", "")
                vectors = await generate_embeddings(
                    engine=engine,
                    model=self.valves.EMBEDDING_MODEL,
                    text=texts,
                    prefix=None,
                    url=url,
                    key=key,
                    user=user,
                    azure_api_version=getattr(
                        cfg, "RAG_AZURE_OPENAI_API_VERSION", None
                    ),
                )
            else:
                # Local engine (or unknown): use the embedding function Open WebUI
                # already built from its RAG configuration.
                ef = getattr(request.app.state, "EMBEDDING_FUNCTION", None)
                if ef is None:
                    return self._embed_fallback(None)
                vectors = await ef(texts, user=user)

            if (
                isinstance(vectors, list)
                and len(vectors) == len(texts)
                and all(isinstance(v, list) and v for v in vectors)
            ):
                return vectors
            return self._embed_fallback(None)
        except Exception as e:
            return self._embed_fallback(e)

    def _embed_fallback(self, err: Optional[Exception]) -> None:
        """Log the lexical fallback once per process, then return ``None``."""
        if not self._embed_fallback_logged:
            self._embed_fallback_logged = True
            if err is not None:
                log.info(
                    "Embedding rerank unavailable (%s); falling back to the "
                    "lexical scorer.",
                    err,
                )
            else:
                log.info(
                    "Embeddings endpoint not configured/usable; falling back to "
                    "the lexical scorer."
                )
        return None

    async def _absorb(
        self,
        collected: List[Dict],
        source_urls: List[str],
        work_item: Dict,
        result: Dict,
        page: Dict,
        *,
        depth: int,
        request: Any,
        user: Any,
    ) -> bool:
        """Relevance-extract a fetched page and, if useful, append it to
        `collected` with full attribution (#1, #2, #9). Returns True if kept."""
        passages, score = await self._relevance(
            page.get("text", ""), work_item["query"], request, user
        )
        # Drop empty/blocked shells, but keep concise factual passages — the
        # content is already relevance-filtered, so a small floor suffices (the
        # old >20-word floor was tuned for whole-page text, not extracts).
        if not passages or len(passages.split()) < 8:
            return False
        url = page.get("url") or result.get("url")
        collected.append(
            {
                "url": url,
                "title": result.get("title") or url,
                "content": passages,
                "section": work_item["section"],
                "query": work_item["query"],
                "published_date": result.get("published_date"),
                "score": round(score, 3),
                "depth": depth,
            }
        )
        if url and url not in source_urls:
            source_urls.append(url)
        return True

    async def _gap_queries(
        self,
        query: str,
        starved_sections: List[str],
        plan: Dict,
        request: Any,
        user: Any,
    ) -> List[Dict[str, str]]:
        """Generate search queries targeted at the sections still short on sources
        (#4). Returns ``[{section, desc, query}]`` attributed to those sections."""
        sec_map = {
            (s.get("title") or "").strip(): s.get("description", "") or ""
            for s in plan.get("sections", []) or []
        }
        desc_lines = "\n".join(
            f"- {t}: {sec_map.get(t, '')}" for t in starved_sections
        )
        raw = await self._llm_call(
            [
                {
                    "role": "user",
                    "content": (
                        f"Research topic: {query}\n\n"
                        f"These report sections still lack sources:\n{desc_lines}\n\n"
                        f"Generate up to {self.valves.QUERIES_PER_CYCLE} focused web "
                        "search queries to fill these gaps. Respond with ONLY a JSON "
                        'array of objects: [{"section":"<exact section title>",'
                        '"query":"<search query>"}].'
                    ),
                }
            ],
            request,
            user,
            temperature=0.5,
        )
        data = _extract_json(raw)
        out: List[Dict[str, str]] = []
        if isinstance(data, list):
            for item in data:
                if isinstance(item, dict):
                    sec = str(item.get("section", "")).strip()
                    q = str(item.get("query", "")).strip()
                    if not q:
                        continue
                    if sec not in sec_map:
                        sec = starved_sections[0]
                    out.append({"section": sec, "desc": sec_map.get(sec, ""), "query": q})
                elif isinstance(item, str) and item.strip():
                    sec = starved_sections[0]
                    out.append(
                        {"section": sec, "desc": sec_map.get(sec, ""), "query": item.strip()}
                    )
        if not out:
            for t in starved_sections[: self.valves.QUERIES_PER_CYCLE]:
                out.append({"section": t, "desc": sec_map.get(t, ""), "query": f"{query} {t}"})
        return out

    async def _followup_queries(
        self,
        query: str,
        new_items: List[Dict],
        plan: Dict,
        request: Any,
        user: Any,
    ) -> List[Dict[str, str]]:
        """Spawn follow-up searches from what the latest passages actually say.

        A cheap LLM pass reads the cycle's freshly-gathered passages and names the
        specific entities/claims (a pivotal study, dataset, person, organisation,
        or counter-claim) that deserve their own search — letting the loop pull
        threads rather than only execute the fixed plan. Returns up to
        ``FOLLOWUP_QUERIES_PER_CYCLE`` ``{section, desc, query}`` items attributed
        to the most relevant planned section (falling back to "General"); ``[]``
        when nothing warrants follow-up or the model returns nothing usable.
        """
        limit = self.valves.FOLLOWUP_QUERIES_PER_CYCLE
        if limit <= 0 or not new_items:
            return []
        sec_map = {
            (s.get("title") or "").strip(): s.get("description", "") or ""
            for s in plan.get("sections", []) or []
        }
        section_list = ", ".join(t for t in sec_map) or "General"

        # Compact digest of the new findings (title + extracted passage), capped
        # so this stays a cheap pass regardless of how much was gathered.
        notes: List[str] = []
        budget = 6000
        for s in new_items:
            if s.get("url") == "compressed_summary":
                continue
            chunk = f"- {s.get('title') or s.get('url')}: {s.get('content', '')}"
            notes.append(chunk[:1200])
            budget -= len(chunk[:1200])
            if budget <= 0:
                break
        if not notes:
            return []

        raw = await self._llm_call(
            [
                {
                    "role": "user",
                    "content": (
                        f"Research topic: {query}\n\n"
                        "Below are notes just gathered. Identify the most "
                        "promising NEW threads worth a dedicated follow-up web "
                        "search — a specific named study/paper, dataset, person, "
                        "organisation, event, statistic, or a counter-claim that "
                        "should be checked. Ignore generic restatements of the "
                        "topic. For each, write a focused search query.\n\n"
                        f"Notes:\n{chr(10).join(notes)}\n\n"
                        f"Attribute each to the single most relevant section from: "
                        f"{section_list}.\n"
                        f"Return up to {limit} items as ONLY a JSON array of "
                        'objects: [{"section":"<section title>","query":'
                        '"<search query>"}]. Return [] if nothing genuinely '
                        "warrants a follow-up."
                    ),
                }
            ],
            request,
            user,
            temperature=0.4,
            max_tokens=500,
        )
        data = _extract_json(raw)
        out: List[Dict[str, str]] = []
        if isinstance(data, list):
            for item in data:
                if not isinstance(item, dict):
                    continue
                q = str(item.get("query", "")).strip()
                if not q:
                    continue
                sec = str(item.get("section", "")).strip()
                if sec not in sec_map:
                    sec = "General"
                out.append({"section": sec, "desc": sec_map.get(sec, ""), "query": q})
                if len(out) >= limit:
                    break
        return out

    @staticmethod
    def _relevance_key(recency: str) -> Callable[[Dict], Tuple[float, float]]:
        """Sort key ``(score, freshness)`` for ranking sources by relevance.

        On a time-sensitive topic (`recency` set) the second element is the
        source's publish timestamp so equal-relevance sources rank fresher-first;
        otherwise it's 0.0 and a stable sort preserves the original order.
        """
        def key(s: Dict) -> Tuple[float, float]:
            score = float(s.get("score", 0.0) or 0.0)
            if not recency:
                return (score, 0.0)
            dt = _parse_date(s.get("published_date"))
            return (score, dt.timestamp() if dt else 0.0)

        return key

    async def _compress(
        self,
        query: str,
        collected: List[Dict],
        request: Any,
        user: Any,
        *,
        recency: str = "",
    ) -> List[Dict[str, Any]]:
        """Shrink the running context by keeping the highest-relevance sources
        verbatim and summarizing the rest (#5) — relevance-aware, unlike the old
        keep-the-recent-half approach. Section attribution and citation URLs of
        the summarized sources are preserved. When `recency` is set (a time-
        sensitive topic), equal-relevance sources are kept fresher-first so the
        verbatim survivors skew recent."""
        # Keep prior summaries as-is; rank the rest by relevance score, breaking
        # ties toward fresher sources on time-sensitive topics.
        prior_summaries = [s for s in collected if s.get("url") == "compressed_summary"]
        real = [s for s in collected if s.get("url") != "compressed_summary"]
        ranked = sorted(real, key=self._relevance_key(recency), reverse=True)

        budget = max(1, self.valves.MAX_TOTAL_CONTEXT_WORDS // 2)
        keep_ids: set = set()
        used = 0
        for s in ranked:
            wc = len(s.get("content", "").split())
            if used + wc <= budget or not keep_ids:
                keep_ids.add(id(s))
                used += wc
        rest = [s for s in real if id(s) not in keep_ids]
        if not rest:
            return collected

        old_text = "\n\n".join(
            f"Source: {s['title']} ({s['url']})\n{s['content']}" for s in rest
        )
        summary = await self._llm_call(
            [
                {
                    "role": "user",
                    "content": (
                        "Summarise these research notes. Keep all key facts/data.\n"
                        f"Topic: {query}\n\n{old_text[:15000]}"
                    ),
                }
            ],
            request,
            user,
            temperature=0.2,
            max_tokens=3000,
        )
        covered = [s["url"] for s in rest if s.get("url")]
        summary_entry = {
            "url": "compressed_summary",
            "title": f"Summary of {len(rest)} lower-relevance sources",
            "content": summary,
            "source_urls": covered,
            "section": "General",
            "query": query,
            "score": 0.0,
            "depth": 0,
        }
        # Verbatim keepers in their original order, then prior + new summaries.
        kept = [s for s in collected if id(s) in keep_ids]
        return kept + prior_summaries + [summary_entry]

    # -----------------------------------------------------------------------
    # Phase 3 — final report
    # -----------------------------------------------------------------------
    def _build_source_index(
        self, collected: List[Dict], recency: str = ""
    ) -> Tuple[List[Dict], str]:
        """Assign one global ``[n]`` number to each source and build the shared
        citation list. Sources past REPORT_CONTEXT_MAX_CHARS are dropped from
        BOTH the data and the references, so a citation can never point at a
        source the model wasn't shown. Each source's publish date (when known) is
        shown in its data block and reference line, and — on a time-sensitive
        topic (`recency` set) — clearly old sources are flagged ``may be
        outdated``. Returns ``(indexed, refs)`` where each `indexed` entry is
        ``{idx, src, block}`` (block = the SOURCE-n text)."""
        source_cap = self.valves.REPORT_SOURCE_MAX_CHARS
        ctx_budget = self.valves.REPORT_CONTEXT_MAX_CHARS
        now = datetime.utcnow()

        indexed: List[Dict] = []
        ref_lines: List[str] = []
        used = 0
        n = 0
        for s in collected:
            content = s.get("content") or ""
            if source_cap > 0:
                content = content[:source_cap]
            idx = n + 1
            dt = _parse_date(s.get("published_date"))
            stale = _is_stale(dt, recency, now)
            date_str = dt.strftime("%Y-%m-%d") if dt else ""
            block_date = f" [published {date_str}]" if date_str else ""
            block = (
                f"\n--- SOURCE {idx}: {s.get('title') or s.get('url')} "
                f"({s.get('url')}){block_date} ---\n{content}\n"
            )
            if ctx_budget > 0 and n > 0 and used + len(block) > ctx_budget:
                break
            used += len(block)
            n = idx
            indexed.append({"idx": idx, "src": s, "block": block})
            if s.get("url") == "compressed_summary":
                covered = s.get("source_urls") or []
                ref_lines.append(
                    f"[{idx}] Summary of earlier sources"
                    + (": " + "; ".join(covered) if covered else "")
                )
            else:
                ref = f"[{idx}] {s.get('url')}"
                if date_str:
                    ref += f" (published {date_str}"
                    ref += " — may be outdated)" if stale else ")"
                ref_lines.append(ref)
        if len(collected) - n > 0:
            log.info(
                "Report context budget reached: included %d of %d sources",
                n,
                len(collected),
            )
        return indexed, "\n".join(ref_lines)

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
        indexed, refs = self._build_source_index(
            collected, self._plan_recency(plan)
        )

        mode = (self.valves.REPORT_MODE or "sectioned").strip().lower()
        if mode == "single" or not plan.get("sections"):
            return await self._report_single(query, plan, indexed, refs, request, user)
        return await self._report_sectioned(
            query, plan, indexed, refs, request, user, emitter
        )

    async def _report_single(
        self, query: str, plan: Dict, indexed: List[Dict], refs: str,
        request: Any, user: Any,
    ) -> str:
        """Legacy one-shot report: the whole report in a single LLM call."""
        ctx = "".join(e["block"] for e in indexed)
        secs = "\n".join(
            f"- {s['title']}: {s.get('description','')}"
            for s in plan.get("sections", [])
        )
        smin = self.valves.SECTION_MIN_WORDS
        smax = self.valves.SECTION_MAX_WORDS
        prompt = f"""You are a research report writer. Write a comprehensive, well-structured research report based ONLY on the provided research data. Do NOT rely on your own knowledge — use only the sources below.

**Research Topic:** {query}

**Planned Sections:**
{secs}

**Research Data:**
{ctx}

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
- Each section MUST be {smin}-{smax} words. Develop each section thoroughly with analysis, examples, and data from the sources.
- Write in a clear, professional, analytical tone.
- Base ALL claims on the provided research data and cite with [n].
- Include specific data, statistics, and findings from sources.
- Cover ALL planned sections — do not skip or merge sections.
- If data is thin for a section, note the gap but still write what you can.
- Write the COMPLETE report — do not truncate or say "continued below"."""
        return await self._llm_call(
            [{"role": "user", "content": prompt}],
            request, user, temperature=0.3, max_tokens=self.valves.REPORT_MAX_TOKENS,
        )

    async def _report_sectioned(
        self, query: str, plan: Dict, indexed: List[Dict], refs: str,
        request: Any, user: Any, emitter: Optional[Callable],
    ) -> str:
        """Section-by-section report (#2, #3): each section is drafted in its own
        LLM call against only its attributed sources (plus shared cross-cutting
        ones), all citing the SAME global ``[n]`` index; a synthesis pass then
        writes the title/abstract/conclusion. Sections are drafted concurrently."""
        sections = plan.get("sections", []) or []
        # Cross-cutting sources (General bucket + compressed summaries) are shared
        # context every section may cite.
        general = [
            e for e in indexed
            if e["src"].get("section") in (None, "", "General")
            or e["src"].get("url") == "compressed_summary"
        ]

        async def draft(sec: Dict) -> Tuple[str, str]:
            title = (sec.get("title") or "Section").strip()
            own = [e for e in indexed if e["src"].get("section") == title]
            # Always give the section its own sources; top up with shared ones.
            chosen = own + [e for e in general if e not in own]
            await self._emit_status(emitter, f"📝 Writing section: {title[:60]}…")
            text = await self._write_section(
                query, title, sec.get("description", "") or "", chosen, len(own),
                request, user,
            )
            return title, text

        await self._emit_status(
            emitter, f"📝 Drafting {len(sections)} sections…"
        )
        drafts = await asyncio.gather(
            *[draft(sec) for sec in sections], return_exceptions=True
        )
        section_texts: List[str] = []
        toc: List[str] = []
        for i, d in enumerate(drafts):
            if isinstance(d, Exception):
                title = (sections[i].get("title") or f"Section {i+1}").strip()
                section_texts.append(f"## {title}\n\n*(Section generation failed.)*")
                toc.append(title)
                continue
            title, text = d
            toc.append(title)
            text = text.strip()
            if not text.lower().startswith("#"):
                text = f"## {title}\n\n{text}"
            section_texts.append(text)

        # Synthesis pass: title, abstract, conclusion from the section drafts.
        await self._emit_status(emitter, "📝 Writing abstract & conclusion…")
        title, abstract, conclusion = await self._write_synthesis(
            query, "\n\n".join(section_texts), request, user
        )

        toc_md = "\n".join(f"{i+1}. {t}" for i, t in enumerate(toc))
        parts = [
            f"# {title}",
            "## Abstract\n" + abstract,
            "## Table of Contents\n" + toc_md,
            "\n\n".join(section_texts),
            "## Conclusion\n" + conclusion,
            "## Sources\n" + (refs or "*(no sources)*"),
        ]
        return "\n\n".join(parts)

    async def _write_section(
        self, query: str, title: str, desc: str, chosen: List[Dict],
        own_count: int, request: Any, user: Any,
    ) -> str:
        """Draft a single report section against `chosen` sources, citing their
        global ``[n]`` numbers."""
        smin = self.valves.SECTION_MIN_WORDS
        smax = self.valves.SECTION_MAX_WORDS
        blocks = "".join(e["block"] for e in chosen) or "(no sources gathered for this section)"
        coverage_note = (
            f"This section has {own_count} directly-relevant source(s)."
            + (" Coverage is thin — note any gaps briefly but still write what the "
               "sources support." if own_count < self.valves.MIN_SOURCES_PER_SECTION else "")
        )
        prompt = f"""You are writing ONE section of a larger research report. Write ONLY this section — no title page, abstract, intro, or conclusion.

Research topic: {query}
Section title: {title}
Section focus: {desc}

{coverage_note}

Sources (cite using the bracketed [n] numbers EXACTLY as shown — they are global to the whole report):
{blocks}

Write the section as Markdown beginning with "## {title}". Target {smin}-{smax} words.
Base every claim ONLY on the sources above and cite with [n]. Include specific data, findings, and differing viewpoints where present. Do not invent sources or citation numbers."""
        return await self._llm_call(
            [{"role": "user", "content": prompt}],
            request, user, temperature=0.3, max_tokens=self.valves.REPORT_MAX_TOKENS,
        )

    async def _write_synthesis(
        self, query: str, sections_md: str, request: Any, user: Any,
    ) -> Tuple[str, str, str]:
        """Write the report title, abstract, and conclusion from the drafted
        sections. Returns ``(title, abstract, conclusion)`` with safe fallbacks."""
        prompt = f"""You are finalizing a research report. Based ONLY on the drafted sections below, write the report's title, abstract, and conclusion.

Research topic: {query}

Drafted sections:
{sections_md[:20000]}

Respond with ONLY a JSON object:
{{"title": "concise report title", "abstract": "200-300 word summary of key findings", "conclusion": "synthesis of findings, key takeaways, limitations, and areas for further research"}}
You may reference the existing [n] citation numbers that appear in the sections."""
        raw = await self._llm_call(
            [{"role": "user", "content": prompt}],
            request, user, temperature=0.3, max_tokens=2000,
        )
        data = _extract_json(raw)
        if isinstance(data, dict):
            return (
                str(data.get("title") or f"Research Report: {query}").strip(),
                str(data.get("abstract") or "").strip(),
                str(data.get("conclusion") or "").strip(),
            )
        return f"Research Report: {query}", "", ""

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
                p.append(f"**Sections:** " f"{', '.join(s['title'] for s in ss)}\n")
        if max_cycles:
            p.append(
                f"**Progress:** cycle {cycle}/{max_cycles} · "
                f"{snippets} snippets · {sources} sources\n"
            )
        if urls:
            p.append("\nSources found so far\n")
            for i, u in enumerate(urls, 1):
                p.append(f"{i}. {u}")
        p.append("\n*Research in progress — this updates automatically…*\n")
        return "\n".join(p)

    # -----------------------------------------------------------------------
    # Main entry point
    # -----------------------------------------------------------------------
    async def pipe(
        self,
        body: dict,
        __user__: Optional[dict] = None,
        __event_emitter__: Optional[Callable[[dict], Awaitable[None]]] = None,
        __event_call__: Optional[Callable[[dict], Awaitable[Any]]] = None,
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
                    user_obj = await Users.get_user_by_id(__user__["id"])
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
                return await self._read_completion(response) or "Deep Research"
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
                content = msg.get("content")
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
            user_obj = await Users.get_user_by_id(__user__["id"])

        try:
            async with aiohttp.ClientSession() as session:

                # ======================================================
                # PHASE 1 — plan
                # ======================================================
                await self._emit_status(__event_emitter__, "🚀 Starting deep research…")
                plan_text, _, plan = await self._generate_plan(
                    user_query,
                    __request__,
                    user_obj,
                    session,
                    __event_emitter__,
                )

                # ======================================================
                # PLAN CONFIRMATION (only user interaction point)
                # ======================================================
                if not self.valves.SKIP_PLAN_CONFIRMATION and __event_call__:
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
                                "placeholder": ("ok / yes / your modifications…"),
                            },
                        }
                    )

                    resp = ""
                    if isinstance(confirmation, dict):
                        resp = str(confirmation.get("value", "")).strip().lower()
                    elif isinstance(confirmation, str):
                        resp = confirmation.strip().lower()

                    if resp not in {
                        "ok",
                        "yes",
                        "y",
                        "continue",
                        "proceed",
                        "go",
                        "looks good",
                        "lgtm",
                        "approve",
                        "confirmed",
                        "",
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
                            __request__,
                            user_obj,
                            temperature=0.4,
                        )
                        modified = _extract_json(mod)
                        if isinstance(modified, dict):
                            plan = modified
                        else:
                            log.warning("Modified plan parse failed")

                # ======================================================
                # Write initial progress into message body
                # ======================================================
                await self._emit_replace(
                    __event_emitter__,
                    self._progress_msg("Starting research…", plan=plan),
                )

                # ======================================================
                # PHASE 2 — research loop (autonomous, no user prompts)
                # ======================================================
                await self._emit_status(__event_emitter__, "🔬 Researching…")
                collected, source_urls = await self._research_loop(
                    user_query,
                    plan,
                    __request__,
                    user_obj,
                    session,
                    __event_emitter__,
                )

                if not collected:
                    msg = (
                        "Unable to gather sufficient research data. "
                        "Check SearXNG availability or refine the query."
                    )
                    await self._emit_replace(__event_emitter__, "")
                    await self._emit_status(
                        __event_emitter__,
                        "⚠️ No data collected",
                        done=True,
                    )
                    return msg + _DONE_MARKER

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
                    user_query,
                    plan,
                    collected,
                    source_urls,
                    __request__,
                    user_obj,
                    __event_emitter__,
                )

                # Append the done-marker so the re-entry guard works
                report_with_marker = report + _DONE_MARKER

                # Clear any "in-progress" content from the message body so
                # the returned report doesn't get appended after it.
                await self._emit_replace(__event_emitter__, "")

                # Citations
                for s in collected:
                    if s["url"] and s["url"] != "compressed_summary":
                        await self._emit_citation(
                            __event_emitter__,
                            s["url"],
                            s["title"],
                            s["content"][:300],
                        )

                await self._emit_status(
                    __event_emitter__,
                    f"✅ Done — {len(source_urls)} sources, "
                    f"{len(plan.get('sections',[]))} sections",
                    done=True,
                )

                # Return the report as the final message content.
                # The _DONE_MARKER prevents the re-entry guard at the top
                # of pipe() from re-running research on any subsequent
                # invocation in the same conversation.
                return report_with_marker

        except Exception as e:
            log.error(f"Deep Research error: {e}", exc_info=True)
            await self._emit_status(
                __event_emitter__,
                f"❌ Failed: {e}",
                done=True,
            )
            return f"Research error: {e}"