"""
title: Smart Web Search
description: A tool that lets the LLM search the web when it lacks knowledge to answer a question. The model crafts its own search queries and can fetch full page content for deeper research. Supports SearXNG, Tavily, and any SearXNG-compatible API. Optionally uses FlareSolverr to bypass bot protection on fetched pages.
author: mdelponte
version: 2.0.0
license: MIT
requirements: requests, beautifulsoup4
"""

import json
import random
import requests
from typing import Callable, Any
from pydantic import BaseModel, Field
from bs4 import BeautifulSoup


# Realistic browser User-Agent strings to rotate through
USER_AGENTS = [
    "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/131.0.0.0 Safari/537.36",
    "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/131.0.0.0 Safari/537.36",
    "Mozilla/5.0 (X11; Linux x86_64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/131.0.0.0 Safari/537.36",
    "Mozilla/5.0 (Windows NT 10.0; Win64; x64; rv:133.0) Gecko/20100101 Firefox/133.0",
    "Mozilla/5.0 (Macintosh; Intel Mac OS X 10.15; rv:133.0) Gecko/20100101 Firefox/133.0",
    "Mozilla/5.0 (X11; Ubuntu; Linux x86_64; rv:133.0) Gecko/20100101 Firefox/133.0",
    "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/131.0.0.0 Safari/537.36 Edg/131.0.0.0",
]


