"""
title: Smart Web Search
description: An intelligent web search tool that lets the model decide when it needs external knowledge. Uses SearXNG for search, scrapes pages for full content, falls back to FlareSolverr for captcha-blocked pages, and handles PDFs. The model formulates its own search queries and iterates until it has enough information to answer.
author: Claude
version: 1.0.0
license: MIT
requirements: beautifulsoup4, requests
"""

import json
import logging
import re
import asyncio
import concurrent.futures
from typing import Callable, Any, Optional, List, Dict
from urllib.parse import urlparse, urljoin, quote_plus

import requests
from bs4 import BeautifulSoup
from pydantic import BaseModel, Field

logger = logging.getLogger(__name__)
if not logger.handlers:
    handler = logging.StreamHandler()
    handler.setFormatter(logging.Formatter("%(asctime)s - %(levelname)s - %(message)s"))
    logger.addHandler(handler)
    logger.setLevel(logging.INFO)


# ---------------------------------------------------------------------------
# Event emitter helper
# ---------------------------------------------------------------------------
class EventEmitter:
    """Convenience wrapper around Open WebUI's __event_emitter__ callback."""

    def __init__(self, event_emitter: Callable[[dict], Any]):
        self.event_emitter = event_emitter

    async def emit(
        self,
        description: str,
        status: str = "in_progress",
        done: bool = False,
    ):
        if self.event_emitter:
            await self.event_emitter(
                {
                    "type": "status",
                    "data": {
                        "status": status,
                        "description": description,
                        "done": done,
                    },
                }
            )

    async def citation(self, title: str, url: str, content: str):
        if self.event_emitter:
            await self.event_emitter(
                {
                    "type": "citation",
                    "data": {
                        "document": [content[:1000]],
                        "metadata": [{"source": url, "title": title}],
                        "source": {"name": title, "url": url},
                    },
                }
            )

    async def error(self, description: str):
        await self.emit(description=description, status="error", done=True)

    async def done(self, description: str = "Complete"):
        await self.emit(description=description, status="complete", done=True)


