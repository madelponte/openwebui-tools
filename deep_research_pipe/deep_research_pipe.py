"""
title: Deep Research
author: mdelponte
version: 3.0.0
license: MIT
description: >
    A deep research pipe that takes a user query, generates a research plan,
    presents it for confirmation, then runs a section-attributed, gap-driven
    research loop and writes a structured, citation-grounded report. Searching
    and page reading are delegated to the companion MCP server's `search_web`
    (Brave LLM Context: relevance-ranked excerpts plus source metadata) and
    `fetch_page` (browser-first acquisition, quality gate, Firecrawl fallback,
    Tika documents, Reddit, and YouTube transcripts) tools, attached to Open
    WebUI as an MCP tool server. Fetched pages are relevance-extracted (the
    passages matching each query, not the page head), de-duplicated, and the
    loop follows promising in-page links (multi-hop) and biases toward recent
    sources when the topic is time-sensitive. A page that can't be read still
    contributes its search excerpts. The report is written section by section
    against each section's own sources under one global citation index.
required_open_webui_version: 0.12.4
"""

import asyncio
import json
import logging
import math
import re
from datetime import datetime, timezone
from urllib.parse import (
    urlparse,
    urlunparse,
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

from pydantic import BaseModel, Field

from open_webui.config import (
    RAG_EMBEDDING_CONTENT_PREFIX,
    RAG_EMBEDDING_QUERY_PREFIX,
)
from open_webui.utils.chat import generate_chat_completion
from open_webui.utils.misc import pop_system_message, get_last_user_message
from open_webui.models.config import Config
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
            logging.Formatter("%(asctime)s - %(name)s - %(levelname)s - %(message)s")
        )
        logger.addHandler(handler)
        logger.propagate = False
    return logger


log = _setup_logger()

# Marker we embed in the message body so we can detect that this
# conversation already contains a completed research report. A later message
# in the same chat is then answered as a follow-up question about the report
# instead of starting a second research run.
_DONE_MARKER = "\n\n---DEEP-RESEARCH-COMPLETE---"


# ---------------------------------------------------------------------------
# MCP tool layer
#
# The pipe no longer fetches anything itself. It calls two tools on the
# companion MCP server, which owns search (Brave LLM Context) and the whole
# page-acquisition ladder (SSRF guard, FlareSolverr, quality gate, Firecrawl,
# Tika, Reddit, YouTube transcripts). Open WebUI hands a pipe every tool
# resolved for the chat in `__tools__`, keyed "<server id>_<tool name>"; when
# the server isn't attached to the chat the pipe connects to it directly
# through Open WebUI's own MCP connection settings.
# ---------------------------------------------------------------------------

SEARCH_TOOL = "search_web"
FETCH_TOOL = "fetch_page"
_REQUIRED_TOOLS = (SEARCH_TOOL, FETCH_TOOL)

# search_web rejects queries over 600 characters or 75 words; LLM-written
# queries are trimmed to fit rather than failing the call.
_SEARCH_QUERY_MAX_CHARS = 600
_SEARCH_QUERY_MAX_WORDS = 75

# fetch_page formats whose content is markdown (links, heading anchors).
_MARKDOWN_FORMATS = ("markdown", "section")


class ToolCallError(RuntimeError):
    """An MCP tool call failed: a tool error (isError), a transport failure, a
    timeout, or a result without a JSON object payload."""


def _parse_tool_result(result: Any) -> Dict[str, Any]:
    """Return the JSON object payload of an Open WebUI MCP tool result.

    Open WebUI's ``MCPClient.call_tool`` returns the MCP content list
    (``[{"type": "text", "text": "<json>"}, ...]``) and raises on ``isError``.
    This server's text block is the JSON payload (identical to its
    structuredContent), so the first text block that parses to an object wins.
    """
    if isinstance(result, dict) and "type" not in result:
        return result
    items = result if isinstance(result, list) else [result]
    for item in items:
        text = item.get("text") if isinstance(item, dict) else item
        if not isinstance(text, str):
            continue
        try:
            data = json.loads(text)
        except (TypeError, ValueError):
            continue
        if isinstance(data, dict):
            return data
    raise ToolCallError("tool returned no JSON object payload")