class Tools:
    class Valves(BaseModel):
        SEARCH_ENGINE_URL: str = Field(
            default="http://searxng:8080/search",
            description="The search API endpoint URL. For SearXNG use the /search endpoint (e.g. http://searxng:8080/search). For Tavily use https://api.tavily.com/search.",
        )
        SEARCH_ENGINE_TYPE: str = Field(
            default="searxng",
            description="Type of search engine: 'searxng' or 'tavily'.",
        )
        SEARCH_API_KEY: str = Field(
            default="",
            description="API key for the search engine (required for Tavily, optional for SearXNG if auth is enabled).",
        )
        MAX_SEARCH_RESULTS: int = Field(
            default=5,
            description="Maximum number of search results to return per query.",
        )
        MAX_CONTENT_LENGTH: int = Field(
            default=4000,
            description="Maximum character length of fetched page content to return to the model.",
        )
        FETCH_TIMEOUT: int = Field(
            default=15,
            description="Timeout in seconds for HTTP requests.",
        )
        SEARCH_CATEGORIES: str = Field(
            default="general",
            description="Comma-separated SearXNG categories to search (e.g. 'general,it,science'). Only used for SearXNG.",
        )
        FETCH_FULL_PAGE: bool = Field(
            default=True,
            description="When enabled, the fetch_page tool is available for the model to read full webpage content.",
        )
        FLARESOLVERR_URL: str = Field(
            default="",
            description="FlareSolverr endpoint URL (e.g. http://flaresolverr:8191/v1). Leave empty to disable. Used as a fallback when direct fetch fails due to bot protection.",
        )
        RETRY_WITH_FLARESOLVERR: bool = Field(
            default=True,
            description="When enabled, if a direct page fetch fails (403, captcha, etc.), automatically retry through FlareSolverr.",
        )

    class UserValves(BaseModel):
        SHOW_STATUS_UPDATES: bool = Field(
            default=True,
            description="Show status messages during search and fetch operations.",
        )

    def __init__(self):
        self.valves = self.Valves()

    def _get_headers(self) -> dict:
        """Get request headers with a random realistic User-Agent."""
        ua = random.choice(USER_AGENTS)
        return {
            "User-Agent": ua,
            "Accept": "text/html,application/xhtml+xml,application/xml;q=0.9,image/webp,*/*;q=0.8",
            "Accept-Language": "en-US,en;q=0.9",
            "Accept-Encoding": "gzip, deflate, br",
            "DNT": "1",
            "Connection": "keep-alive",
            "Upgrade-Insecure-Requests": "1",
            "Sec-Fetch-Dest": "document",
            "Sec-Fetch-Mode": "navigate",
            "Sec-Fetch-Site": "none",
            "Sec-Fetch-User": "?1",
            "Cache-Control": "max-age=0",
        }

    async def _emit_status(
        self,
        __event_emitter__: Callable[[dict], Any],
        description: str,
        done: bool = False,
    ):
        """Emit a status update to the UI."""
        if __event_emitter__:
            await __event_emitter__(
                {
                    "type": "status",
                    "data": {
                        "status": "complete" if done else "in_progress",
                        "description": description,
                        "done": done,
                    },
                }
            )

    async def _emit_source(
        self,
        __event_emitter__: Callable[[dict], Any],
        url: str,
        title: str,
    ):
        """Emit a source citation to the UI."""
        if __event_emitter__:
            await __event_emitter__(
                {
                    "type": "citation",
                    "data": {
                        "document": [title],
                        "metadata": [{"source": url, "html": False}],
                        "source": {"name": title, "url": url},
                    },
                }
            )

    def _search_searxng(self, query: str) -> list[dict]:
        """Execute a search against a SearXNG instance."""
        params = {
            "q": query,
            "format": "json",
            "categories": self.valves.SEARCH_CATEGORIES,
        }
        headers = {}
        if self.valves.SEARCH_API_KEY:
            headers["Authorization"] = f"Bearer {self.valves.SEARCH_API_KEY}"

        response = requests.get(
            self.valves.SEARCH_ENGINE_URL,
            params=params,
            headers=headers,
            timeout=self.valves.FETCH_TIMEOUT,
        )
        response.raise_for_status()
        data = response.json()

        results = []
        for r in data.get("results", [])[: self.valves.MAX_SEARCH_RESULTS]:
            results.append(
                {
                    "title": r.get("title", ""),
                    "url": r.get("url", ""),
                    "snippet": r.get("content", ""),
                    "engine": r.get("engine", ""),
                }
            )
        return results

    def _search_tavily(self, query: str) -> list[dict]:
        """Execute a search against the Tavily API."""
        payload = {
            "api_key": self.valves.SEARCH_API_KEY,
            "query": query,
            "max_results": self.valves.MAX_SEARCH_RESULTS,
            "include_answer": False,
        }
        response = requests.post(
            self.valves.SEARCH_ENGINE_URL,
            json=payload,
            timeout=self.valves.FETCH_TIMEOUT,
        )
        response.raise_for_status()
        data = response.json()

        results = []
        for r in data.get("results", []):
            results.append(
                {
                    "title": r.get("title", ""),
                    "url": r.get("url", ""),
                    "snippet": r.get("content", ""),
                    "engine": "tavily",
                }
            )
        return results

    def _extract_text(self, html: str) -> str:
        """Extract clean text content from HTML."""
        soup = BeautifulSoup(html, "html.parser")

        # Remove non-content elements
        for tag in soup(
            [
                "script", "style", "nav", "footer", "header", "aside",
                "iframe", "noscript", "svg", "form", "button",
            ]
        ):
            tag.decompose()

        # Try to find the main content area
        main = (
            soup.find("main")
            or soup.find("article")
            or soup.find("div", {"role": "main"})
            or soup.find("div", {"id": "content"})
            or soup.find("div", {"class": "content"})
        )
        if main:
            text = main.get_text(separator="\n", strip=True)
        else:
            text = soup.get_text(separator="\n", strip=True)

        # Clean up excessive whitespace
        lines = [line.strip() for line in text.splitlines() if line.strip()]
        text = "\n".join(lines)
        return text

    def _fetch_direct(self, url: str) -> tuple[str, int]:
        """
        Fetch a URL directly with browser-like headers.
        Returns (html_content, status_code).
        """
        response = requests.get(
            url,
            headers=self._get_headers(),
            timeout=self.valves.FETCH_TIMEOUT,
            allow_redirects=True,
        )
        return response.text, response.status_code

    def _fetch_via_flaresolverr(self, url: str) -> str:
        """
        Fetch a URL through FlareSolverr to bypass bot protection.
        Returns the HTML content.
        """
        payload = {
            "cmd": "request.get",
            "url": url,
            "maxTimeout": self.valves.FETCH_TIMEOUT * 1000,
        }
        response = requests.post(
            self.valves.FLARESOLVERR_URL,
            json=payload,
            timeout=self.valves.FETCH_TIMEOUT + 10,  # Extra time for solver
        )
        response.raise_for_status()
        data = response.json()

        if data.get("status") == "ok":
            solution = data.get("solution", {})
            return solution.get("response", "")
        else:
            raise Exception(
                data.get("message", "FlareSolverr returned a non-ok status")
            )

    async def search_web(
        self,
        query: str,
        __event_emitter__: Callable[[dict], Any] = None,
        __user__: dict = {},
    ) -> str:
        """
        Search the web for information. You should use this tool liberally — not just when
        you are uncertain, but any time your response could be improved with up-to-date data,
        verified facts, specific references, official documentation, configuration examples,
        version-specific details, changelogs, or technical accuracy. If the user is asking
        about a specific technology, product, API, library, tool, error message, configuration,
        or anything where the correct answer depends on the current version or state of the
        world, search for it. When a user reports an error or unexpected behavior, search for
        that error message or symptom. When discussing setup steps, helm charts, config files,
        or deployment, search for the official docs. Prefer searching over guessing. You should
        craft a concise, targeted search query yourself — do NOT pass the user's full message.
        Good queries are 2-8 words focused on the specific fact or document you need.
        You may call this tool multiple times with different queries to gather information
        from different angles.

        :param query: A concise search query you have crafted to find the specific information needed (2-8 words).
        :return: A JSON string containing search results with titles, URLs, and snippets.
        """
        user_valves = __user__.get("valves")
        if not user_valves:
            user_valves = self.UserValves()
        show_status = user_valves.SHOW_STATUS_UPDATES

        if show_status:
            await self._emit_status(__event_emitter__, f"Searching: {query}")

        try:
            if self.valves.SEARCH_ENGINE_TYPE.lower() == "tavily":
                results = self._search_tavily(query)
            else:
                results = self._search_searxng(query)

            if not results:
                if show_status:
                    await self._emit_status(
                        __event_emitter__, "No results found.", done=True
                    )
                return json.dumps(
                    {"query": query, "results": [], "message": "No results found."}
                )

            # Emit citations for each result
            for r in results:
                await self._emit_source(__event_emitter__, r["url"], r["title"])

            if show_status:
                await self._emit_status(
                    __event_emitter__,
                    f"Found {len(results)} results.",
                    done=True,
                )

            return json.dumps({"query": query, "results": results}, ensure_ascii=False)

        except requests.exceptions.ConnectionError:
            error_msg = f"Could not connect to search engine at {self.valves.SEARCH_ENGINE_URL}. Check the URL in tool settings."
            if show_status:
                await self._emit_status(__event_emitter__, error_msg, done=True)
            return json.dumps({"error": error_msg})
        except requests.exceptions.Timeout:
            error_msg = "Search request timed out."
            if show_status:
                await self._emit_status(__event_emitter__, error_msg, done=True)
            return json.dumps({"error": error_msg})
        except Exception as e:
            error_msg = f"Search failed: {str(e)}"
            if show_status:
                await self._emit_status(__event_emitter__, error_msg, done=True)
            return json.dumps({"error": error_msg})

    async def fetch_page(
        self,
        url: str,
        __event_emitter__: Callable[[dict], Any] = None,
        __user__: dict = {},
    ) -> str:
        """
        Fetch and extract the text content of a webpage. Use this after searching when a
        search snippet does not contain enough detail to fully answer the question. This is
        especially useful for reading official documentation, configuration references, changelogs,
        troubleshooting guides, README files, and technical articles. Only fetch URLs that were
        returned by a previous search_web call. You may call this multiple times to read
        several pages if needed.

        :param url: The full URL of the page to fetch.
        :return: The extracted text content of the page, truncated to the configured maximum length.
        """
        if not self.valves.FETCH_FULL_PAGE:
            return json.dumps(
                {"error": "Page fetching is disabled in tool settings."}
            )

        user_valves = __user__.get("valves")
        if not user_valves:
            user_valves = self.UserValves()
        show_status = user_valves.SHOW_STATUS_UPDATES

        if show_status:
            await self._emit_status(__event_emitter__, f"Fetching: {url}")

        html = None
        used_flaresolverr = False

        # --- Attempt 1: Direct fetch ---
        try:
            html, status_code = self._fetch_direct(url)

            # Detect soft blocks: 403, captcha pages, empty responses
            is_blocked = False
            if status_code == 403:
                is_blocked = True
            elif status_code == 503:
                is_blocked = True
            elif html and len(html) < 2000:
                html_lower = html.lower()
                block_signals = [
                    "captcha",
                    "cf-browser-verification",
                    "challenge-platform",
                    "just a moment",
                    "checking your browser",
                    "access denied",
                    "please enable javascript",
                    "ray id",
                    "cloudflare",
                    "bot detection",
                    "are you a robot",
                    "verify you are human",
                ]
                if any(signal in html_lower for signal in block_signals):
                    is_blocked = True

            if is_blocked:
                raise requests.exceptions.HTTPError(
                    f"Blocked (status {status_code})"
                )

        except Exception as direct_error:
            # --- Attempt 2: FlareSolverr fallback ---
            if (
                self.valves.FLARESOLVERR_URL
                and self.valves.RETRY_WITH_FLARESOLVERR
            ):
                if show_status:
                    await self._emit_status(
                        __event_emitter__,
                        f"Direct fetch blocked, retrying with FlareSolverr...",
                    )
                try:
                    html = self._fetch_via_flaresolverr(url)
                    used_flaresolverr = True
                except Exception as flare_error:
                    error_msg = (
                        f"Direct fetch failed: {str(direct_error)}. "
                        f"FlareSolverr also failed: {str(flare_error)}"
                    )
                    if show_status:
                        await self._emit_status(
                            __event_emitter__, error_msg, done=True
                        )
                    return json.dumps({"error": error_msg})
            else:
                error_msg = f"Failed to fetch page: {str(direct_error)}"
                if self.valves.FLARESOLVERR_URL == "":
                    error_msg += " (FlareSolverr is not configured — set FLARESOLVERR_URL in tool settings to handle bot protection)"
                if show_status:
                    await self._emit_status(
                        __event_emitter__, error_msg, done=True
                    )
                return json.dumps({"error": error_msg})

        # --- Extract text from HTML ---
        try:
            text = self._extract_text(html)

            if not text or len(text) < 50:
                return json.dumps(
                    {
                        "url": url,
                        "content": "",
                        "message": "Page returned very little readable text. It may require JavaScript rendering or authentication.",
                    }
                )

            # Truncate to max length
            max_len = self.valves.MAX_CONTENT_LENGTH
            if len(text) > max_len:
                text = text[:max_len] + "\n\n[Content truncated — increase MAX_CONTENT_LENGTH in tool settings to see more]"

            await self._emit_source(__event_emitter__, url, url)

            method = "FlareSolverr" if used_flaresolverr else "direct"
            if show_status:
                await self._emit_status(
                    __event_emitter__,
                    f"Fetched {len(text)} chars ({method}).",
                    done=True,
                )

            return json.dumps(
                {"url": url, "content": text}, ensure_ascii=False
            )

        except Exception as e:
            error_msg = f"Failed to extract content: {str(e)}"
            if show_status:
                await self._emit_status(__event_emitter__, error_msg, done=True)
            return json.dumps({"error": error_msg})
