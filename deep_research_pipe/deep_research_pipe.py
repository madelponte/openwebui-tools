"""
title: Deep Research
author: mdelponte
version: 1.5.0
license: MIT
description: >
    A deep research pipe that takes a user query, generates a research plan
    using an LLM, presents it for user confirmation, then iteratively
    searches the web via SearXNG, fetches and extracts page content, and
    synthesizes a final structured report.  Fetching mirrors the companion
    fetch_page MCP tool's resilient ladder: Apache Tika for PDF/Office/
    OpenDocument/RTF/EPUB documents, bot-wall/CAPTCHA/429 detection that
    re-renders through FlareSolverr, a Wayback Machine fallback for pages
    that stay blocked or render empty, and YouTube transcript extraction.
required_open_webui_version: 0.9.0
"""

import asyncio
import json
import logging
import re
import time
from urllib.parse import urlparse, urlunparse, parse_qs, parse_qsl, urlencode
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

        # --- Apache Tika (PDF extraction) ---
        TIKA_URL: str = Field(
            default="http://tika:9998",
            description="Base URL of your Apache Tika server used for PDF text extraction.",
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
                "Max tokens the LLM may generate for the final report. "
                "Increase if the report is being truncated."
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

    def _trim_words(self, text: str) -> str:
        """Cap a snippet to SNIPPET_MAX_WORDS words to protect the LLM context."""
        words = text.split()
        if len(words) > self.valves.SNIPPET_MAX_WORDS:
            return " ".join(words[: self.valves.SNIPPET_MAX_WORDS]) + " [...]"
        return text

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
    ) -> Tuple[str, str]:
        """Fetch one URL's readable text through a resilient fallback ladder.

        Returns ``(url, text)``; ``text`` is empty when nothing usable could be
        recovered (the research loop skips snippets under ~20 words). The ladder:
        Reddit JSON → YouTube transcript → direct GET (document→Tika), and on a
        bot wall / 429 / empty JS shell, FlareSolverr and then the Wayback
        Machine. A challenge page we can't get past is returned as empty rather
        than as if it were content.
        """
        # Reddit serves anti-bot HTML; route through its public .json endpoint.
        if self._is_reddit_url(url):
            text = await self._fetch_reddit(session, url)
            return url, self._trim_words(text)

        # A YouTube video's real content is its transcript, not the watch page.
        if _YT_AVAILABLE and self._is_youtube_video_url(url):
            transcript = await self._fetch_youtube_transcript(url)
            if transcript:
                return url, self._trim_words(transcript)
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
            return url, self._trim_words(extracted)

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
                    return url, self._trim_words(fs_text)
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
                return url, note + self._trim_words(wb_text)

        # Don't surface an unbypassed challenge page as if it were content.
        if blocked:
            return url, ""
        return url, self._trim_words(rendered_text)

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
        for q in plan.get("initial_queries", [query]) + [
            sq
            for sec in plan.get("sections", [])
            for sq in sec.get("search_queries", [])
        ]:
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
                await self._emit_status(emitter, "🧠 Generating follow-up queries…")
                cqs = await self._followup_queries(query, collected, request, user)
            elif not cqs:
                break

            # Search. De-dup on a normalized key so the same article reached via
            # http/https, www., trailing-slash, or tracking-param variants is
            # only fetched once.
            results: List[Dict[str, str]] = []
            for cq in cqs:
                await self._emit_status(emitter, f"🔍 Searching: {cq[:80]}…")
                for r in await self._search(session, cq):
                    if not r["url"]:
                        continue
                    key = _dedup_key(r["url"])
                    if key not in seen_urls:
                        results.append(r)
                        seen_urls.add(key)

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
                    collected.append({"url": url, "title": title, "content": text})
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
                await self._emit_status(emitter, "📦 Compressing older notes…")
                collected = await self._compress(query, collected, request, user)

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

            # Optional politeness pause before the next cycle's searches/fetches.
            if (
                self.valves.CYCLE_DELAY_SECONDS > 0
                and cycle < self.valves.MAX_RESEARCH_CYCLES
            ):
                await asyncio.sleep(self.valves.CYCLE_DELAY_SECONDS)

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
            request,
            user,
            temperature=0.5,
        )
        qs = _extract_json(raw)
        if isinstance(qs, list):
            return [str(q) for q in qs[: self.valves.QUERIES_PER_CYCLE]]
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
            request,
            user,
            temperature=0.2,
            max_tokens=100,
        )
        return r.strip().upper().startswith("CONTINUE")

    async def _compress(
        self, query: str, collected: List[Dict], request: Any, user: Any
    ) -> List[Dict[str, str]]:
        sp = len(collected) // 2
        old, recent = collected[:sp], collected[sp:]
        old_text = "\n\n".join(
            f"Source: {s['title']} ({s['url']})\n{s['content']}" for s in old
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
            request,
            user,
            temperature=0.2,
            max_tokens=3000,
        )
        # Preserve the real URLs the summary folds together so the report's
        # citation list can still name them (the per-source content is gone, but
        # the attribution shouldn't be).
        covered = [
            s["url"]
            for s in old
            if s.get("url") and s["url"] != "compressed_summary"
        ]
        return [
            {
                "url": "compressed_summary",
                "title": f"Summary of {len(old)} earlier sources",
                "content": summary,
                "source_urls": covered,
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

        # Build the source blocks and the [n] citation list from ONE numbering so
        # "SOURCE n" in the data, "[n]" in the text, and the reference list always
        # agree. Sources are added until the character budget is hit; any past it
        # are dropped from BOTH the data and the references, so the model can
        # never cite a source it wasn't actually shown. (`source_urls` — the full
        # running list — is still used for the progress snapshot, not here.)
        source_cap = self.valves.REPORT_SOURCE_MAX_CHARS
        ctx_budget = self.valves.REPORT_CONTEXT_MAX_CHARS

        blocks: List[str] = []
        ref_lines: List[str] = []
        used_chars = 0
        n = 0
        for s in collected:
            content = s.get("content") or ""
            if source_cap > 0:
                content = content[:source_cap]
            idx = n + 1
            block = (
                f"\n--- SOURCE {idx}: {s.get('title') or s.get('url')} "
                f"({s.get('url')}) ---\n{content}\n"
            )
            # Stop once the budget is exhausted, but always keep at least one.
            if ctx_budget > 0 and n > 0 and used_chars + len(block) > ctx_budget:
                break
            blocks.append(block)
            used_chars += len(block)
            n = idx
            if s.get("url") == "compressed_summary":
                covered = s.get("source_urls") or []
                if covered:
                    ref_lines.append(
                        f"[{idx}] Summary of earlier sources: " + "; ".join(covered)
                    )
                else:
                    ref_lines.append(f"[{idx}] Summary of earlier sources")
            else:
                ref_lines.append(f"[{idx}] {s.get('url')}")

        ctx = "".join(blocks)
        refs = "\n".join(ref_lines)
        dropped = len(collected) - n
        if dropped > 0:
            log.info(
                "Report context budget reached: included %d of %d sources",
                n,
                len(collected),
            )

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
- Each section MUST be {smin}-{smax} words. Do not write short paragraphs — develop each section thoroughly with analysis, examples, and data from the sources.
- Write in a clear, professional, analytical tone.
- Base ALL claims on the provided research data and cite with [n].
- Include specific data, statistics, and findings from sources.
- Cover ALL planned sections — do not skip or merge sections.
- If data is thin for a section, note the gap but still write what you can.
- Write the COMPLETE report — do not truncate, summarise prematurely, or say "continued below"."""

        return await self._llm_call(
            [{"role": "user", "content": prompt}],
            request,
            user,
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