def _tools_from_registry(
    registry: Any, server_id: str = ""
) -> Optional[Tuple[str, Dict[str, Callable[..., Any]]]]:
    """Find search_web + fetch_page callables in Open WebUI's ``__tools__``.

    MCP entries are keyed ``"<server id>_<tool name>"`` with ``type == "mcp"``
    and an async ``callable(**kwargs)``. Returns ``(server_id, {tool: callable})``
    for a server that exposes both tools (restricted to `server_id` when given),
    or ``None``.
    """
    servers: Dict[str, Dict[str, Callable[..., Any]]] = {}
    for key, entry in (registry or {}).items():
        if not isinstance(entry, dict) or entry.get("type") != "mcp":
            continue
        fn = entry.get("callable")
        if not callable(fn):
            continue
        for tool in _REQUIRED_TOOLS:
            suffix = f"_{tool}"
            if isinstance(key, str) and key.endswith(suffix) and len(key) > len(suffix):
                servers.setdefault(key[: -len(suffix)], {})[tool] = fn
    candidates = sorted(
        sid
        for sid, fns in servers.items()
        if all(t in fns for t in _REQUIRED_TOOLS) and (not server_id or sid == server_id)
    )
    if not candidates:
        return None
    return candidates[0], servers[candidates[0]]


class ResearchTools:
    """One research run's handle on the MCP server's search_web / fetch_page.

    `invoke(tool_name, args)` performs the raw call — either a ``__tools__``
    callable or a pipe-owned ``MCPClient`` — and this class adds the per-call
    timeout, result parsing, and mapping onto the shapes the research loop
    consumes. Instances are per run, so `last_error` never leaks between
    concurrent chats.
    """

    def __init__(
        self,
        server_id: str,
        invoke: Callable[[str, Dict[str, Any]], Awaitable[Any]],
        *,
        timeout: float,
        num_results: int,
        default_time_range: str,
    ):
        self.server_id = server_id
        self._invoke = invoke
        self._timeout = timeout
        self._num_results = num_results
        self._default_time_range = default_time_range
        self.last_error = ""

    async def call(self, tool: str, args: Dict[str, Any]) -> Dict[str, Any]:
        """Call `tool` and return its JSON payload, or raise ``ToolCallError``.

        Uses ``asyncio.timeout`` rather than ``wait_for`` so the call stays in
        the caller's task (Open WebUI's MCP client must not be torn down from a
        foreign task). Task cancellation is never swallowed.
        """
        try:
            if self._timeout > 0:
                async with asyncio.timeout(self._timeout):
                    raw = await self._invoke(tool, args)
            else:
                raw = await self._invoke(tool, args)
            return _parse_tool_result(raw)
        except ToolCallError:
            raise
        except TimeoutError as e:
            raise ToolCallError(f"{tool} timed out after {self._timeout:g}s") from e
        except Exception as e:
            raise ToolCallError(str(e) or type(e).__name__) from e

    async def search(self, query: str, time_range: str = "") -> List[Dict[str, Any]]:
        """Run search_web and map its results to ``{title, url, snippet,
        published_date}``. ``snippet`` is Brave's query-relevant excerpts joined
        one per line. Returns ``[]`` on failure (recorded in `last_error`)."""
        q = " ".join((query or "").split()[:_SEARCH_QUERY_MAX_WORDS])[
            :_SEARCH_QUERY_MAX_CHARS
        ].strip()
        if not q:
            return []
        args: Dict[str, Any] = {"query": q}
        if self._num_results > 0:
            args["num_results"] = self._num_results
        tr = (time_range or self._default_time_range or "").strip()
        if tr:
            args["time_range"] = tr
        try:
            payload = await self.call(SEARCH_TOOL, args)
        except ToolCallError as e:
            self.last_error = f"{SEARCH_TOOL}: {e}"
            log.warning("search_web failed for %r: %s", q, e)
            return []
        out: List[Dict[str, Any]] = []
        for r in payload.get("results") or []:
            if not isinstance(r, dict) or not r.get("url"):
                continue
            snippets = [
                s.strip()
                for s in (r.get("snippets") or [])
                if isinstance(s, str) and s.strip()
            ]
            out.append(
                {
                    "title": r.get("title") or r.get("site_name") or "",
                    "url": r["url"],
                    "snippet": "\n".join(snippets) or (r.get("description") or ""),
                    "published_date": r.get("page_date"),
                }
            )
        return out

    async def fetch(self, url: str) -> Dict[str, Any]:
        """Read `url` with fetch_page (default text mode).

        Returns a page dict ``{url, title, text, links, kind}``: ``text`` is the
        readable content (markdown flattened to plain lines; relevance extraction
        happens later), ``links`` are the page's ``(anchor, url)`` links for
        multi-hop, and ``kind`` is fetch_page's ``format`` (or ``"failed"``, with
        empty text, when the tool raised).
        """
        try:
            payload = await self.call(FETCH_TOOL, {"url": url})
        except ToolCallError as e:
            log.debug("fetch_page failed for %s: %s", url, e)
            return {"url": url, "title": "", "text": "", "links": [], "kind": "failed"}
        content = payload.get("content")
        if not isinstance(content, str):
            content = json.dumps(content, ensure_ascii=False) if content else ""
        fmt = str(payload.get("format") or "")
        is_md = fmt in _MARKDOWN_FORMATS
        return {
            "url": payload.get("url") or url,
            "title": payload.get("title") or "",
            "text": _markdown_to_text(content) if is_md else content,
            "links": _markdown_links(content) if is_md else [],
            "kind": fmt or "text",
        }


