"""
title: Agentic Web Search
author: mdelponte
author_url: https://github.com/mdelponte
version: 1.0.0
license: MIT
description: Lets the model decide when it needs to search the web, craft its own query, fetch pages, and use structured data. Uses a self-hosted SearXNG instance with optional FlareSolverr fallback for Cloudflare-protected pages. Includes Reddit JSON endpoint handling and PDF support.
requirements: httpx, beautifulsoup4, lxml, pypdf
"""

import asyncio
import io
import json
import re
from typing import Any, Awaitable, Callable, Optional
from urllib.parse import urlparse, urlunparse

import httpx
from bs4 import BeautifulSoup
from pydantic import BaseModel, Field


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

DEFAULT_UA = (
    "Mozilla/5.0 (X11; Linux x86_64) AppleWebKit/537.36 "
    "(KHTML, like Gecko) Chrome/124.0.0.0 Safari/537.36"
)

# Status codes / markers that indicate Cloudflare / similar protection
CLOUDFLARE_STATUS_CODES = {403, 503, 520, 521, 522, 523, 524, 525, 526, 527}
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


def _is_cloudflare_block(status: int, text: str, headers: dict) -> bool:
    """Best-effort detection that a response is a Cloudflare/CAPTCHA wall."""
    hdr_lower = {k.lower(): str(v).lower() for k, v in (headers or {}).items()}
    server = hdr_lower.get("server", "")
    if "cloudflare" in server and status in CLOUDFLARE_STATUS_CODES:
        return True
    if status in CLOUDFLARE_STATUS_CODES:
        t = (text or "")[:8000].lower()
        if any(m in t for m in CLOUDFLARE_MARKERS):
            return True
    # Sometimes Cloudflare returns 200 with an interstitial
    if status == 200 and text:
        t = text[:4000].lower()
        hits = sum(1 for m in CLOUDFLARE_MARKERS if m in t)
        if hits >= 2:
            return True
    return False


def _normalize_reddit_url(url: str) -> str:
    """
    Reddit tends to block scraping on the HTML pages.
    Force the .json endpoint for reddit links.
    """
    try:
        p = urlparse(url)
    except Exception:
        return url
    host = (p.netloc or "").lower()
    if not host.endswith("reddit.com"):
        return url
    # Normalize host: old.reddit.com, www.reddit.com, np.reddit.com -> www.reddit.com
    host = "www.reddit.com"
    path = p.path or "/"
    # Strip trailing slash to avoid //.json
    if path.endswith("/"):
        path = path[:-1]
    if not path.endswith(".json"):
        path = path + ".json"
    return urlunparse((p.scheme or "https", host, path, "", p.query, ""))


def _trim(text: str, limit: int) -> str:
    if limit <= 0 or len(text) <= limit:
        return text
    return text[:limit].rstrip() + f"\n\n[... truncated at {limit} chars ...]"


# ---------------------------------------------------------------------------
# Content extraction
# ---------------------------------------------------------------------------

def _extract_jsonld(soup: BeautifulSoup) -> list:
    """Pull JSON-LD structured data blocks from <script type=application/ld+json>."""
    out = []
    for tag in soup.find_all("script", attrs={"type": "application/ld+json"}):
        raw = tag.string or tag.get_text() or ""
        raw = raw.strip()
        if not raw:
            continue
        try:
            parsed = json.loads(raw)
        except Exception:
            # Sometimes pages concatenate multiple JSON blobs. Try a lenient fallback.
            try:
                # Replace unescaped newlines inside strings is too risky; just skip.
                continue
            except Exception:
                continue
        if isinstance(parsed, list):
            out.extend(parsed)
        else:
            out.append(parsed)
    return out


def _headings_outline(soup: BeautifulSoup, max_items: int = 40) -> list[dict]:
    """Build a lightweight 'table of contents' from heading tags."""
    outline = []
    for h in soup.find_all(["h1", "h2", "h3", "h4"]):
        text = " ".join(h.get_text(" ", strip=True).split())
        if not text:
            continue
        outline.append({"level": int(h.name[1]), "text": text})
        if len(outline) >= max_items:
            break
    return outline


