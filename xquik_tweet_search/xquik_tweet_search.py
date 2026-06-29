"""
title: Xquik Tweet Search
author: Burak Bayir
author_url: https://github.com/kriptoburak
version: 1.0.0
required_open_webui_version: 0.5.0
license: MIT
description: Search X/Twitter posts through Xquik's public REST API. Requires an Xquik API key and keeps the tool read-only.
requirements: requests
"""

from __future__ import annotations

import json
import time
from typing import Any, Awaitable, Callable, Literal, Optional

import anyio
import requests
from pydantic import BaseModel, Field


class Tools:
    """
    Read-only X/Twitter tweet search for Open WebUI via Xquik.

    The tool maps directly to the public `GET /api/v1/x/tweets/search`
    endpoint and returns compact JSON so models can cite and compare results.
    """

    class Valves(BaseModel):
        xquik_api_key: str = Field(
            default="",
            description="Xquik API key. Sent only as the x-api-key header.",
            json_schema_extra={"input": {"type": "password"}},
        )
        base_url: str = Field(
            default="https://xquik.com",
            description="Xquik API base URL.",
        )
        request_timeout: int = Field(
            default=30,
            description="HTTP request timeout in seconds.",
        )
        default_limit: int = Field(
            default=20,
            description="Default maximum tweets to return when no limit is supplied.",
        )
        max_limit: int = Field(
            default=50,
            description="Maximum tweets this Open WebUI tool will request at once.",
        )
        cache_ttl_seconds: int = Field(
            default=30,
            description="Cache identical searches for this many seconds. Set to 0 to disable.",
        )

    class UserValves(BaseModel):
        verbose_status: bool = Field(
            default=True,
            description="Show progress status messages while the tool searches.",
        )
        include_metrics: bool = Field(
            default=True,
            description="Include like, repost, reply, quote, and view counts when present.",
        )
        max_text_length: int = Field(
            default=500,
            description="Maximum tweet text characters returned per result. Set to 0 for no truncation.",
        )

    def __init__(self) -> None:
        self.valves = self.Valves()
        self._cache: dict[str, tuple[float, Any]] = {}
        self.citation = True

    def _cache_get(self, key: str) -> Optional[Any]:
        if self.valves.cache_ttl_seconds <= 0:
            return None
        entry = self._cache.get(key)
        if entry is None:
            return None
        created_at, value = entry
        if time.time() - created_at > self.valves.cache_ttl_seconds:
            self._cache.pop(key, None)
            return None
        return value

    def _cache_set(self, key: str, value: Any) -> None:
        if self.valves.cache_ttl_seconds <= 0:
            return
        self._cache[key] = (time.time(), value)

    def _normalize_limit(self, limit: Optional[int]) -> int:
        requested = self.valves.default_limit if limit is None else int(limit)
        max_limit = max(1, min(int(self.valves.max_limit), 200))
        return max(1, min(requested, max_limit))

    def _api_url(self) -> str:
        return f"{self.valves.base_url.rstrip('/')}/api/v1/x/tweets/search"

    def _headers(self) -> dict[str, str]:
        key = self.valves.xquik_api_key.strip()
        headers = {
            "Accept": "application/json",
            "User-Agent": "OpenWebUI-XquikTweetSearch/1.0",
        }
        if key:
            headers["x-api-key"] = key
        return headers

    def _get_json(self, params: dict[str, str | int]) -> Any:
        cache_key = json.dumps(params, sort_keys=True)
        cached = self._cache_get(cache_key)
        if cached is not None:
            return cached

        response = requests.get(
            self._api_url(),
            headers=self._headers(),
            params=params,
            timeout=self.valves.request_timeout,
        )
        try:
            data = response.json()
        except ValueError:
            data = {
                "error": "invalid_response",
                "message": response.text[:300],
            }

        if response.status_code >= 400:
            message = data.get("message") if isinstance(data, dict) else None
            return {
                "error": data.get("error", "request_failed")
                if isinstance(data, dict)
                else "request_failed",
                "message": message or f"Xquik request failed with HTTP {response.status_code}.",
                "status_code": response.status_code,
            }

        self._cache_set(cache_key, data)
        return data

    async def _emit(
        self,
        event_emitter: Optional[Callable[[dict], Awaitable[None]]],
        description: str,
        done: bool = False,
        user: Optional[dict] = None,
    ) -> None:
        if event_emitter is None:
            return
        try:
            valves = (user or {}).get("valves")
            verbose = getattr(valves, "verbose_status", True) if valves is not None else True
        except Exception:
            verbose = True
        if not verbose and not done:
            return
        try:
            await event_emitter(
                {"type": "status", "data": {"description": description, "done": done}}
            )
        except Exception:
            pass

    @staticmethod
    def _user_valves(user: Optional[dict]) -> UserValves:
        valves = (user or {}).get("valves")
        if isinstance(valves, Tools.UserValves):
            return valves
        if isinstance(valves, dict):
            return Tools.UserValves(**valves)
        return Tools.UserValves()

    @staticmethod
    def _truncate_text(text: str, max_length: int) -> str:
        if max_length <= 0 or len(text) <= max_length:
            return text
        return f"{text[: max(0, max_length - 1)].rstrip()}..."

    def _compact_tweet(self, tweet: Any, user_valves: UserValves) -> dict[str, Any]:
        if not isinstance(tweet, dict):
            return {"raw": tweet}
        author = tweet.get("author")
        author_block = author if isinstance(author, dict) else {}
        compact: dict[str, Any] = {
            "id": tweet.get("id"),
            "text": self._truncate_text(str(tweet.get("text", "")), user_valves.max_text_length),
            "createdAt": tweet.get("createdAt"),
            "author": {
                "id": author_block.get("id"),
                "username": author_block.get("username"),
                "name": author_block.get("name"),
                "verified": author_block.get("verified"),
            },
        }
        if user_valves.include_metrics:
            compact["metrics"] = {
                "likeCount": tweet.get("likeCount"),
                "retweetCount": tweet.get("retweetCount"),
                "replyCount": tweet.get("replyCount"),
                "quoteCount": tweet.get("quoteCount"),
                "viewCount": tweet.get("viewCount"),
                "bookmarkCount": tweet.get("bookmarkCount"),
            }
        return compact

    def _compact_response(
        self,
        query: str,
        query_type: str,
        data: Any,
        user_valves: UserValves,
    ) -> dict[str, Any]:
        if not isinstance(data, dict):
            return {"query": query, "queryType": query_type, "raw": data}
        if "error" in data and "tweets" not in data:
            return data
        tweets = data.get("tweets", [])
        tweet_list = tweets if isinstance(tweets, list) else []
        return {
            "query": query,
            "queryType": query_type,
            "count": len(tweet_list),
            "has_next_page": bool(data.get("has_next_page", False)),
            "next_cursor": data.get("next_cursor", ""),
            "tweets": [
                self._compact_tweet(tweet, user_valves) for tweet in tweet_list
            ],
        }

    async def search_tweets(
        self,
        query: str,
        query_type: Literal["Latest", "Top"] = "Latest",
        limit: Optional[int] = None,
        cursor: str = "",
        since_time: str = "",
        until_time: str = "",
        __event_emitter__: Optional[Callable[[dict], Awaitable[None]]] = None,
        __user__: Optional[dict] = None,
    ) -> str:
        """
        Search X/Twitter posts with Xquik.

        Use this when the user asks to search posts, monitor a topic manually,
        inspect conversation snippets, find posts by keyword, or fetch a page
        of results with a cursor. The tool is read-only.
        """
        clean_query = query.strip()
        if clean_query == "":
            return "Enter a non-empty X/Twitter search query."
        if self.valves.xquik_api_key.strip() == "":
            return "Set xquik_api_key in the tool valves before searching tweets."

        normalized_limit = self._normalize_limit(limit)
        params: dict[str, str | int] = {
            "q": clean_query,
            "queryType": query_type,
            "limit": normalized_limit,
        }
        if cursor.strip():
            params["cursor"] = cursor.strip()
        if since_time.strip():
            params["sinceTime"] = since_time.strip()
        if until_time.strip():
            params["untilTime"] = until_time.strip()

        await self._emit(__event_emitter__, "Searching X/Twitter posts...", user=__user__)
        data = await anyio.to_thread.run_sync(lambda: self._get_json(params))
        await self._emit(__event_emitter__, "Xquik search complete.", done=True, user=__user__)

        user_valves = self._user_valves(__user__)
        compact = self._compact_response(clean_query, query_type, data, user_valves)
        return json.dumps(compact, indent=2, ensure_ascii=False)