# ---------------------------------------------------------------------------
# Page scraping helpers
# ---------------------------------------------------------------------------
class PageScraper:
    """Fetches and extracts text from web pages with FlareSolverr fallback."""

    CAPTCHA_INDICATORS = [
        "captcha",
        "cf-challenge",
        "challenge-platform",
        "just a moment",
        "checking your browser",
        "ray id",
        "attention required",
        "access denied",
        "blocked",
        "security check",
        "ddos protection",
    ]

    def __init__(self, valves):
        self.valves = valves
        self.session = requests.Session()
        self.session.headers.update(
            {
                "User-Agent": valves.USER_AGENT,
                "Accept": "text/html,application/xhtml+xml,application/xml;q=0.9,*/*;q=0.8",
                "Accept-Language": "en-US,en;q=0.5",
            }
        )

    def _looks_blocked(self, response: requests.Response) -> bool:
        """Heuristic check for captcha / anti-bot pages."""
        if response.status_code in (403, 429, 503):
            return True
        body_lower = response.text[:5000].lower()
        hits = sum(1 for indicator in self.CAPTCHA_INDICATORS if indicator in body_lower)
        # If the page is very short AND has indicators, it's likely a block page
        if hits >= 2:
            return True
        if response.status_code == 200 and len(response.text) < 2000 and hits >= 1:
            return True
        return False

    def _is_pdf_url(self, url: str, response: Optional[requests.Response] = None) -> bool:
        """Check if a URL points to a PDF."""
        if url.lower().endswith(".pdf"):
            return True
        if response and "application/pdf" in response.headers.get("Content-Type", ""):
            return True
        return False

    def _extract_pdf_text(self, content: bytes) -> str:
        """
        Extract text from PDF bytes.
        Tries PyPDF2/pypdf first (commonly available in Open WebUI),
        then falls back to a basic binary extraction.
        """
        # Try pypdf (the modern fork, often available in Open WebUI)
        try:
            import pypdf
            import io

            reader = pypdf.PdfReader(io.BytesIO(content))
            text_parts = []
            for page in reader.pages:
                page_text = page.extract_text()
                if page_text:
                    text_parts.append(page_text)
            if text_parts:
                return "\n\n".join(text_parts)
        except ImportError:
            pass
        except Exception as e:
            logger.warning(f"pypdf extraction failed: {e}")

        # Try PyPDF2 (legacy, but also commonly installed)
        try:
            import PyPDF2
            import io

            reader = PyPDF2.PdfReader(io.BytesIO(content))
            text_parts = []
            for page in reader.pages:
                page_text = page.extract_text()
                if page_text:
                    text_parts.append(page_text)
            if text_parts:
                return "\n\n".join(text_parts)
        except ImportError:
            pass
        except Exception as e:
            logger.warning(f"PyPDF2 extraction failed: {e}")

        # Try pdfminer (another common option)
        try:
            from pdfminer.high_level import extract_text
            import io

            text = extract_text(io.BytesIO(content))
            if text and text.strip():
                return text
        except ImportError:
            pass
        except Exception as e:
            logger.warning(f"pdfminer extraction failed: {e}")

        return "[PDF detected but no PDF extraction library available. Install pypdf, PyPDF2, or pdfminer.six.]"

    def _extract_text_from_html(self, html: str, url: str) -> str:
        """Extract meaningful text content from HTML."""
        soup = BeautifulSoup(html, "html.parser")

        # Remove noise elements
        for tag in soup.find_all(
            ["script", "style", "nav", "footer", "header", "aside", "iframe", "noscript"]
        ):
            tag.decompose()

        # Try to find the main content area
        main = (
            soup.find("main")
            or soup.find("article")
            or soup.find("div", {"role": "main"})
            or soup.find("div", class_=re.compile(r"(content|article|post|entry|main)", re.I))
        )

        target = main if main else soup.body if soup.body else soup
        text = target.get_text(separator="\n", strip=True)

        # Clean up excessive whitespace
        lines = [line.strip() for line in text.splitlines() if line.strip()]
        text = "\n".join(lines)

        # Truncate to configured max
        max_chars = self.valves.MAX_PAGE_CONTENT_LENGTH
        if len(text) > max_chars:
            text = text[:max_chars] + f"\n\n[Content truncated at {max_chars} characters]"

        return text

    def _fetch_via_flaresolverr(self, url: str) -> Optional[str]:
        """Attempt to fetch a page through FlareSolverr to bypass captchas."""
        if not self.valves.FLARESOLVERR_URL:
            return None

        try:
            payload = {
                "cmd": "request.get",
                "url": url,
                "maxTimeout": self.valves.FLARESOLVERR_TIMEOUT * 1000,
            }
            resp = requests.post(
                self.valves.FLARESOLVERR_URL,
                json=payload,
                timeout=self.valves.FLARESOLVERR_TIMEOUT + 10,
            )
            resp.raise_for_status()
            data = resp.json()

            if data.get("status") == "ok":
                solution = data.get("solution", {})
                return solution.get("response", "")
            else:
                logger.warning(f"FlareSolverr returned status: {data.get('status')}")
                return None
        except Exception as e:
            logger.error(f"FlareSolverr request failed for {url}: {e}")
            return None

    def scrape(self, url: str) -> Dict[str, Any]:
        """
        Scrape a URL and return extracted content.
        Returns dict with keys: url, title, content, source, error
        """
        result = {"url": url, "title": "", "content": "", "source": "direct", "error": None}
        parsed = urlparse(url)
        if not parsed.scheme:
            url = "https://" + url
            result["url"] = url

        try:
            resp = self.session.get(url, timeout=self.valves.REQUEST_TIMEOUT, allow_redirects=True)

            # Handle PDFs
            if self._is_pdf_url(url, resp):
                result["title"] = url.split("/")[-1]
                result["content"] = self._extract_pdf_text(resp.content)
                result["source"] = "pdf"
                return result

            # Check if blocked / captcha
            if self._looks_blocked(resp):
                logger.info(f"Page appears blocked, trying FlareSolverr: {url}")
                html = self._fetch_via_flaresolverr(url)
                if html:
                    result["source"] = "flaresolverr"
                else:
                    result["error"] = "Page blocked by captcha/anti-bot and FlareSolverr unavailable or failed"
                    # Still try to extract whatever we got
                    html = resp.text
            else:
                resp.raise_for_status()
                html = resp.text

            soup = BeautifulSoup(html, "html.parser")
            title_tag = soup.find("title")
            result["title"] = title_tag.get_text(strip=True) if title_tag else parsed.netloc
            result["content"] = self._extract_text_from_html(html, url)

        except requests.exceptions.Timeout:
            result["error"] = f"Request timed out after {self.valves.REQUEST_TIMEOUT}s"
        except requests.exceptions.ConnectionError:
            result["error"] = "Connection failed"
        except requests.exceptions.HTTPError as e:
            # Try FlareSolverr as fallback for HTTP errors
            logger.info(f"HTTP error {e}, trying FlareSolverr: {url}")
            html = self._fetch_via_flaresolverr(url)
            if html:
                soup = BeautifulSoup(html, "html.parser")
                title_tag = soup.find("title")
                result["title"] = title_tag.get_text(strip=True) if title_tag else parsed.netloc
                result["content"] = self._extract_text_from_html(html, url)
                result["source"] = "flaresolverr"
            else:
                result["error"] = f"HTTP {e.response.status_code if e.response else 'unknown'}"
        except Exception as e:
            result["error"] = str(e)

        return result