def _toc_from_jsonld(jsonld: list) -> Optional[list[str]]:
    """
    Some sites (how-tos, recipes, articles) expose explicit structure in JSON-LD.
    Extract useful 'table of contents'-like info when possible.
    """
    toc: list[str] = []

    def walk(obj):
        if isinstance(obj, dict):
            t = obj.get("@type")
            # Recipe steps
            if t in ("Recipe", "HowTo") or (isinstance(t, list) and ("Recipe" in t or "HowTo" in t)):
                steps = obj.get("recipeInstructions") or obj.get("step") or []
                if isinstance(steps, list):
                    for s in steps:
                        if isinstance(s, str):
                            toc.append(s.strip())
                        elif isinstance(s, dict):
                            name = s.get("name") or s.get("text") or ""
                            if name:
                                toc.append(str(name).strip())
            # Article headline + sections
            if isinstance(t, str) and t.endswith("Article"):
                hl = obj.get("headline")
                if hl and hl not in toc:
                    toc.append(str(hl).strip())
            for v in obj.values():
                walk(v)
        elif isinstance(obj, list):
            for v in obj:
                walk(v)

    walk(jsonld)
    return toc or None


def _page_title(soup: BeautifulSoup) -> Optional[str]:
    if soup.title and soup.title.string:
        return soup.title.string.strip()
    og = soup.find("meta", attrs={"property": "og:title"})
    if og and og.get("content"):
        return og["content"].strip()
    return None


def _page_description(soup: BeautifulSoup) -> Optional[str]:
    for sel in [
        ("meta", {"name": "description"}),
        ("meta", {"property": "og:description"}),
        ("meta", {"name": "twitter:description"}),
    ]:
        tag = soup.find(*sel)
        if tag and tag.get("content"):
            return tag["content"].strip()
    return None


def _plain_text_from_html(html: str) -> str:
    """Strip scripts/styles/nav and return readable text."""
    soup = BeautifulSoup(html, "lxml")
    for t in soup(["script", "style", "noscript", "template", "iframe", "svg"]):
        t.decompose()
    # Prefer <article> / <main> if present (better signal, less chrome)
    root = soup.find("article") or soup.find("main") or soup.body or soup
    text = root.get_text("\n", strip=True)
    # Collapse excessive blank lines
    text = re.sub(r"\n{3,}", "\n\n", text)
    return text


def _structured_from_html(html: str, url: str) -> dict:
    """Return a structured representation of the page."""
    soup = BeautifulSoup(html, "lxml")
    jsonld = _extract_jsonld(soup)
    result = {
        "url": url,
        "title": _page_title(soup),
        "description": _page_description(soup),
        "headings": _headings_outline(soup),
        "jsonld": jsonld if jsonld else None,
        "toc": _toc_from_jsonld(jsonld),
    }
    return result


def _pdf_to_text(data: bytes) -> str:
    """Extract text from a PDF byte stream using pypdf."""
    try:
        from pypdf import PdfReader  # lazy import; declared in requirements
    except Exception as e:
        return f"[PDF extraction failed: pypdf not available: {e}]"
    try:
        reader = PdfReader(io.BytesIO(data))
        parts = []
        for i, page in enumerate(reader.pages):
            try:
                t = page.extract_text() or ""
            except Exception as pe:
                t = f"[Page {i + 1} extraction error: {pe}]"
            if t.strip():
                parts.append(f"--- Page {i + 1} ---\n{t.strip()}")
        return "\n\n".join(parts) if parts else "[PDF contained no extractable text]"
    except Exception as e:
        return f"[Failed to parse PDF: {e}]"


# ---------------------------------------------------------------------------
# Fetching (with FlareSolverr fallback)
# ---------------------------------------------------------------------------

