# YouTube Transcript — Open WebUI Tool

Fetch the transcript (subtitles/captions) of a YouTube video so your model can summarize, quote, translate, or otherwise reason about its contents.

## Features

- Accepts any YouTube URL format (`youtube.com/watch`, `youtu.be`, `/shorts/`, `/embed/`, `/live/`, mobile) or a bare 11-character video ID
- No API key required — uses the free open-source [`youtube-transcript-api`](https://github.com/jdepoix/youtube-transcript-api) library
- Language priority list with automatic fallback to any available transcript
- Optional `[M:SS]` timestamps so the model can cite moments in the video
- Optional character limit to protect your context window
- Built-in support for Webshare residential proxies (for cloud-hosted deployments)
- Generic HTTP/SOCKS proxy support as a fallback (e.g. Tor)

## Installation

1. In Open WebUI, go to **Workspace → Tools → +** (Create new tool).
2. Paste in the contents of `youtube_transcript.py`.
3. Save. Open WebUI will auto-install the `youtube-transcript-api` dependency from the frontmatter.
4. Enable the tool on whichever model(s) you want to use it with.

## Usage

The model will call this tool automatically when the user pastes a YouTube link or asks about a video. Example prompts:

- "Summarize this video: https://youtu.be/dQw4w9WgXcQ"
- "What does this video say about machine learning? <url>"
- "Translate the key points from this YouTube clip: <url>"

## Configuration (Valves)

| Valve | Default | Purpose |
|---|---|---|
| `default_languages` | `en` | Comma-separated language codes to try in priority order (e.g. `en,en-US,es`) |
| `include_timestamps` | `false` | Prefix each line with `[M:SS]` or `[H:MM:SS]` |
| `max_characters` | `0` | Truncate transcript to N chars (`0` = no limit) |
| `webshare_proxy_username` | `""` | Webshare *Residential* proxy username (see below) |
| `webshare_proxy_password` | `""` | Webshare *Residential* proxy password |
| `http_proxy_url` | `""` | Generic proxy URL fallback, e.g. `socks5://127.0.0.1:9050` |

## ⚠️ Cloud server IP blocks

YouTube blocks most cloud-provider IPs (AWS, GCP, Azure, DigitalOcean, Hetzner, etc.). If you see `IpBlocked` or `RequestBlocked` errors, you have three options:

### Option 1 — Run Open WebUI on a residential connection
Easiest fix. Leave all proxy valves blank.

### Option 2 — Webshare residential proxies (recommended for cloud deployments)
1. Sign up at [webshare.io](https://www.webshare.io/).
2. Purchase a **"Residential"** package — **not** "Proxy Server" and **not** "Static Residential". Only plain Residential works.
3. Copy the proxy username and password from your Webshare dashboard.
4. Paste them into the `webshare_proxy_username` and `webshare_proxy_password` valves.

### Option 3 — Any other HTTP/SOCKS proxy
Paste a proxy URL into `http_proxy_url`, e.g.:
- `http://user:pass@host:port`
- `socks5://127.0.0.1:9050` (local Tor proxy)

If both Webshare valves are filled, those take priority. Otherwise `http_proxy_url` is used.

## Returned content

A short metadata header followed by the transcript text:

```
Transcript for YouTube video dQw4w9WgXcQ
Language: English (en) — auto-generated
Segments: 142
Source: https://www.youtube.com/watch?v=dQw4w9WgXcQ
---
We're no strangers to love You know the rules and so do I...
```

With `include_timestamps` enabled:

```
[0:00] We're no strangers to love
[0:04] You know the rules and so do I
...
```

## Error handling

The tool returns a friendly text message rather than crashing for:

- Invalid URLs or unrecognized video ID format
- Videos with subtitles disabled by the uploader
- Videos with no captions in any language
- Private, removed, or region-blocked videos
- YouTube IP blocks (with guidance on setting up the proxy valves)

## License

MIT