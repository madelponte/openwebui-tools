"""
title: YouTube Transcript
description: >
    Fetch the transcript (subtitles/captions) of a YouTube video so the model
    can summarize, quote, translate, or otherwise reason about its contents.
    Accepts a full YouTube URL (youtube.com/watch, youtu.be, /shorts/, /embed/, /live/)
    or a bare 11-character video ID. No API key required — uses the open-source
    youtube-transcript-api library which scrapes YouTube's public caption tracks.
    Example commands: "summarize this video: https://youtu.be/dQw4w9WgXcQ",
    "what does this video say about X: <url>", "transcript of <url>".
author: mdelponte
version: 1.0.1
license: MIT
requirements: youtube-transcript-api
"""

import anyio
import functools
import re
from typing import Optional, Awaitable, Callable, List
from urllib.parse import urlparse, parse_qs

from pydantic import BaseModel, Field

# Compatible with youtube-transcript-api >= 1.0.0
from youtube_transcript_api import YouTubeTranscriptApi
from youtube_transcript_api.proxies import WebshareProxyConfig, GenericProxyConfig
from youtube_transcript_api._errors import (
    TranscriptsDisabled,
    NoTranscriptFound,
    VideoUnavailable,
    RequestBlocked,
    IpBlocked,
)


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

_VIDEO_ID_RE = re.compile(r"^[A-Za-z0-9_-]{11}$")


def _extract_video_id(url_or_id: str) -> str:
    """
    Extract an 11-character YouTube video ID from a URL or pass through a bare ID.

    Supports:
      - https://www.youtube.com/watch?v=VIDEOID
      - https://youtu.be/VIDEOID
      - https://www.youtube.com/shorts/VIDEOID
      - https://www.youtube.com/embed/VIDEOID
      - https://www.youtube.com/live/VIDEOID
      - https://m.youtube.com/watch?v=VIDEOID
      - VIDEOID (already an 11-char ID)
    """
    s = (url_or_id or "").strip()
    if not s:
        raise ValueError("No URL or video ID provided.")

    # Already a bare video ID?
    if _VIDEO_ID_RE.match(s):
        return s

    # Add scheme if missing so urlparse behaves
    if not s.startswith(("http://", "https://")):
        s = "https://" + s

    parsed = urlparse(s)
    host = (parsed.hostname or "").lower().lstrip("www.")
    path = parsed.path or ""

    # youtu.be/<id>
    if host == "youtu.be":
        candidate = path.lstrip("/").split("/")[0]
        if _VIDEO_ID_RE.match(candidate):
            return candidate

    # youtube.com variants
    if host.endswith("youtube.com") or host == "youtube-nocookie.com":
        # /watch?v=<id>
        qs = parse_qs(parsed.query)
        if "v" in qs and qs["v"]:
            candidate = qs["v"][0]
            if _VIDEO_ID_RE.match(candidate):
                return candidate

        # /shorts/<id>, /embed/<id>, /live/<id>, /v/<id>
        m = re.match(r"^/(?:shorts|embed|live|v)/([A-Za-z0-9_-]{11})", path)
        if m:
            return m.group(1)

    raise ValueError(
        f"Could not extract a YouTube video ID from: {url_or_id!r}. "
        "Pass a full YouTube URL or an 11-character video ID."
    )


def _format_timestamp(seconds: float) -> str:
    """Format seconds as H:MM:SS or M:SS."""
    total = int(seconds)
    h = total // 3600
    m = (total % 3600) // 60
    s = total % 60
    if h:
        return f"{h}:{m:02d}:{s:02d}"
    return f"{m}:{s:02d}"


async def _emit(
    emitter: Optional[Callable[[dict], Awaitable[None]]],
    description: str,
    done: bool = False,
) -> None:
    if emitter is None:
        return
    await emitter(
        {
            "type": "status",
            "data": {"description": description, "done": done, "hidden": False},
        }
    )


# ---------------------------------------------------------------------------
# Tool
# ---------------------------------------------------------------------------