# ---------------------------------------------------------------------------
# Markdown helpers for fetch_page content
#
# fetch_page returns markdown: links are absolute ``[anchor](url)`` (in-page
# citation markers already rewritten to their source URLs), headings carry
# ``{#anchor}`` citation markers. Links feed multi-hop following; the scoring
# text drops the markup so URLs and anchors don't dilute relevance.
# ---------------------------------------------------------------------------

# A markdown link or image. The anchor allows one level of nested brackets so
# citation markers rendered as "[[12]](url)" match; the URL allows one level of
# balanced parentheses so Wikipedia-style "Foo_(bar)" targets aren't cut short;
# an optional "title" is tolerated.
_MD_LINK_RE = re.compile(
    r"(!?)\[((?:[^\[\]\n]|\[[^\[\]\n]*\]){0,300})\]"
    r'\(((?:https?://)(?:[^\s()]|\([^\s()]*\))+)(?:\s+"[^"\n]*")?\)'
)
# An anchor that is only a citation marker ("[12]", "12", "[a]", "note 3").
_CITATION_ANCHOR_RE = re.compile(
    r"^(?:\[(?:\d{1,4}|[a-z])\]|\d{1,4}|note\s*\d{1,4})$", re.I
)
# fetch_page's image placeholder, which links to the image's file page.
_IMAGE_MARKER_PREFIX = "[Image at this location"
# How much text before a citation marker stands in for its anchor text.
_CITATION_CONTEXT_CHARS = 200
_MD_HEADING_ANCHOR_RE = re.compile(r"[ \t]*\{#[^{}\s]+\}")
_MD_LINE_PREFIX_RE = re.compile(r"^[ \t]{0,3}(?:#{1,6}|>+|[-*+]|\d+[.)])[ \t]+", re.M)
_MD_RULE_RE = re.compile(r"^[ \t]*(?:[-*_=][ \t]*){3,}$", re.M)


def _link_text(m: "re.Match[str]") -> str:
    """Replacement text for a matched link: its anchor, or nothing for a
    citation marker."""
    anchor = m.group(2).strip()
    return "" if _CITATION_ANCHOR_RE.match(anchor) else anchor


def _markdown_links(md: str) -> List[Tuple[str, str]]:
    """``(anchor_text, absolute_url)`` pairs for the non-image links in `md`.

    A citation marker (``[[12]](https://source)``, already resolved by
    fetch_page to the footnote's external source) has no useful anchor text, so
    the sentence it is attached to — the text just before it on the same line —
    stands in. That lets multi-hop rank a page's primary sources by the claim
    they support.
    """
    md = md or ""
    out: List[Tuple[str, str]] = []
    for m in _MD_LINK_RE.finditer(md):
        bang, anchor, url = m.group(1), m.group(2).strip(), m.group(3)
        if bang or anchor.startswith(_IMAGE_MARKER_PREFIX):
            continue
        if _CITATION_ANCHOR_RE.match(anchor):
            line_start = md.rfind("\n", 0, m.start()) + 1
            before = md[max(line_start, m.start() - _CITATION_CONTEXT_CHARS) : m.start()]
            anchor = _MD_LINK_RE.sub(_link_text, before).strip()
        out.append((anchor, url))
    return out


