# Xquik Tweet Search - Open WebUI Tool

Search X/Twitter posts from Open WebUI through the public Xquik REST API.

The tool is read-only. It calls `GET /api/v1/x/tweets/search`, returns compact JSON, and keeps the API key in an Open WebUI valve.

## Features

- Search by keyword, account query, Tweet ID, or X status URL
- Choose `Latest` or `Top` sorting
- Use cursors for follow-up pages
- Optional `since_time` and `until_time` filters
- Compact tweet output with author metadata and optional metrics
- Short in-memory caching for repeated prompts

## Installation

1. In Open WebUI, go to **Workspace -> Tools -> +**.
2. Paste in the contents of `xquik_tweet_search.py`.
3. Save the tool.
4. Set the `xquik_api_key` valve.
5. Enable the tool for the models that should search X/Twitter posts.

Open WebUI installs the `requests` dependency from the metadata header. For pinned deployments, add it to your container image:

```dockerfile
FROM ghcr.io/open-webui/open-webui:main
RUN pip install --no-cache-dir requests
```

## Configuration

| Valve | Default | Purpose |
|---|---|---|
| `xquik_api_key` | `""` | Xquik API key, sent as the `x-api-key` header |
| `base_url` | `https://xquik.com` | Xquik API base URL |
| `request_timeout` | `30` | HTTP timeout in seconds |
| `default_limit` | `20` | Default maximum tweets to return |
| `max_limit` | `50` | Maximum tweets this tool will request at once |
| `cache_ttl_seconds` | `30` | Cache identical searches for this many seconds |

## Methods

### `search_tweets(query, query_type="Latest", limit=None, cursor="", since_time="", until_time="")`

Returns JSON with:

- `query`
- `queryType`
- `count`
- `has_next_page`
- `next_cursor`
- `tweets`

Each tweet includes its ID, text, creation time, author metadata, and metrics when enabled.

## Example prompts

- "Search X for posts about open source agent frameworks."
- "Find recent tweets from @xquik."
- "Search Top posts for 'Open WebUI tools'."
- "Use this cursor to get the next page: `<cursor>`."

## Privacy

The tool sends the search parameters to Xquik. It does not send unrelated chat content or user identifiers. API keys are stored by Open WebUI as tool valves and are never returned to the model.

## License

MIT.