async def _emit_status(emitter, description: str, done: bool = False):
    if emitter:
        try:
            await emitter(
                {"type": "status", "data": {"description": description, "done": done}}
            )
        except Exception:
            pass


async def _httpx_fetch(
    url: str,
    timeout: float,
    user_agent: str,
    verify_ssl: bool,
) -> tuple[int, dict, bytes, str]:
    """
    Direct fetch via httpx. Returns (status, headers, body_bytes, content_type).
    """
    headers = {
        "User-Agent": user_agent,
        "Accept": (
            "text/html,application/xhtml+xml,application/xml;q=0.9,"
            "application/json;q=0.9,application/pdf;q=0.9,*/*;q=0.8"
        ),
        "Accept-Language": "en-US,en;q=0.9",
    }
    async with httpx.AsyncClient(
        follow_redirects=True,
        timeout=timeout,
        verify=verify_ssl,
        headers=headers,
    ) as client:
        resp = await client.get(url)
        ctype = resp.headers.get("content-type", "")
        return resp.status_code, dict(resp.headers), resp.content, ctype


async def _flaresolverr_fetch(
    url: str,
    flaresolverr_url: str,
    max_timeout_ms: int,
    http_timeout: float,
) -> tuple[int, dict, str]:
    """
    Use FlareSolverr to fetch a Cloudflare-protected page.
    Returns (status, headers, html_text).
    Raises httpx.HTTPError on transport failure.
    """
    endpoint = flaresolverr_url.rstrip("/") + "/v1"
    payload = {
        "cmd": "request.get",
        "url": url,
        "maxTimeout": max_timeout_ms,
    }
    async with httpx.AsyncClient(timeout=http_timeout) as client:
        resp = await client.post(
            endpoint,
            json=payload,
            headers={"Content-Type": "application/json"},
        )
        resp.raise_for_status()
        data = resp.json()
    if data.get("status") != "ok":
        msg = data.get("message", "unknown FlareSolverr error")
        raise RuntimeError(f"FlareSolverr failed: {msg}")
    sol = data.get("solution") or {}
    status = int(sol.get("status") or 0)
    hdrs = sol.get("headers") or {}
    body = sol.get("response") or ""
    return status, hdrs, body


async def _resilient_fetch(
    url: str,
    *,
    timeout: float,
    user_agent: str,
    verify_ssl: bool,
    flaresolverr_url: Optional[str],
    flaresolverr_timeout_ms: int,
    emitter=None,
) -> dict:
    """
    Try direct httpx. If response looks like a Cloudflare wall and
    FlareSolverr is configured, retry through it.

    Returns:
        {
          "url": final_url,
          "status": int,
          "content_type": str,
          "text": str | None,   # for text-like responses
          "bytes": bytes | None, # for binary (e.g. PDFs)
          "via": "direct" | "flaresolverr",
          "blocked_detected": bool,
        }
    """
    # httpx first
    try:
        status, headers, body, ctype = await _httpx_fetch(
            url, timeout=timeout, user_agent=user_agent, verify_ssl=verify_ssl
        )
    except Exception as e:
        # Direct fetch failed entirely; try FlareSolverr if available
        if flaresolverr_url:
            await _emit_status(emitter, f"Direct fetch failed ({e}); trying FlareSolverr...")
            try:
                fs_status, fs_headers, fs_html = await _flaresolverr_fetch(
                    url,
                    flaresolverr_url=flaresolverr_url,
                    max_timeout_ms=flaresolverr_timeout_ms,
                    http_timeout=max(timeout, flaresolverr_timeout_ms / 1000 + 10),
                )
                return {
                    "url": url,
                    "status": fs_status,
                    "content_type": "text/html",
                    "text": fs_html,
                    "bytes": None,
                    "via": "flaresolverr",
                    "blocked_detected": True,
                }
            except Exception as fe:
                raise RuntimeError(f"Both direct and FlareSolverr fetches failed: {e!r} / {fe!r}")
        raise

    # Binary content (PDF, images, etc.) — don't try Cloudflare detection on bytes
    is_textlike = (
        ctype.startswith("text/")
        or "json" in ctype
        or "xml" in ctype
        or "html" in ctype
    )

    if not is_textlike:
        return {
            "url": url,
            "status": status,
            "content_type": ctype,
            "text": None,
            "bytes": body,
            "via": "direct",
            "blocked_detected": False,
        }

    # Decode text response
    try:
        text = body.decode("utf-8", errors="replace")
    except Exception:
        text = body.decode("latin-1", errors="replace")

    blocked = _is_cloudflare_block(status, text, headers)
    if blocked and flaresolverr_url:
        await _emit_status(
            emitter, f"Cloudflare challenge detected on {url}; retrying via FlareSolverr..."
        )
        try:
            fs_status, fs_headers, fs_html = await _flaresolverr_fetch(
                url,
                flaresolverr_url=flaresolverr_url,
                max_timeout_ms=flaresolverr_timeout_ms,
                http_timeout=max(timeout, flaresolverr_timeout_ms / 1000 + 10),
            )
            return {
                "url": url,
                "status": fs_status,
                "content_type": "text/html",
                "text": fs_html,
                "bytes": None,
                "via": "flaresolverr",
                "blocked_detected": True,
            }
        except Exception as fe:
            # Return the original blocked response so the caller can decide what to do
            return {
                "url": url,
                "status": status,
                "content_type": ctype,
                "text": text,
                "bytes": None,
                "via": "direct",
                "blocked_detected": True,
                "flaresolverr_error": str(fe),
            }

    return {
        "url": url,
        "status": status,
        "content_type": ctype,
        "text": text,
        "bytes": None,
        "via": "direct",
        "blocked_detected": blocked,
    }