def _markdown_to_text(md: str) -> str:
    """Flatten fetch_page markdown to plain lines for relevance scoring.

    Links and images become their anchor/alt text (citation markers vanish),
    heading anchors, list/quote/heading markers, rules, and emphasis are
    dropped, and blank lines collapse. Line breaks are kept so ``_segments``
    scores paragraphs individually.
    """
    text = _MD_HEADING_ANCHOR_RE.sub("", md or "")
    text = _MD_LINK_RE.sub(_link_text, text)
    text = _MD_RULE_RE.sub("", text)
    text = _MD_LINE_PREFIX_RE.sub("", text)
    text = re.sub(r"\*{1,3}|`+", "", text)
    return re.sub(r"\n\s*\n+", "\n", text).strip()


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

    Splits per line first (fetch_page markdown, documents, and search excerpts
    keep newlines), then breaks any over-long line — a transcript or a page
    flattened into one blob — into sentence-ish chunks. Blank fragments are
    dropped.
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
# search_web reports a provider page date (``page_date``) only for some results,
# and its format isn't guaranteed (ISO 8601, RFC 822, a bare year, …). We parse
# it best-effort to: surface it in the report's reference list, break relevance
# ties toward fresher sources when the plan flagged the topic as time-sensitive,
# and flag clearly stale sources. Anything unparseable is simply treated as "no
# date" — never an error.
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
    """Best-effort parse of a search result's page date into a naive-UTC datetime.

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
                "snippet analysis, and final report writing. This must be an "
                "accessible Open WebUI model ID; it cannot be this pipe's ID."
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

        # --- MCP server (search_web + fetch_page) ---
        MCP_SERVER_ID: str = Field(
            default="",
            description=(
                "ID of the MCP tool server (Admin Settings → External Tools) "
                "that provides search_web and fetch_page. Blank = auto-detect: "
                "use the server's tools when it is enabled for the chat, else "
                "connect to the first enabled MCP connection that exposes both "
                "tools."
            ),
        )
        SEARCH_RESULTS_PER_QUERY: int = Field(
            default=5,
            description=(
                "Max results per search_web query (passed as num_results; the "
                "MCP server clamps it to its own configured cap). 0 = the "
                "server's cap."
            ),
        )
        SEARCH_TIME_RANGE: str = Field(
            default="",
            description=(
                "Freshness filter applied to every search: '', 'day', 'week', "
                "'month', 'year', 'all', or 'YYYY-MM-DD to YYYY-MM-DD'. Blank = "
                "the server's default. The research plan's own recency hint "
                "overrides this per run."
            ),
        )
        MAX_PAGE_FETCHES_PER_CYCLE: int = Field(
            default=10,
            description=(
                "How many new search results per cycle are read in full with "
                "fetch_page. The rest (and any page that can't be read) still "
                "contribute their search_web excerpts. 0 = excerpts only (much "
                "faster, shallower)."
            ),
        )
        MAX_CONCURRENT_FETCHES: int = Field(
            default=5,
            description="Max fetch_page calls in flight at once.",
        )
        TOOL_CALL_TIMEOUT: int = Field(
            default=180,
            description=(
                "Seconds the pipe waits for one search_web/fetch_page call "
                "before giving up on it (the source then falls back to its "
                "search excerpts). 0 = rely on the server's own deadlines."
            ),
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
        model = (self.valves.RESEARCH_MODEL or "").strip()
        if not model:
            raise ValueError(
                "RESEARCH_MODEL is not configured. Set it to an accessible "
                "Open WebUI model ID in the Deep Research function valves."
            )
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
        # Open WebUI normally returns a dict for non-streaming completions, but
        # provider errors and some compatible backends return a JSONResponse.
        if hasattr(response, "model_dump"):
            response = response.model_dump()
        if not isinstance(response, dict) and hasattr(response, "body"):
            body = response.body
            if isinstance(body, bytes):
                body = body.decode("utf-8", errors="replace")
            try:
                response = json.loads(body)
            except (TypeError, json.JSONDecodeError):
                response = None
        if isinstance(response, dict):
            if response.get("error"):
                error = response["error"]
                if isinstance(error, dict):
                    error = error.get("message") or error.get("detail") or error
                raise RuntimeError(f"Model request failed: {error}")
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
                    "metadata": [
                        {"source": url, "name": title or url, "url": url}
                    ],
                    "source": {"name": title or url, "id": url, "url": url},
                },
            }
        )

    # -----------------------------------------------------------------------
    # Phase 1 — research plan
    # -----------------------------------------------------------------------
    async def _generate_plan(
        self,
        query: str,
        request: Any,
        user: Any,
        tools: ResearchTools,
        emitter: Optional[Callable],
    ) -> Tuple[str, List[str], Dict]:
        await self._emit_status(emitter, "🔍 Exploratory searches…")

        exploratory = [query]
        words = query.split()
        if len(words) > 3:
            exploratory.append(" ".join(words[:4]) + " overview")
        exploratory.append(query + " recent developments")

        # Run the exploratory searches concurrently — they're independent (the
        # MCP server paces its Brave requests itself).
        searched = await asyncio.gather(
            *[tools.search(eq) for eq in exploratory[:3]]
        )
        snippets: List[str] = []
        for hits in searched:
            for r in hits[:5]:
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
        tools: ResearchTools,
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
                return await tools.fetch(u)

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
            # Run this cycle's queries concurrently rather than one-after-another;
            # each is an independent search_web call. De-dup is applied serially
            # over the gathered results, in batch order, so the kept set doesn't
            # depend on which search finished first.
            await self._emit_status(
                emitter,
                f"🔍 Searching {len(batch)} queries: "
                + ", ".join(w["query"][:40] for w in batch),
            )
            searched = await asyncio.gather(
                *[
                    tools.search(w["query"], time_range=recency)
                    for w in batch
                ]
            )
            results: List[Tuple[Dict, Dict]] = []  # (work_item, search_result)
            for w, hits in zip(batch, searched):
                for r in hits:
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

            # --- Read + relevance-extract (#1) ---
            # The first MAX_PAGE_FETCHES_PER_CYCLE results are read in full with
            # fetch_page; the rest keep their search_web excerpts (Brave already
            # extracted the query-relevant passages), so no result is dropped.
            read_cap = max(0, self.valves.MAX_PAGE_FETCHES_PER_CYCLE)
            to_read = results[:read_cap]
            excerpt_only = len(results) - len(to_read)
            await self._emit_status(
                emitter,
                f"📄 Reading {len(to_read)} pages"
                + (f" (+{excerpt_only} from search excerpts)" if excerpt_only else "")
                + "…",
            )
            fetched = await asyncio.gather(
                *[fetch(r["url"]) for _, r in to_read],
                return_exceptions=True,
            )

            new = 0
            cycle_start = len(collected)  # index of this cycle's first new passage
            hop_pool: List[Tuple[Dict, List[Tuple[str, str]]]] = []
            valid: List[Tuple[Dict, Dict, Dict]] = []
            for i, (w, r) in enumerate(results):
                page = fetched[i] if i < len(fetched) else None
                if isinstance(page, BaseException) or not page:
                    page = None
                elif page.get("url"):
                    # fetch_page may land on a redirect target; don't re-fetch it.
                    seen_urls.add(_dedup_key(page["url"]))
                valid.append((w, r, self._source_page(r, page)))
            # Relevance-extract the whole batch concurrently — with embeddings on,
            # each extraction is a network round-trip, so overlapping them turns N
            # serial calls into one. The append (`_record`) is then done serially
            # in batch order, keeping `collected` deterministic.
            extracted = await asyncio.gather(
                *[
                    self._relevance(page.get("text", ""), w["query"], request, user)
                    for w, r, page in valid
                ]
            )
            for (w, r, page), (passages, score) in zip(valid, extracted):
                if self._record(
                    collected, source_urls, w, r, page, passages, score, depth=0
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
                    valid_hops = [
                        (w, href, page)
                        for (w, href), page in zip(chosen, hopped)
                        if not isinstance(page, Exception) and page
                    ]
                    extracted = await asyncio.gather(
                        *[
                            self._relevance(
                                page.get("text", ""), w["query"], request, user
                            )
                            for w, href, page in valid_hops
                        ]
                    )
                    for (w, href, page), (passages, score) in zip(
                        valid_hops, extracted
                    ):
                        link_r = {
                            "title": page.get("title") or "",
                            "url": page.get("url") or href,
                            "published_date": None,
                        }
                        if self._record(
                            collected, source_urls, w, link_r, page, passages, score,
                            depth=1,
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

        # Prefix-aware models use different instructions for queries and stored
        # content. Open WebUI 0.11 consistently applies these prefixes, so embed
        # the query and page segments separately rather than mixing both roles in
        # one batch with prefix=None.
        query_vectors, segment_vectors = await asyncio.gather(
            self._embed(
                [query],
                request,
                user,
                prefix=RAG_EMBEDDING_QUERY_PREFIX,
            ),
            self._embed(
                segs,
                request,
                user,
                prefix=RAG_EMBEDDING_CONTENT_PREFIX,
            ),
        )
        if (
            not query_vectors
            or len(query_vectors) != 1
            or not segment_vectors
            or len(segment_vectors) != len(segs)
        ):
            return None

        qv = query_vectors[0]
        scored = [
            (_cosine(qv, sv), i, seg)
            for i, (sv, seg) in enumerate(zip(segment_vectors, segs))
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
        self,
        texts: List[str],
        request: Any,
        user: Any,
        *,
        prefix: Optional[str] = None,
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
            # Open WebUI 0.11 replaced request.app.state.config with the async,
            # per-key Config model. Read the current RAG connection settings in
            # one query, matching Open WebUI's own embedding setup.
            cfg = await Config.get_many(
                "rag.embedding_engine",
                "rag.openai.api_base_url",
                "rag.openai.api_key",
                "rag.ollama.base_url",
                "rag.ollama.api_key",
                "rag.azure_openai.base_url",
                "rag.azure_openai.api_key",
                "rag.azure_openai.api_version",
            )
            engine = (cfg.get("rag.embedding_engine") or "").lower()
            if engine in ("openai", "ollama", "azure_openai"):
                from open_webui.retrieval.utils import generate_embeddings

                if engine == "openai":
                    url = cfg.get("rag.openai.api_base_url") or ""
                    key = cfg.get("rag.openai.api_key") or ""
                elif engine == "ollama":
                    url = cfg.get("rag.ollama.base_url") or ""
                    key = cfg.get("rag.ollama.api_key") or ""
                else:
                    url = cfg.get("rag.azure_openai.base_url") or ""
                    key = cfg.get("rag.azure_openai.api_key") or ""
                vectors = await generate_embeddings(
                    engine=engine,
                    model=self.valves.EMBEDDING_MODEL,
                    text=texts,
                    prefix=prefix,
                    url=url,
                    key=key,
                    user=user,
                    azure_api_version=cfg.get("rag.azure_openai.api_version"),
                )
            else:
                # The local engine uses Open WebUI's configured local embedding
                # model; EMBEDDING_MODEL acts as the opt-in switch in this case.
                ef = getattr(request.app.state, "EMBEDDING_FUNCTION", None)
                if ef is None:
                    return self._embed_fallback(None)
                vectors = await ef(texts, prefix=prefix, user=user)

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

    # A fetched page with fewer words than this is treated as unread (an empty
    # shell or a stub); the result's search excerpts are used instead.
    _MIN_PAGE_WORDS = 30

    @classmethod
    def _source_page(cls, result: Dict, page: Optional[Dict]) -> Dict[str, Any]:
        """Pick the text a search result contributes: the fetched page when it
        has real content, else the result's search_web excerpts (kind
        ``"excerpt"``). Keeps the page's links either way for multi-hop."""
        if page and len((page.get("text") or "").split()) >= cls._MIN_PAGE_WORDS:
            return page
        page = page or {}
        snippet = result.get("snippet") or ""
        return {
            "url": page.get("url") or result.get("url"),
            "title": page.get("title") or "",
            "text": snippet or page.get("text") or "",
            "links": page.get("links") or [],
            "kind": "excerpt" if snippet else page.get("kind", "failed"),
        }

    def _record(
        self,
        collected: List[Dict],
        source_urls: List[str],
        work_item: Dict,
        result: Dict,
        page: Dict,
        passages: str,
        score: float,
        *,
        depth: int,
    ) -> bool:
        """Append an already relevance-extracted page to `collected` with full
        attribution (#1, #2, #9). Returns True if kept.

        The async relevance extraction is done by the caller (concurrently across
        a batch); this is the ordered, synchronous append so `collected` stays
        deterministic regardless of which extraction finished first.
        """
        # Drop empty/blocked shells, but keep concise factual passages — the
        # content is already relevance-filtered, so a small floor suffices (the
        # old >20-word floor was tuned for whole-page text, not extracts).
        if not passages or len(passages.split()) < 8:
            return False
        url = page.get("url") or result.get("url")
        collected.append(
            {
                "url": url,
                "title": result.get("title") or page.get("title") or url,
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
    # MCP tool resolution
    # -----------------------------------------------------------------------
    async def _resolve_tools(
        self,
        registry: Any,
        request: Any,
        user: Any,
        metadata: Optional[dict],
        event_call: Optional[Callable],
    ) -> Tuple[Optional[ResearchTools], Any]:
        """Locate search_web + fetch_page for this run.

        Prefers the callables Open WebUI already resolved for the chat
        (``__tools__``, present when the MCP server is enabled on the chat or
        attached to the model). Otherwise connects directly through Open WebUI's
        configured MCP connections — honouring their auth, access control, and
        tool filters via ``connect_mcp_server`` — so the pipe works without the
        user toggling the tool on every chat.

        Returns ``(tools, owned_client)``; `owned_client` is non-None only when
        the pipe opened the connection itself and must disconnect it (in this
        same task) when the run ends. ``(None, None)`` when no server qualifies.
        """
        wanted = (self.valves.MCP_SERVER_ID or "").strip()
        settings = dict(
            timeout=float(max(0, self.valves.TOOL_CALL_TIMEOUT)),
            num_results=max(0, self.valves.SEARCH_RESULTS_PER_QUERY),
            default_time_range=(self.valves.SEARCH_TIME_RANGE or "").strip(),
        )

        found = _tools_from_registry(registry, wanted)
        if found:
            server_id, fns = found

            async def invoke_registry(tool: str, args: Dict[str, Any]) -> Any:
                return await fns[tool](**args)

            log.info("Using MCP server %r from the chat's tools", server_id)
            return ResearchTools(server_id, invoke_registry, **settings), None

        server_id, client = await self._connect_mcp(
            wanted, request, user, metadata, event_call
        )
        if client is None:
            return None, None

        async def invoke_client(tool: str, args: Dict[str, Any]) -> Any:
            return await client.call_tool(tool, function_args=args)

        log.info("Connected to MCP server %r directly", server_id)
        return ResearchTools(server_id, invoke_client, **settings), client

    async def _connect_mcp(
        self,
        wanted: str,
        request: Any,
        user: Any,
        metadata: Optional[dict],
        event_call: Optional[Callable],
    ) -> Tuple[str, Any]:
        """Open a connection to the MCP server that provides both tools.

        Tries `wanted` when set, else every enabled MCP tool-server connection
        in order, keeping the first whose (filtered) tool list has both
        search_web and fetch_page. Returns ``(server_id, client)`` or
        ``("", None)``. Runs sequentially in the caller's task: Open WebUI's MCP
        client must be closed in the task that opened it.
        """
        if request is None or user is None:
            return "", None
        try:
            from open_webui.utils.tools import connect_mcp_server
        except ImportError as e:
            log.warning("Open WebUI MCP support unavailable: %s", e)
            return "", None

        if wanted:
            server_ids = [wanted]
        else:
            try:
                connections = await Config.get("tool_server.connections", []) or []
            except Exception as e:
                log.warning("Could not read tool server connections: %s", e)
                connections = []
            server_ids = [
                (c.get("info") or {}).get("id")
                for c in connections
                if isinstance(c, dict)
                and c.get("type") == "mcp"
                and (c.get("config") or {}).get("enable")
            ]

        extra_params = {"__metadata__": metadata or {}, "__event_call__": event_call}
        for server_id in server_ids:
            if not server_id:
                continue
            try:
                result = await connect_mcp_server(request, server_id, user, extra_params)
            except Exception as e:
                log.warning("Could not connect to MCP server %r: %s", server_id, e)
                continue
            if result is None:
                continue
            client, specs = result
            names = {s.get("name") for s in specs or [] if isinstance(s, dict)}
            if all(t in names for t in _REQUIRED_TOOLS):
                return server_id, client
            await client.disconnect()
        return "", None

    # -----------------------------------------------------------------------
    # Follow-up questions about a finished report
    # -----------------------------------------------------------------------
    async def _answer_followup(
        self, messages: List[Dict], request: Any, user: Any
    ) -> str:
        """Answer a message sent after a report was delivered in this chat.

        The conversation (report included, done-markers stripped) goes to
        RESEARCH_MODEL with an instruction to answer from the report's sources,
        rather than starting a second research run or returning an empty reply.
        """
        convo: List[Dict] = []
        for m in messages:
            content = m.get("content")
            if m.get("role") == "assistant" and isinstance(content, str):
                m = {**m, "content": content.replace(_DONE_MARKER, "")}
            convo.append(m)
        system = {
            "role": "system",
            "content": (
                "Earlier in this conversation you delivered a deep-research report "
                "with numbered [n] sources. Answer the user's follow-up using that "
                "report and its sources, citing [n] where relevant. If the answer "
                "isn't supported by the report, say so plainly and suggest starting "
                "a new Deep Research chat for that question."
            ),
        }
        return await self._llm_call(
            [system, *convo], request, user, temperature=0.3, max_tokens=4096
        )

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
        __tools__: Optional[dict] = None,
    ) -> str:
        """
        Orchestrate the deep-research workflow.

        Returns the final report directly, as required by Open WebUI's pipe
        response lifecycle. Replace events are used only for live progress.
        """
        # ==============================================================
        # TASK GUARD: Open WebUI invokes the selected model for background
        # tasks like title_generation, tags_generation, follow_up_generation
        # and autocomplete_generation. These must NOT trigger a full
        # research run. Instead, forward the request to the underlying
        # model for a quick answer.
        # ==============================================================
        if __task__:
            log.info(f"Task call received: {__task__} — forwarding to model")
            task_model = (self.valves.RESEARCH_MODEL or "").strip()
            task_messages = body.get("messages", [])
            if not task_model or task_model == body.get("model"):
                # A pipe has no implicit "default" model ID in Open WebUI.
                # Returning a stable label is preferable to recursively invoking
                # this pipe or making a request for a nonexistent model.
                return "Deep Research"
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

        research_model = (self.valves.RESEARCH_MODEL or "").strip()
        if not research_model:
            return (
                "Deep Research is not configured: set the RESEARCH_MODEL valve "
                "to an accessible Open WebUI model ID."
            )
        if research_model == body.get("model"):
            return (
                "Deep Research is misconfigured: RESEARCH_MODEL cannot be the "
                "Deep Research pipe itself."
            )

        user_obj = None
        if __user__:
            user_obj = await Users.get_user_by_id(__user__["id"])

        # ==============================================================
        # FOLLOW-UP: the conversation already has a completed report, so
        # answer the new message about it instead of researching again.
        # ==============================================================
        all_messages = body.get("messages", [])
        if any(
            msg.get("role") == "assistant"
            and isinstance(msg.get("content"), str)
            and _DONE_MARKER in msg["content"]
            for msg in all_messages
        ):
            log.info("Report already delivered in this chat; answering follow-up")
            try:
                return await self._answer_followup(all_messages, __request__, user_obj)
            except Exception as e:
                log.error(f"Follow-up answer failed: {e}")
                return f"Follow-up failed: {e}"

        # Open WebUI 0.12 records the user's prompt (before file/knowledge
        # context is injected into the messages) as `base_user_prompt`. Prefer
        # it so an attached file's RAG wrapper doesn't become the research
        # query.
        meta = __metadata__ or {}
        user_query = str(
            meta.get("base_user_prompt") or meta.get("user_prompt") or ""
        ).strip()
        if not user_query:
            _, messages = pop_system_message(all_messages)
            user_query = (get_last_user_message(messages) or "").strip()
        if not user_query:
            return "Please provide a research topic or question."

        tools, owned_client = await self._resolve_tools(
            __tools__, __request__, user_obj, __metadata__, __event_call__
        )
        if tools is None:
            wanted = (self.valves.MCP_SERVER_ID or "").strip()
            return (
                "Deep Research needs the MCP server's search_web and fetch_page "
                "tools, but "
                + (
                    f"the MCP server {wanted!r} (MCP_SERVER_ID) is unavailable or "
                    "doesn't expose both."
                    if wanted
                    else "no enabled MCP tool server exposing both was found."
                )
                + " Add it under Admin Settings → External Tools (type MCP, "
                "Streamable HTTP), or set MCP_SERVER_ID in this function's valves."
            )

        try:
            # ======================================================
            # PHASE 1 — plan
            # ======================================================
            await self._emit_status(__event_emitter__, "🚀 Starting deep research…")
            plan_text, _, plan = await self._generate_plan(
                user_query,
                __request__,
                user_obj,
                tools,
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
                try:
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
                except Exception as exc:
                    # Continue with the generated plan rather than discarding an
                    # otherwise valid research run.
                    log.warning("Plan confirmation unavailable: %s", exc)
                    confirmation = None

                # The dialog's Cancel button answers False: stop here.
                if confirmation is False:
                    await self._emit_status(
                        __event_emitter__, "🛑 Research cancelled", done=True
                    )
                    return "Research cancelled at the plan review step."

                resp = ""
                if isinstance(confirmation, dict):
                    # Timeouts and closed tabs come back as {"error": ...};
                    # proceed with the generated plan.
                    if confirmation.get("error"):
                        log.warning(
                            "Plan confirmation interrupted: %s",
                            confirmation["error"],
                        )
                    else:
                        resp = str(
                            confirmation.get("value", "")
                        ).strip().lower()
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
                tools,
                __event_emitter__,
            )

            if not collected:
                msg = (
                    "Unable to gather sufficient research data. Check that the "
                    f"MCP server {tools.server_id!r} can search, or refine the "
                    "query."
                )
                if tools.last_error:
                    msg += f"\n\nLast tool error: {tools.last_error}"
                await self._emit_replace(__event_emitter__, "")
                await self._emit_status(
                    __event_emitter__,
                    "⚠️ No data collected",
                    done=True,
                )
                return msg

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

            # Append the done-marker so later messages in this chat are
            # answered as follow-ups instead of re-running research.
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

            return report_with_marker

        except Exception as e:
            log.error(f"Deep Research error: {e}", exc_info=True)
            await self._emit_status(
                __event_emitter__,
                f"❌ Failed: {e}",
                done=True,
            )
            return f"Research error: {e}"
        finally:
            # A connection the pipe opened itself is closed here, in the task
            # that opened it (MCP sessions can't be closed from another task).
            if owned_client is not None:
                await owned_client.disconnect()