# ---------------------------------------------------------------------------
# Main Tool class
# ---------------------------------------------------------------------------
class Tools:
    class Valves(BaseModel):
        # --- SearXNG Configuration ---
        SEARXNG_BASE_URL: str = Field(
            default="http://searxng:8080",
            description="Base URL of your SearXNG instance (e.g. http://searxng:8080 or http://192.168.1.100:8080)",
        )

        # --- FlareSolverr Configuration ---
        FLARESOLVERR_URL: str = Field(
            default="http://flaresolverr:8191/v1",
            description="FlareSolverr API endpoint URL. Leave empty to disable FlareSolverr fallback.",
        )
        FLARESOLVERR_TIMEOUT: int = Field(
            default=60,
            description="Timeout in seconds for FlareSolverr requests",
        )

        # --- Search Behavior ---
        SEARCH_RESULTS_COUNT: int = Field(
            default=5,
            description="Number of search results to return from SearXNG per query",
        )
        PAGES_TO_SCRAPE: int = Field(
            default=3,
            description="Number of top search result pages to scrape for full content",
        )
        SEARCH_CATEGORIES: str = Field(
            default="general",
            description="Comma-separated SearXNG search categories (e.g. general,it,science)",
        )
        SEARCH_LANGUAGE: str = Field(
            default="en",
            description="Language code for search results (e.g. en, de, fr)",
        )
        SEARCH_TIME_RANGE: str = Field(
            default="",
            description="Time range filter for search results. Leave empty for no filter. Options: day, week, month, year",
        )

        # --- Content Processing ---
        MAX_PAGE_CONTENT_LENGTH: int = Field(
            default=20000,
            description="Maximum number of characters to extract per page",
        )
        MIN_CONTENT_LENGTH: int = Field(
            default=50,
            description="Minimum content length (in characters) for a scraped page to be considered valid",
        )

        # --- Request Settings ---
        REQUEST_TIMEOUT: int = Field(
            default=15,
            description="Timeout in seconds for direct HTTP requests",
        )
        USER_AGENT: str = Field(
            default="Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/120.0.0.0 Safari/537.36",
            description="User-Agent header for HTTP requests",
        )
        CONCURRENT_SCRAPE_WORKERS: int = Field(
            default=3,
            description="Number of concurrent workers for page scraping",
        )

        # --- Ignored Domains ---
        IGNORED_DOMAINS: str = Field(
            default="",
            description="Comma-separated list of domains to skip when scraping (e.g. pinterest.com,facebook.com)",
        )

    class UserValves(BaseModel):
        SHOW_STATUS_UPDATES: bool = Field(
            default=True,
            description="Show status updates during search operations",
        )
        INCLUDE_CITATIONS: bool = Field(
            default=True,
            description="Include source citations with search results",
        )

    def __init__(self):
        self.valves = self.Valves()

    def _get_ignored_domains(self) -> set:
        """Parse the ignored domains valve into a set."""
        if not self.valves.IGNORED_DOMAINS.strip():
            return set()
        return {d.strip().lower() for d in self.valves.IGNORED_DOMAINS.split(",") if d.strip()}

    def _is_domain_ignored(self, url: str) -> bool:
        """Check if a URL's domain is in the ignore list."""
        ignored = self._get_ignored_domains()
        if not ignored:
            return False
        try:
            domain = urlparse(url).netloc.lower()
            return any(ignored_domain in domain for ignored_domain in ignored)
        except Exception:
            return False

    async def search_web(
        self,
        query: str,
        __event_emitter__: Callable[[dict], Any] = None,
        __user__: Optional[dict] = None,
    ) -> str:
        """
        Search the web and return the search results with scraped page content.
        Use this tool when you need current or up-to-date information that you don't have,
        when you're unsure about specific facts, versions, configurations, or documentation,
        or when the user's question involves recent events, products, or rapidly changing information.

        You should formulate the search query yourself based on what specific information
        you need. Keep queries short and focused (1-6 words work best). If the first search
        doesn't give you what you need, you can call this tool again with a refined query.

        :param query: A concise search query to find the information you need. Formulate this yourself based on what knowledge gap you're trying to fill.
        :return: A JSON string containing search results with scraped page content, titles, URLs, and snippets.
        """
        emitter = EventEmitter(__event_emitter__)

        # Get user valve preferences
        show_status = True
        include_citations = True
        if __user__:
            user_valves = __user__.get("valves")
            if user_valves:
                show_status = getattr(user_valves, "SHOW_STATUS_UPDATES", True)
                include_citations = getattr(user_valves, "INCLUDE_CITATIONS", True)

        if show_status:
            await emitter.emit(f"Searching: {query}")

        # --- Step 1: Query SearXNG ---
        search_url = f"{self.valves.SEARXNG_BASE_URL.rstrip('/')}/search"
        params = {
            "q": query,
            "format": "json",
            "number_of_results": self.valves.SEARCH_RESULTS_COUNT,
            "categories": self.valves.SEARCH_CATEGORIES,
            "language": self.valves.SEARCH_LANGUAGE,
        }
        if self.valves.SEARCH_TIME_RANGE:
            params["time_range"] = self.valves.SEARCH_TIME_RANGE

        try:
            resp = requests.get(
                search_url,
                params=params,
                headers={"User-Agent": self.valves.USER_AGENT},
                timeout=self.valves.REQUEST_TIMEOUT,
            )
            resp.raise_for_status()
            search_data = resp.json()
        except requests.exceptions.RequestException as e:
            await emitter.error(f"Search request failed: {e}")
            return json.dumps({"error": f"SearXNG search failed: {str(e)}", "results": []})

        raw_results = search_data.get("results", [])
        if not raw_results:
            await emitter.done("No search results found")
            return json.dumps({"query": query, "results": [], "message": "No results found. Try a different or broader search query."})

        # --- Step 2: Filter and deduplicate results ---
        seen_urls = set()
        filtered_results = []
        for r in raw_results:
            url = r.get("url", "")
            if not url or url in seen_urls:
                continue
            if self._is_domain_ignored(url):
                continue
            seen_urls.add(url)
            filtered_results.append(r)

        if show_status:
            await emitter.emit(f"Found {len(filtered_results)} results, scraping top pages...")

        # --- Step 3: Scrape top N pages concurrently ---
        scraper = PageScraper(self.valves)
        pages_to_scrape = filtered_results[: self.valves.PAGES_TO_SCRAPE]
        remaining_results = filtered_results[self.valves.PAGES_TO_SCRAPE :]

        scraped_pages = []
        loop = asyncio.get_event_loop()

        with concurrent.futures.ThreadPoolExecutor(
            max_workers=self.valves.CONCURRENT_SCRAPE_WORKERS
        ) as executor:
            futures = {
                executor.submit(scraper.scrape, r["url"]): r for r in pages_to_scrape
            }
            for future in concurrent.futures.as_completed(futures):
                original_result = futures[future]
                try:
                    page_data = future.result()
                    scraped_pages.append(
                        {
                            "url": page_data["url"],
                            "title": page_data["title"] or original_result.get("title", ""),
                            "snippet": original_result.get("content", ""),
                            "content": page_data["content"],
                            "source": page_data["source"],
                            "error": page_data["error"],
                        }
                    )
                except Exception as e:
                    scraped_pages.append(
                        {
                            "url": original_result.get("url", ""),
                            "title": original_result.get("title", ""),
                            "snippet": original_result.get("content", ""),
                            "content": "",
                            "source": "failed",
                            "error": str(e),
                        }
                    )

        # Filter out pages with too little content
        valid_pages = []
        for page in scraped_pages:
            if page["content"] and len(page["content"]) >= self.valves.MIN_CONTENT_LENGTH:
                valid_pages.append(page)
            elif page["snippet"]:
                # Fall back to the search snippet if scrape failed
                page["content"] = page["snippet"]
                page["source"] = "snippet_only"
                valid_pages.append(page)

        # --- Step 4: Build additional results (not scraped, snippet only) ---
        additional_results = []
        for r in remaining_results:
            additional_results.append(
                {
                    "url": r.get("url", ""),
                    "title": r.get("title", ""),
                    "snippet": r.get("content", ""),
                }
            )

        # --- Step 5: Emit citations ---
        if include_citations:
            for page in valid_pages:
                await emitter.citation(
                    title=page["title"],
                    url=page["url"],
                    content=page["content"][:500],
                )

        # --- Step 6: Format and return ---
        output = {
            "query": query,
            "results_scraped": valid_pages,
            "results_additional": additional_results,
            "total_found": len(filtered_results),
            "pages_scraped": len(valid_pages),
        }

        if show_status:
            await emitter.done(
                f"Search complete: {len(valid_pages)} pages scraped, {len(additional_results)} additional results"
            )

        return json.dumps(output, ensure_ascii=False)

    async def fetch_page(
        self,
        url: str,
        __event_emitter__: Callable[[dict], Any] = None,
        __user__: Optional[dict] = None,
    ) -> str:
        """
        Fetch and extract the full text content from a specific URL.
        Use this tool when you need to read the complete content of a specific web page,
        for example when a search result snippet looks promising but you need more detail,
        or when the user provides a URL they want you to read.

        Handles regular web pages, pages blocked by captchas (via FlareSolverr fallback),
        and PDF documents.

        :param url: The complete URL of the page to fetch (must include https:// or http://).
        :return: A JSON string containing the page title, extracted text content, and metadata.
        """
        emitter = EventEmitter(__event_emitter__)

        show_status = True
        include_citations = True
        if __user__:
            user_valves = __user__.get("valves")
            if user_valves:
                show_status = getattr(user_valves, "SHOW_STATUS_UPDATES", True)
                include_citations = getattr(user_valves, "INCLUDE_CITATIONS", True)

        if show_status:
            await emitter.emit(f"Fetching: {url}")

        scraper = PageScraper(self.valves)

        loop = asyncio.get_event_loop()
        page_data = await loop.run_in_executor(None, scraper.scrape, url)

        if page_data["error"] and not page_data["content"]:
            await emitter.error(f"Failed to fetch page: {page_data['error']}")
            return json.dumps(
                {"url": url, "error": page_data["error"], "content": ""},
                ensure_ascii=False,
            )

        if include_citations and page_data["content"]:
            await emitter.citation(
                title=page_data["title"],
                url=url,
                content=page_data["content"][:500],
            )

        if show_status:
            content_len = len(page_data["content"])
            source_note = f" (via {page_data['source']})" if page_data["source"] != "direct" else ""
            await emitter.done(f"Fetched {content_len} chars{source_note}")

        return json.dumps(
            {
                "url": url,
                "title": page_data["title"],
                "content": page_data["content"],
                "source": page_data["source"],
                "error": page_data["error"],
            },
            ensure_ascii=False,
        )