# ---------------------------------------------------------------------------
# SearXNG query
# ---------------------------------------------------------------------------

async def _searxng_query(
    base_url: str,
    query: str,
    *,
    num_results: int,
    categories: str,
    language: str,
    time_range: str,
    safe_search: int,
    timeout: float,
    verify_ssl: bool,
    user_agent: str,
) -> list[dict]:
    """Run a SearXNG JSON query and return [{url, title, snippet, engine}]."""
    params = {"q": query, "format": "json", "safesearch": str(safe_search)}
    if categories:
        params["categories"] = categories
    if language:
        params["language"] = language
    if time_range:
        params["time_range"] = time_range

    url = base_url.rstrip("/") + "/search"
    headers = {
        "User-Agent": user_agent,
        "Accept": "application/json",
    }
    async with httpx.AsyncClient(
        timeout=timeout, verify=verify_ssl, headers=headers
    ) as client:
        resp = await client.get(url, params=params)
        if resp.status_code == 403:
            raise RuntimeError(
                "SearXNG returned 403. Make sure `search.formats` in its settings.yml "
                "includes `json`."
            )
        resp.raise_for_status()
        data = resp.json()

    items = data.get("results") or []
    out: list[dict] = []
    for r in items[:num_results]:
        out.append(
            {
                "url": r.get("url"),
                "title": r.get("title"),
                "snippet": (r.get("content") or "").strip(),
                "engine": r.get("engine"),
            }
        )
    return out


# ---------------------------------------------------------------------------
# The Tool class
# ---------------------------------------------------------------------------

