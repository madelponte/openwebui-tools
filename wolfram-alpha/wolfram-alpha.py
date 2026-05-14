"""
title: Wolfram Alpha
description: >
    Run computations and look up factual data via the Wolfram Alpha LLM API.
    Handles math, unit conversions, science, geography, finance, dates, and more.
author: mdelponte
version: 1.0.0
license: MIT
requirements: httpx
"""

import re
import html
import httpx
from typing import Optional, Awaitable, Callable, Tuple
from pydantic import BaseModel, Field
from fastapi.responses import HTMLResponse


BASE_URL = "https://www.wolframalpha.com/api/v1/llm-api"


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


def _parse_sections(text: str) -> list:
    """
    The LLM API returns plain text with section headers like 'Result:' followed
    by content lines. Parse into [(header, body), ...] for nicer card rendering.
    """
    sections: list = []
    current_header: Optional[str] = None
    current_lines: list = []

    for raw_line in text.splitlines():
        line = raw_line.rstrip()
        # A section header looks like "Something:" on its own line.
        if re.match(r"^[A-Z][^:]{0,80}:$", line):
            if current_header is not None:
                sections.append((current_header, "\n".join(current_lines).strip()))
            current_header = line.rstrip(":")
            current_lines = []
        else:
            if current_header is None:
                # Preamble before the first header — treat as "Query" body
                current_header = "Query"
            current_lines.append(line)

    if current_header is not None:
        sections.append((current_header, "\n".join(current_lines).strip()))

    return sections


def _extract_images(body: str) -> Tuple[str, list]:
    """
    Pull `image: <url>` lines out of a section body. Returns (text_without_images, [urls]).
    """
    urls = []
    text_lines = []
    for line in body.splitlines():
        m = re.match(r"\s*image:\s*(https?://\S+)\s*$", line)
        if m:
            urls.append(m.group(1))
        else:
            text_lines.append(line)
    return "\n".join(text_lines).strip(), urls


def _extract_wolfram_link(text: str) -> Optional[str]:
    m = re.search(r"https://www\.wolframalpha\.com/input\?i=\S+", text)
    return m.group(0) if m else None


def _build_card(query: str, raw_text: str) -> str:
    """Render the Wolfram Alpha response as an inline HTML card."""
    sections = _parse_sections(raw_text)
    wolfram_link = _extract_wolfram_link(raw_text)

    safe_query = html.escape(query)

    section_html_parts = []
    for header, body in sections:
        if not body:
            continue
        # Skip the redundant trailing link section — we render it as a footer button.
        if "wolframalpha.com/input" in body and len(body.splitlines()) <= 2:
            continue

        body_text, image_urls = _extract_images(body)
        safe_header = html.escape(header)
        safe_body = html.escape(body_text)

        images_html = ""
        if image_urls:
            img_tags = "".join(
                f'<img src="{html.escape(u)}" loading="lazy" alt="{safe_header}">'
                for u in image_urls
            )
            images_html = f'<div class="imgs">{img_tags}</div>'

        body_block = f'<pre>{safe_body}</pre>' if safe_body else ""

        section_html_parts.append(
            f'<section><h3>{safe_header}</h3>{body_block}{images_html}</section>'
        )

    sections_html = "\n".join(section_html_parts) or (
        f'<section><pre>{html.escape(raw_text)}</pre></section>'
    )

    footer_html = ""
    if wolfram_link:
        safe_link = html.escape(wolfram_link)
        footer_html = (
            f'<a class="footer-link" href="{safe_link}" target="_blank" rel="noopener">'
            f'Open on Wolfram Alpha →</a>'
        )

    return f"""<!DOCTYPE html>
<html lang="en">
<head>
<meta charset="utf-8">
<meta name="viewport" content="width=device-width,initial-scale=1">
<style>
*,*::before,*::after{{box-sizing:border-box;margin:0;padding:0}}
html,body{{background:transparent;font-family:-apple-system,BlinkMacSystemFont,'Segoe UI',Roboto,sans-serif;color:#e6edf3;padding:6px;}}
.card{{
  max-width:760px;
  background:#161b22;
  border:1px solid rgba(255,255,255,0.08);
  border-radius:12px;
  overflow:hidden;
}}
.header{{
  display:flex;align-items:center;gap:10px;
  padding:12px 16px;
  background:linear-gradient(135deg,#dc2626 0%,#ea580c 100%);
  color:#fff;
}}
.header .logo{{
  font-weight:700;font-size:14px;letter-spacing:0.5px;
  opacity:0.9;
}}
.header .query{{
  font-size:13px;opacity:0.95;margin-left:auto;
  text-align:right;max-width:60%;
  overflow:hidden;text-overflow:ellipsis;white-space:nowrap;
}}
.body{{padding:6px 16px 14px;}}
section{{
  padding:12px 0;
  border-bottom:1px solid rgba(255,255,255,0.06);
}}
section:last-of-type{{border-bottom:none;}}
section h3{{
  font-size:11px;
  text-transform:uppercase;
  letter-spacing:0.6px;
  color:#8b949e;
  margin-bottom:6px;
  font-weight:600;
}}
section pre{{
  font-family:'SF Mono',Menlo,Consolas,monospace;
  font-size:13px;
  line-height:1.5;
  color:#e6edf3;
  white-space:pre-wrap;
  word-break:break-word;
}}
.imgs{{
  display:flex;flex-wrap:wrap;gap:8px;
  margin-top:8px;
}}
.imgs img{{
  max-width:100%;
  background:#fff;
  border-radius:6px;
  padding:4px;
  border:1px solid rgba(255,255,255,0.08);
}}
.footer-link{{
  display:block;
  padding:10px 16px;
  background:rgba(220,38,38,0.1);
  color:#f87171;
  text-decoration:none;
  font-size:12px;
  font-weight:500;
  text-align:center;
  border-top:1px solid rgba(255,255,255,0.06);
}}
.footer-link:hover{{background:rgba(220,38,38,0.18);}}
</style>
</head>
<body>
<div class="card">
  <div class="header">
    <span class="logo">WOLFRAM|ALPHA</span>
    <span class="query">{safe_query}</span>
  </div>
  <div class="body">
    {sections_html}
  </div>
  {footer_html}
</div>
<script>
function reportHeight() {{
  const h = document.documentElement.scrollHeight;
  parent.postMessage({{type: 'iframe:height', height: h}}, '*');
}}
window.addEventListener('load', reportHeight);
if (typeof ResizeObserver !== 'undefined') {{
  new ResizeObserver(reportHeight).observe(document.body);
}}
</script>
</body>
</html>"""


