# Open WebUI Tools

Workspace Tools and a Deep Research Pipe for [Open WebUI](https://github.com/open-webui/open-webui).

The maintained in-process plugins in this repository target **Open WebUI 0.11.0 or later**:

- `agentic_web_search/` — SearXNG search and resilient page fetching
- `ask_clarifying_questions/` — interactive clarification prompts
- `calculator/` — safe arithmetic with an optional Rich UI result card
- `deep_research_pipe/` — multi-cycle, citation-grounded research reports
- `stock_data/` — stock quotes, profiles, financials, earnings, news, and recommendations
- `unit_converter/` — length, weight, temperature, and currency conversion
- `wolfram-alpha/` — Wolfram Alpha LLM API queries
- `youtube_transcript/` — YouTube caption/transcript extraction

See each directory’s README for installation, dependencies, valves, and usage. Workspace Tools run arbitrary Python inside the Open WebUI server; review them before installation and restrict Workspace permissions to trusted administrators.