class Tools:
    def __init__(self):
        self.valves = self.Valves()
        # We return plain text to the LLM, no custom citations — leave default.

    class Valves(BaseModel):
        default_languages: str = Field(
            "en",
            description=(
                "Comma-separated language codes to try, in priority order "
                "(e.g. 'en,en-US,es'). The first available transcript wins. "
                "If none match, the tool falls back to any available language."
            ),
        )
        include_timestamps: bool = Field(
            False,
            description=(
                "If True, prefix each line with a [M:SS] or [H:MM:SS] timestamp. "
                "Useful when you want the model to cite moments in the video."
            ),
        )
        max_characters: int = Field(
            0,
            description=(
                "Truncate the returned transcript to at most this many characters "
                "(0 = no limit). Helps avoid blowing past the model's context window "
                "on very long videos."
            ),
        )
        webshare_proxy_username: str = Field(
            "",
            description=(
                "Optional. Webshare *Residential* proxy username. Set this AND "
                "webshare_proxy_password if your Open WebUI server runs on a "
                "cloud provider (AWS, GCP, Azure, DO, etc.) and YouTube is "
                "blocking your IP. Leave blank if running locally."
            ),
        )
        webshare_proxy_password: str = Field(
            "",
            description="Optional. Webshare Residential proxy password. See username field.",
        )
        http_proxy_url: str = Field(
            "",
            description=(
                "Optional. Generic HTTP/SOCKS proxy URL "
                "(e.g. 'http://user:pass@host:port' or 'socks5://127.0.0.1:9050'). "
                "Used only if the Webshare fields above are empty. Leave blank for no proxy."
            ),
        )

    # -------------------------------------------------------------------
    # Internal: build a configured YouTubeTranscriptApi client
    # -------------------------------------------------------------------
    def _build_client(self) -> YouTubeTranscriptApi:
        v = self.valves
        if v.webshare_proxy_username and v.webshare_proxy_password:
            return YouTubeTranscriptApi(
                proxy_config=WebshareProxyConfig(
                    proxy_username=v.webshare_proxy_username,
                    proxy_password=v.webshare_proxy_password,
                )
            )
        if v.http_proxy_url:
            return YouTubeTranscriptApi(
                proxy_config=GenericProxyConfig(
                    http_url=v.http_proxy_url,
                    https_url=v.http_proxy_url,
                )
            )
        return YouTubeTranscriptApi()

    # -------------------------------------------------------------------
    # Public tool method
    # -------------------------------------------------------------------
    async def get_youtube_transcript(
        self,
        url: str,
        languages: Optional[str] = None,
        __event_emitter__: Optional[Callable[[dict], Awaitable[None]]] = None,
    ) -> str:
        """
        Fetch the transcript / closed captions of a YouTube video and return it
        as plain text so you can summarize, quote, translate, or answer
        questions about its contents.

        USE THIS when the user:
          - Pastes a YouTube URL and asks anything about the video
          - Asks you to summarize, transcribe, or "tell me what this video says"
          - Wants to quote, search, or translate spoken content from a video
          - References a specific YouTube video by URL or 11-char video ID

        DO NOT use this for:
          - Generic web pages (use a web fetch/search tool instead)
          - Non-YouTube videos (Vimeo, TikTok, etc. — not supported)
          - Downloading audio/video files

        Notes for you, the model:
          - The transcript may be auto-generated and contain mis-transcriptions;
            treat exact wording with mild skepticism but the substance is reliable.
          - If the transcript is long, prefer summarizing or extracting the parts
            relevant to the user's question rather than dumping it back verbatim.

        :param url: A YouTube URL (youtube.com/watch, youtu.be, /shorts/, /embed/,
                    /live/) or a bare 11-character video ID.
        :param languages: Optional comma-separated language codes to prefer
                          (e.g. "en,es"). Overrides the default from valves
                          for this single call. If none match, the tool falls
                          back to any available transcript.
        :return: The transcript as a single string (optionally with timestamps),
                 prefixed by a short metadata header. On error, a short message
                 describing what went wrong.
        """
        try:
            await _emit(__event_emitter__, "🔎 Parsing YouTube URL…")
            video_id = _extract_video_id(url)

            # Resolve language preference list
            lang_str = languages if languages else self.valves.default_languages
            lang_list: List[str] = [
                code.strip() for code in lang_str.split(",") if code.strip()
            ] or ["en"]

            await _emit(
                __event_emitter__,
                f"📜 Fetching transcript for {video_id}…",
            )

            client = self._build_client()

            # Try preferred languages first, then fall back to anything available.
            try:
                fetched = await anyio.to_thread.run_sync(
                    functools.partial(client.fetch, video_id, languages=lang_list)
                )
            except NoTranscriptFound:
                await _emit(
                    __event_emitter__,
                    "↪️ Preferred languages unavailable, trying any language…",
                )
                transcript_list = await anyio.to_thread.run_sync(
                    functools.partial(client.list, video_id)
                )
                # Pick the first available transcript (manually-created preferred
                # by find_transcript ordering, but we just iterate).
                any_transcript = None
                for t in transcript_list:
                    any_transcript = t
                    break
                if any_transcript is None:
                    raise
                fetched = await anyio.to_thread.run_sync(any_transcript.fetch)

            # fetched is a FetchedTranscript (iterable of FetchedTranscriptSnippet)
            snippets = list(fetched)
            if not snippets:
                await _emit(__event_emitter__, "⚠️ Transcript was empty.", done=True)
                return f"❌ The transcript for video {video_id} is empty."

            # Detect language of the result for the header (best-effort)
            language = getattr(fetched, "language", None) or "unknown"
            language_code = getattr(fetched, "language_code", None) or "?"
            is_generated = getattr(fetched, "is_generated", None)
            kind = (
                "auto-generated"
                if is_generated
                else ("manually-created" if is_generated is False else "unknown source")
            )

            # Build the body
            include_ts = self.valves.include_timestamps
            lines: List[str] = []
            for snip in snippets:
                text = (snip.text or "").replace("\n", " ").strip()
                if not text:
                    continue
                if include_ts:
                    ts = _format_timestamp(snip.start)
                    lines.append(f"[{ts}] {text}")
                else:
                    lines.append(text)

            body = "\n".join(lines) if include_ts else " ".join(lines)

            # Optional truncation
            truncated_note = ""
            max_chars = self.valves.max_characters
            if max_chars and len(body) > max_chars:
                body = body[:max_chars].rsplit(" ", 1)[0] + " …"
                truncated_note = (
                    f"\n\n[Note: transcript truncated to {max_chars} characters "
                    "by tool configuration.]"
                )

            header = (
                f"Transcript for YouTube video {video_id}\n"
                f"Language: {language} ({language_code}) — {kind}\n"
                f"Segments: {len(snippets)}\n"
                f"Source: https://www.youtube.com/watch?v={video_id}\n"
                "---"
            )

            await _emit(__event_emitter__, "✅ Transcript fetched.", done=True)
            return f"{header}\n{body}{truncated_note}"

        except ValueError as ve:
            msg = f"❌ {ve}"
            await _emit(__event_emitter__, msg, done=True)
            return msg
        except TranscriptsDisabled:
            msg = "❌ This video has subtitles/transcripts disabled by the uploader."
            await _emit(__event_emitter__, msg, done=True)
            return msg
        except NoTranscriptFound:
            msg = (
                "❌ No transcript was found for this video in any language. "
                "It may not have captions at all."
            )
            await _emit(__event_emitter__, msg, done=True)
            return msg
        except VideoUnavailable:
            msg = "❌ This video is unavailable (private, removed, or region-blocked)."
            await _emit(__event_emitter__, msg, done=True)
            return msg
        except (RequestBlocked, IpBlocked):
            msg = (
                "❌ YouTube is blocking requests from this server's IP address. "
                "This is common when Open WebUI runs on a cloud provider "
                "(AWS, GCP, Azure, DigitalOcean, etc.). Configure a residential "
                "proxy in this tool's valves (Webshare username/password, or a "
                "generic HTTP/SOCKS proxy URL) to work around it."
            )
            await _emit(__event_emitter__, "❌ IP blocked by YouTube.", done=True)
            return msg
        except Exception as exc:
            msg = f"❌ Error fetching transcript: {type(exc).__name__}: {exc}"
            await _emit(__event_emitter__, msg, done=True)
            return msg