def _build_error_card(query: str, message: str, suggestions: Optional[str] = None) -> str:
    safe_query = html.escape(query)
    safe_message = html.escape(message)
    suggestions_html = ""
    if suggestions:
        suggestions_html = (
            f'<section><h3>Suggestions from Wolfram</h3>'
            f'<pre>{html.escape(suggestions)}</pre></section>'
        )

    return f"""<!DOCTYPE html>
<html lang="en">
<head>
<meta charset="utf-8">
<meta name="viewport" content="width=device-width,initial-scale=1">
<style>
*,*::before,*::after{{box-sizing:border-box;margin:0;padding:0}}
html,body{{background:transparent;font-family:-apple-system,BlinkMacSystemFont,'Segoe UI',sans-serif;color:#e6edf3;padding:6px;}}
.card{{max-width:760px;background:#161b22;border:1px solid rgba(248,113,113,0.3);border-radius:12px;overflow:hidden;}}
.header{{padding:12px 16px;background:linear-gradient(135deg,#7f1d1d 0%,#991b1b 100%);color:#fff;display:flex;align-items:center;gap:10px;}}
.header .logo{{font-weight:700;font-size:14px;}}
.header .query{{font-size:13px;opacity:0.9;margin-left:auto;max-width:60%;overflow:hidden;text-overflow:ellipsis;white-space:nowrap;}}
.body{{padding:6px 16px 14px;}}
section{{padding:12px 0;border-bottom:1px solid rgba(255,255,255,0.06);}}
section:last-of-type{{border-bottom:none;}}
section h3{{font-size:11px;text-transform:uppercase;letter-spacing:0.6px;color:#8b949e;margin-bottom:6px;font-weight:600;}}
section pre{{font-family:'SF Mono',Menlo,Consolas,monospace;font-size:13px;line-height:1.5;white-space:pre-wrap;word-break:break-word;}}
.msg{{color:#fca5a5;}}
</style>
</head>
<body>
<div class="card">
  <div class="header">
    <span class="logo">WOLFRAM|ALPHA</span>
    <span class="query">{safe_query}</span>
  </div>
  <div class="body">
    <section><h3>No result</h3><pre class="msg">{safe_message}</pre></section>
    {suggestions_html}
  </div>
</div>
<script>
function reportHeight() {{
  const h = document.documentElement.scrollHeight;
  parent.postMessage({{type: 'iframe:height', height: h}}, '*');
}}
window.addEventListener('load', reportHeight);
if (typeof ResizeObserver !== 'undefined') {{
  new ResizeObserver(reportHeight).observe(document.body);
}}
</script>
</body>
</html>"""


