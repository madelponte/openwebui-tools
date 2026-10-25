# AGENTS.md

## Repository overview

This repository contains standalone Python plugins for Open WebUI. The maintained target is **Open WebUI 0.11.0 or later**.

- `agentic_web_search/` — SearXNG search and page/PDF retrieval tool
- `ask_clarifying_questions/` — interactive clarification tool
- `calculator/` — safe arithmetic tool with an optional Rich UI card
- `deep_research_pipe/` — multi-cycle research pipe
- `stock_data/` — market-data tool
- `unit_converter/` — unit and currency conversion tool
- `wolfram-alpha/` — Wolfram Alpha LLM API tool
- `youtube_transcript/` — YouTube transcript tool

Each directory contains one installable Python file and a README. There is no shared Python package or application build: users normally paste or import each Python file individually in Open WebUI.

## Source and compatibility rules

- Keep every plugin self-contained. Do not introduce imports from another repository directory or require users to install the whole repository.
- Preserve the Open WebUI frontmatter docstring at the top of each plugin.
- New and updated plugins must declare `required_open_webui_version: 0.11.0` unless the repository's supported baseline is deliberately changed everywhere.
- Keep frontmatter `requirements` comma-separated and include non-standard runtime dependencies there. Open WebUI installs these requirements when plugin dependency installation is enabled.
- Bump the plugin frontmatter version when behavior or compatibility changes.
- Treat Open WebUI 0.11.0 APIs as the compatibility source of truth. Do not restore removed APIs such as `request.app.state.config`.
- Keep the corresponding directory README and the root tool list accurate when changing behavior, dependencies, valves, or installation steps.

## Plugin conventions

### Workspace tools

Workspace tools expose a `Tools` class. Public callable methods become model-visible tool methods; prefix helpers with `_` so Open WebUI does not expose them in the generated schema.

Use Open WebUI reserved arguments only when needed, including:

- `__event_emitter__` for status, citation, and replace events
- `__event_call__` for interactive client requests
- `__user__` for user information and per-user valves
- `__request__`, `__metadata__`, and `__model__` for request context

Reserved arguments must retain their double-underscore names. They are removed from the model-facing schema and injected by Open WebUI.

### Pipes

`deep_research_pipe/deep_research_pipe.py` exposes a `Pipe` class rather than a `Tools` class. Internal model requests must use a real configured model ID and must not select the Deep Research pipe recursively.

For request-level configuration, use the asynchronous Open WebUI 0.11 configuration APIs. Prefer `__metadata__["user_prompt"]` when the visible message content may have been wrapped by retrieval middleware.

### Valves and credentials

- Global configuration belongs in a Pydantic `Valves` model.
- Per-user configuration belongs in `UserValves` where supported.
- Mark secrets with `json_schema_extra={"input_type": "password"}`.
- Never include API keys, proxy credentials, tokens, or full authenticated URLs in logs, events, exceptions returned to users, or documentation examples.

## Async and event behavior

Plugin methods that perform network or interactive work should be `async`.

- Use async clients for HTTP requests where practical.
- Offload blocking third-party SDK calls with `anyio.to_thread.run_sync` or `asyncio.to_thread`; do not block Open WebUI's event loop.
- Treat `__event_emitter__` and `__event_call__` as optional.
- Event delivery can fail when the browser disconnects. Status/citation emission should not turn otherwise successful work into a failure.
- Interactive calls can time out, raise an exception, or return `{"error": "Client session disconnected."}`. Handle those cases explicitly.
- Every operation that emits progress statuses should emit a terminal status with `done: true` on success and handled failure paths.

Current citation events should provide `document`, `metadata`, and `source` fields. Include stable source names and URLs when available. Keep large documents out of event payloads by emitting a useful excerpt.

## Return values and Rich UI

Normal tools may return strings or JSON-serializable structured data. Open WebUI 0.11 Rich UI tools may return:

```python
(
    HTMLResponse(
        content=html,
        headers={"Content-Disposition": "inline"},
    ),
    model_context,
)
```

The second tuple value must contain useful text or structured context for the model; do not return only a visual card. Escape all untrusted values before inserting them into HTML.

Pipes should return their final assistant content. Replace events can still be used to display transient progress, but should not be the only delivery mechanism for the final result.

## Security and reliability

- Do not add `eval` or `exec` for user-controlled expressions. The calculator intentionally evaluates a restricted AST.
- Validate URLs, symbols, units, model IDs, and other external inputs before use.
- Set explicit network timeouts and provide actionable provider errors without exposing secrets.
- Preserve citation/source provenance through search and research flows.
- Avoid broad behavioral rewrites when making a compatibility fix.
- Preserve unrelated working-tree changes. In particular, never revert files simply because they were modified before the current task.

## Validation

There is currently no repository-wide automated test suite. At minimum, run:

```bash
python3 -m py_compile \
  agentic_web_search/agentic_web_search.py \
  ask_clarifying_questions/ask_clarifying_questions.py \
  calculator/calculator.py \
  deep_research_pipe/deep_research_pipe.py \
  stock_data/stock_data_tool.py \
  unit_converter/unit_converter.py \
  wolfram-alpha/wolfram-alpha.py \
  youtube_transcript/youtube_transcript.py

git diff --check
```

Also perform focused offline or mocked tests for changed behavior. Local environments may not contain Open WebUI or every frontmatter dependency, so small import stubs are acceptable for isolated tests. Do not make live paid/authenticated API calls unless the user explicitly requests them and provides a safe test setup.

Before finishing:

1. Inspect `git status --short` and preserve unrelated modifications.
2. Review the complete diff for credential leaks and outdated documentation.
3. Confirm changed Python files still end with a newline.
4. Report which checks ran and which provider integrations were not live-tested.