class Tools:
    class Valves(BaseModel):
        # SearXNG
        SEARXNG_URL: str = Field(
            default="http://searxng:8080",
            description="Base URL of your SearXNG instance (no trailing /search). "
                        "Example: http://searxng:8080 or https://search.example.com",
        )
        NUM_RESULTS: int = Field(
            default=5,
            description="Number of search results the search_web tool returns.",
        )
        ENRICH_TOP_N: int = Field(
            default=3,
            description="For this many top results, also fetch the page and return its "
                        "structured data (title, description, heading outline, JSON-LD "
                        "table-of-contents when available). Set 0 to disable enrichment.",
        )
        SEARXNG_CATEGORIES: str = Field(
            default="general",
            description="Comma-separated SearXNG categories (e.g. 'general', 'news', "
                        "'general,news', 'it').",
        )
        SEARXNG_LANGUAGE: str = Field(
            default="en",
            description="SearXNG language code (e.g. 'en', 'de', 'all').",
        )
        SEARXNG_TIME_RANGE: str = Field(
            default="",
            description="Time range filter: '', 'day', 'week', 'month', or 'year'.",
        )
        SEARXNG_SAFESEARCH: int = Field(
            default=0,
            description="SearXNG safesearch: 0=off, 1=moderate, 2=strict.",
        )

        # FlareSolverr
        FLARESOLVERR_URL: str = Field(
            default="http://flaresolverr:8191",
            description="Base URL of FlareSolverr (no trailing /v1). Leave blank to disable fallback.",
        )
        FLARESOLVERR_TIMEOUT_MS: int = Field(
            default=60000,
            description="maxTimeout passed to FlareSolverr, in milliseconds.",
        )

        # HTTP
        HTTP_TIMEOUT_SECONDS: float = Field(
            default=25.0,
            description="HTTP timeout for direct page/search fetches, in seconds.",
        )
        VERIFY_SSL: bool = Field(
            default=True,
            description="Verify TLS certificates. Disable only for local instances with self-signed certs.",
        )
        USER_AGENT: str = Field(
            default=DEFAULT_UA,
            description="User-Agent sent with direct fetches.",
        )

        # Output sizing
        MAX_PAGE_CHARS: int = Field(
            default=25000,
            description="Max characters of page content returned by fetch_page before truncation.",
        )
        MAX_ENRICH_HEADINGS: int = Field(
            default=25,
            description="Max headings per enriched search result.",
        )
        MAX_SNIPPET_CHARS: int = Field(
            default=400,
            description="Max characters of each result's SearXNG snippet.",
        )

        # Misc
        EMIT_CITATIONS: bool = Field(
            default=True,
            description="Emit citation events for pages fetched via fetch_page so they show up as chat sources.",
        )
        LOG_REQUESTS: bool = Field(
            default=False,
            description="Print debug info to the Open WebUI logs.",
        )

    def __init__(self):
        self.valves = self.Valves()
        # We manage citations ourselves via event emitter
        self.citation = False

    # ------------------------------------------------------------------
    # search_web
    # ------------------------------------------------------------------
    async def search_web(
        self,
        query: str,
        __event_emitter__: Optional[Callable[[dict], Awaitable[None]]] = None,
    ) -> str:
        """
        Search the web via SearXNG and return a ranked list of results.

        Use this when you don't already know the answer, the question concerns
        current events, or you need to verify a fact. Craft a focused query
        (a few keywords) — do NOT just echo the user's whole prompt. If the
        first search isn't useful, you may call this again with a refined query.

        Each result includes: url, title, snippet, and (for the top results)
        page metadata such as a description and a heading-based outline /
        JSON-LD table of contents, so you can decide which links are worth
        fetching in full.

        :param query: A concise search query (keywords, not a full sentence).
        :return: JSON string of results.
        """
        v = self.valves
        query = (query or "").strip()
        if not query:
            return json.dumps({"error": "Empty query"})

        await _emit_status(__event_emitter__, f"Searching: {query}")

        try:
            results = await _searxng_query(
                base_url=v.SEARXNG_URL,
                query=query,
                num_results=max(1, v.NUM_RESULTS),
                categories=v.SEARXNG_CATEGORIES,
                language=v.SEARXNG_LANGUAGE,
                time_range=v.SEARXNG_TIME_RANGE,
                safe_search=v.SEARXNG_SAFESEARCH,
                timeout=v.HTTP_TIMEOUT_SECONDS,
                verify_ssl=v.VERIFY_SSL,
                user_agent=v.USER_AGENT,
            )
        except Exception as e:
            await _emit_status(
                __event_emitter__, f"Search failed: {e}", done=True
            )
            return json.dumps({"error": f"SearXNG query failed: {e}", "query": query})

        if not results:
            await _emit_status(
                __event_emitter__, "No results.", done=True
            )
            return json.dumps({"query": query, "results": []})

        # Trim snippets
        for r in results:
            if r.get("snippet"):
                r["snippet"] = _trim(r["snippet"], v.MAX_SNIPPET_CHARS)

        # Enrich top N by fetching their structured metadata in parallel
        enrich_n = min(max(0, v.ENRICH_TOP_N), len(results))
        if enrich_n > 0:
            await _emit_status(
                __event_emitter__,
                f"Fetching metadata for top {enrich_n} result(s)...",
            )
            tasks = [
                self._enrich_result(r.get("url"))
                for r in results[:enrich_n]
            ]
            enriched = await asyncio.gather(*tasks, return_exceptions=True)
            for i, data in enumerate(enriched):
                if isinstance(data, Exception):
                    results[i]["page_meta_error"] = str(data)
                    continue
                if not data:
                    continue
                # Attach compact metadata — drop the huge jsonld blob, but keep TOC-style info
                headings = (data.get("headings") or [])[: v.MAX_ENRICH_HEADINGS]
                results[i]["page_title"] = data.get("title")
                results[i]["page_description"] = data.get("description")
                if headings:
                    results[i]["page_headings"] = headings
                if data.get("toc"):
                    results[i]["page_toc"] = data["toc"][:20]

        await _emit_status(
            __event_emitter__,
            f"Got {len(results)} result(s).",
            done=True,
        )

        return json.dumps(
            {"query": query, "results": results},
            ensure_ascii=False,
            indent=2,
        )

    async def _enrich_result(self, url: Optional[str]) -> Optional[dict]:
        """Fetch a URL just enough to extract structured metadata."""
        if not url:
            return None
        v = self.valves
        try:
            fetched = await _resilient_fetch(
                url,
                timeout=v.HTTP_TIMEOUT_SECONDS,
                user_agent=v.USER_AGENT,
                verify_ssl=v.VERIFY_SSL,
                flaresolverr_url=v.FLARESOLVERR_URL or None,
                flaresolverr_timeout_ms=v.FLARESOLVERR_TIMEOUT_MS,
                emitter=None,  # silent during bulk enrichment
            )
        except Exception as e:
            return {"error": str(e)}

        ctype = (fetched.get("content_type") or "").lower()
        if "pdf" in ctype:
            # No structured data for PDFs; return minimal info
            return {"title": None, "description": None, "headings": [], "toc": None}
        text = fetched.get("text")
        if not text:
            return None
        return _structured_from_html(text, url)

    # ------------------------------------------------------------------
    # fetch_page
    # ------------------------------------------------------------------
    async def fetch_page(
        self,
        url: str,
        mode: str = "text",
        __event_emitter__: Optional[Callable[[dict], Awaitable[None]]] = None,
    ) -> str:
        """
        Fetch the contents of a web page (or a URL returned by search_web).

        Choose the mode that fits your need:
        - "text":       plain readable text of the page. Best for reading an
                        article or extracting facts. Also used automatically for PDFs.
        - "structured": metadata only — title, description, heading outline,
                        and JSON-LD structured data (schema.org Recipe, HowTo,
                        Article, etc.). Best for understanding what's on a page
                        without downloading the full body.

        If the page is blocked by Cloudflare / a CAPTCHA interstitial and a
        FlareSolverr instance is configured, it will be retried through that
        automatically. Reddit links are automatically redirected to the .json
        endpoint for reliability.

        :param url: Absolute URL to fetch (http/https).
        :param mode: "text" or "structured".
        :return: JSON string with the result.
        """
        v = self.valves
        if not url or not isinstance(url, str):
            return json.dumps({"error": "Missing url"})
        url = url.strip()
        if not re.match(r"^https?://", url, re.I):
            return json.dumps({"error": f"Invalid URL: {url}"})

        mode = (mode or "text").lower().strip()
        if mode not in ("text", "structured"):
            return json.dumps({"error": f"Invalid mode '{mode}'. Use 'text' or 'structured'."})

        # Reddit rewrite
        fetch_url = _normalize_reddit_url(url)
        reddit_rewritten = fetch_url != url

        await _emit_status(
            __event_emitter__,
            f"Fetching {fetch_url}" + (" (reddit .json)" if reddit_rewritten else ""),
        )

        try:
            fetched = await _resilient_fetch(
                fetch_url,
                timeout=v.HTTP_TIMEOUT_SECONDS,
                user_agent=v.USER_AGENT,
                verify_ssl=v.VERIFY_SSL,
                flaresolverr_url=v.FLARESOLVERR_URL or None,
                flaresolverr_timeout_ms=v.FLARESOLVERR_TIMEOUT_MS,
                emitter=__event_emitter__,
            )
        except Exception as e:
            await _emit_status(__event_emitter__, f"Fetch failed: {e}", done=True)
            return json.dumps({"error": f"Fetch failed: {e}", "url": fetch_url})

        status = fetched["status"]
        ctype = (fetched.get("content_type") or "").lower()
        via = fetched.get("via")

        # PDF handling: always plain text, regardless of requested mode
        if "pdf" in ctype or fetch_url.lower().endswith(".pdf"):
            body = fetched.get("bytes")
            if not body and fetched.get("text"):
                body = fetched["text"].encode("utf-8", errors="replace")
            if not body:
                await _emit_status(__event_emitter__, "PDF fetch returned no body", done=True)
                return json.dumps(
                    {"error": "PDF returned no content", "url": fetch_url, "status": status}
                )
            await _emit_status(__event_emitter__, "Extracting PDF text...")
            extracted = _pdf_to_text(body)
            extracted = _trim(extracted, v.MAX_PAGE_CHARS)
            await self._maybe_emit_citation(
                __event_emitter__, fetch_url, f"PDF: {fetch_url}", extracted
            )
            await _emit_status(__event_emitter__, "Done.", done=True)
            return json.dumps(
                {
                    "url": fetch_url,
                    "original_url": url,
                    "status": status,
                    "content_type": ctype or "application/pdf",
                    "via": via,
                    "format": "pdf_text",
                    "content": extracted,
                },
                ensure_ascii=False,
            )

        text = fetched.get("text") or ""

        # Reddit returns JSON; both modes just surface that JSON (it IS structured data)
        if reddit_rewritten or "json" in ctype:
            try:
                parsed = json.loads(text)
                # Produce a compact, model-friendly view for reddit posts/comments
                compact = self._compact_reddit_json(parsed) if reddit_rewritten else parsed
                dumped = json.dumps(compact, ensure_ascii=False, indent=2)
                dumped = _trim(dumped, v.MAX_PAGE_CHARS)
                await self._maybe_emit_citation(
                    __event_emitter__,
                    url,
                    f"Reddit JSON: {url}" if reddit_rewritten else fetch_url,
                    dumped,
                )
                await _emit_status(__event_emitter__, "Done.", done=True)
                return json.dumps(
                    {
                        "url": fetch_url,
                        "original_url": url,
                        "status": status,
                        "content_type": ctype or "application/json",
                        "via": via,
                        "format": "json",
                        "content": dumped,
                    },
                    ensure_ascii=False,
                )
            except Exception:
                # Not actually JSON — fall through to HTML handling
                pass

        # HTML / text
        if mode == "structured":
            try:
                structured = _structured_from_html(text, fetch_url)
            except Exception as e:
                return json.dumps({"error": f"Failed to parse HTML: {e}", "url": fetch_url})
            # Limit headings
            if structured.get("headings"):
                structured["headings"] = structured["headings"][: v.MAX_ENRICH_HEADINGS]
            dumped = json.dumps(structured, ensure_ascii=False, indent=2)
            dumped = _trim(dumped, v.MAX_PAGE_CHARS)
            await self._maybe_emit_citation(
                __event_emitter__,
                fetch_url,
                structured.get("title") or fetch_url,
                dumped,
            )
            await _emit_status(__event_emitter__, "Done.", done=True)
            return json.dumps(
                {
                    "url": fetch_url,
                    "original_url": url,
                    "status": status,
                    "content_type": ctype,
                    "via": via,
                    "format": "structured",
                    "content": structured,
                },
                ensure_ascii=False,
            )

        # mode == "text"
        try:
            plain = _plain_text_from_html(text)
        except Exception as e:
            return json.dumps({"error": f"Failed to parse HTML: {e}", "url": fetch_url})

        # Prepend title for context
        soup_title = None
        try:
            soup_title = _page_title(BeautifulSoup(text, "lxml"))
        except Exception:
            pass

        if soup_title:
            plain = f"{soup_title}\n\n{plain}"

        plain = _trim(plain, v.MAX_PAGE_CHARS)

        await self._maybe_emit_citation(
            __event_emitter__, fetch_url, soup_title or fetch_url, plain
        )
        if fetched.get("blocked_detected") and via == "direct":
            note = "NOTE: page appeared to be Cloudflare-blocked and FlareSolverr fallback did not succeed."
        else:
            note = None

        await _emit_status(__event_emitter__, "Done.", done=True)
        return json.dumps(
            {
                "url": fetch_url,
                "original_url": url,
                "status": status,
                "content_type": ctype,
                "via": via,
                "format": "text",
                "title": soup_title,
                "content": plain,
                "note": note,
            },
            ensure_ascii=False,
        )

    # ------------------------------------------------------------------
    # Internals
    # ------------------------------------------------------------------
    def _compact_reddit_json(self, data: Any) -> Any:
        """
        Reddit JSON for a post is a list of 2 listings (post, comments) full of
        redundant fields. Compact it to just what the model usually needs.
        """
        try:
            if isinstance(data, list) and len(data) == 2:
                post_listing, comments_listing = data

                def child_of(listing):
                    kids = (listing or {}).get("data", {}).get("children", []) or []
                    return kids

                post = None
                kids = child_of(post_listing)
                if kids:
                    pd = kids[0].get("data", {})
                    post = {
                        "title": pd.get("title"),
                        "author": pd.get("author"),
                        "subreddit": pd.get("subreddit"),
                        "score": pd.get("score"),
                        "num_comments": pd.get("num_comments"),
                        "permalink": pd.get("permalink"),
                        "url": pd.get("url"),
                        "selftext": pd.get("selftext"),
                        "created_utc": pd.get("created_utc"),
                    }

                comments = []

                def walk(node, depth=0):
                    if not isinstance(node, dict):
                        return
                    kind = node.get("kind")
                    d = node.get("data") or {}
                    if kind == "t1":
                        comments.append(
                            {
                                "author": d.get("author"),
                                "score": d.get("score"),
                                "depth": depth,
                                "body": d.get("body"),
                            }
                        )
                        replies = d.get("replies")
                        if isinstance(replies, dict):
                            for c in (replies.get("data") or {}).get("children", []) or []:
                                walk(c, depth + 1)

                for c in child_of(comments_listing):
                    walk(c, 0)

                return {"post": post, "comments": comments}
        except Exception:
            pass
        return data

    async def _maybe_emit_citation(
        self,
        emitter,
        url: str,
        title: str,
        content: str,
    ) -> None:
        if not emitter or not self.valves.EMIT_CITATIONS:
            return
        try:
            from datetime import datetime

            await emitter(
                {
                    "type": "citation",
                    "data": {
                        "document": [content[:2000]],
                        "metadata": [
                            {
                                "date_accessed": datetime.utcnow().isoformat(),
                                "source": title or url,
                                "url": url,
                            }
                        ],
                        "source": {"name": title or url, "url": url},
                    },
                }
            )
        except Exception:
            pass