class Tools:
    def __init__(self):
        self.valves = self.Valves()
        self.citation = False

    class Valves(BaseModel):
        app_id: str = Field(
            "",
            description="Your Wolfram Alpha AppID. Get one free at https://developer.wolframalpha.com",
        )
        default_units: str = Field(
            "metric",
            description="Default unit system: 'metric' or 'nonmetric'.",
        )
        max_chars: int = Field(
            6800,
            description="Max characters in Wolfram's response (default 6800).",
        )

    async def query_wolfram_alpha(
        self,
        query: str,
        assumption: Optional[str] = None,
        __event_emitter__: Optional[Callable[[dict], Awaitable[None]]] = None,
    ) -> "Tuple[HTMLResponse, str] | str":
        """
        Compute or look up factual data using Wolfram Alpha. Use this for anything
        requiring exact computation or authoritative reference data, including:
        math (algebra, calculus, linear algebra, statistics, number theory),
        unit/currency conversions, physics & chemistry (constants, formulas,
        properties, reactions), astronomy (planetary positions, eclipses, distances),
        geography & demographics (populations, GDP, distances between cities),
        dates & times (timezone conversion, day of week, time between dates),
        finance (stock data, historical prices), nutrition, weather history,
        words & linguistics, and structured comparisons of named entities.

        Query formatting (important):
        - Send English keyword-style queries, not full sentences:
          "France population" not "how many people live in France".
        - Use single-letter variable names (n, n_1, x) in math.
        - Use exponent notation like 6*10^14, never 6e14.
        - Use named constants ("speed of light") rather than substituting numbers.
        - One property per call — make separate calls for separate properties.
        - If a previous result returned 'Assumptions', re-send the SAME input
          with the assumption parameter set to the relevant value, do not rephrase.

        Do NOT use for: opinions, current news, code generation, creative writing,
        or simple lookups already answerable from general knowledge.

        :param query: The Wolfram Alpha query (English, keyword-style, single line).
        :param assumption: Optional assumption value from a previous result, used
            to disambiguate (e.g. when "mercury" could mean the planet or element).
        :return: A rendered card plus the text result for the model to reference.
        """
        app_id = (self.valves.app_id or "").strip()
        if not app_id:
            msg = (
                "❌ Wolfram Alpha AppID is not configured. "
                "An admin must set the `app_id` valve. "
                "Get a free AppID at https://developer.wolframalpha.com"
            )
            await _emit(__event_emitter__, msg, done=True)
            return msg

        if not query or not query.strip():
            msg = "❌ Empty query. Provide a Wolfram Alpha query string."
            await _emit(__event_emitter__, msg, done=True)
            return msg

        clean_query = query.strip().replace("\n", " ")

        params = {
            "input": clean_query,
            "appid": app_id,
            "maxchars": self.valves.max_chars,
            "units": self.valves.default_units,
        }
        if assumption:
            params["assumption"] = assumption

        await _emit(__event_emitter__, f"🔢 Asking Wolfram Alpha: {clean_query}")

        try:
            async with httpx.AsyncClient(timeout=30) as client:
                response = await client.get(
                    BASE_URL,
                    params=params,
                    headers={"User-Agent": "OpenWebUI-WolframAlpha/1.0"},
                )
        except httpx.TimeoutException:
            msg = "❌ Wolfram Alpha request timed out after 30s."
            await _emit(__event_emitter__, msg, done=True)
            return msg
        except httpx.HTTPError as exc:
            msg = f"❌ Network error contacting Wolfram Alpha: {exc}"
            await _emit(__event_emitter__, msg, done=True)
            return msg

        status = response.status_code
        body = response.text or ""

        # 501: Wolfram couldn't interpret the input. Body often has suggestions.
        if status == 501:
            await _emit(__event_emitter__, "⚠️ Wolfram could not interpret the query", done=True)
            error_card = _build_error_card(
                clean_query,
                "Wolfram Alpha could not interpret this input.",
                suggestions=body.strip() if body.strip() else None,
            )
            text_for_llm = (
                f"Wolfram Alpha could not interpret the query: '{clean_query}'.\n"
                f"Suggestions from the API:\n{body.strip()}\n\n"
                "Try rephrasing as a simpler keyword-style query, or pick one of "
                "the suggested inputs above."
            )
            return (
                HTMLResponse(content=error_card, headers={"content-disposition": "inline"}),
                text_for_llm,
            )

        if status == 403:
            msg = (
                "❌ Wolfram Alpha rejected the AppID (HTTP 403). "
                "Check that the `app_id` valve is set correctly."
            )
            await _emit(__event_emitter__, msg, done=True)
            return msg

        body_snippet = body[:200]
        if status == 400:
            msg = f"❌ Wolfram Alpha rejected the request (HTTP 400): {body_snippet}"
            await _emit(__event_emitter__, msg, done=True)
            return msg

        if status >= 400:
            msg = f"❌ Wolfram Alpha error (HTTP {status}): {body_snippet}"
            await _emit(__event_emitter__, msg, done=True)
            return msg

        if not body.strip():
            msg = "❌ Wolfram Alpha returned an empty response."
            await _emit(__event_emitter__, msg, done=True)
            return msg

        await _emit(__event_emitter__, "✅ Got result from Wolfram Alpha", done=True)

        card_html = _build_card(clean_query, body)
        # Return both: the rendered card AND the raw text for the LLM to read.
        return (
            HTMLResponse(content=card_html, headers={"content-disposition": "inline"}),
            body,
        )