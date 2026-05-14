# Wolfram Alpha — OpenWebUI Tool

An OpenWebUI tool that lets your LLM run computations and look up factual data through the [Wolfram Alpha LLM API](https://products.wolframalpha.com/llm-api/documentation).

## What it does

Wolfram Alpha is good at things LLMs are bad at: exact math, unit conversions, scientific data, and authoritative reference lookups. This tool gives the model one endpoint to query for all of it.

Covers:

- **Math** — algebra, calculus, linear algebra, statistics, number theory
- **Unit & currency conversions**
- **Physics & chemistry** — constants, formulas, properties, reactions
- **Astronomy** — planetary positions, eclipses, distances
- **Geography & demographics** — populations, GDP, distances between cities
- **Dates & times** — timezone conversion, day of week, durations
- **Finance** — stock data, historical prices
- **Nutrition, weather history, linguistics**, and structured comparisons of named entities

## Installation

1. Get a free AppID at [developer.wolframalpha.com](https://developer.wolframalpha.com).
2. In OpenWebUI, go to **Workspace → Tools → Create New Tool** (or import).
3. Paste in the contents of `wolfram_alpha.py`.
4. Save, then open the tool's valve settings and paste your AppID into `app_id`.
5. Enable the tool in any chat that uses a model with native tool-calling.

## Valves

| Valve | Default | Description |
|---|---|---|
| `app_id` | `""` | Your Wolfram Alpha AppID. **Required.** |
| `default_units` | `"metric"` | `metric` or `nonmetric`. Applied to every query. |
| `max_chars` | `6800` | Cap on Wolfram's response length. |
| `render_card` | `True` | If `True`, renders results as an inline HTML card *and* sends text to the LLM. If `False`, returns plain text only — the LLM formats it. |

## Returns

- **Success (with `render_card=True`):** an HTML card rendered inline in chat, plus the raw Wolfram response sent to the LLM so it can reason about the result.
- **Success (with `render_card=False`):** the raw Wolfram response as plain text.
- **Uninterpretable query (HTTP 501):** the model receives Wolfram's suggested rephrasings so it can retry intelligently.
- **Config or network errors:** plain-string error messages.

## How the model uses it

The tool's docstring instructs the model to:

- Send English keyword-style queries (`France population`, not `how many people live in France`)
- Use single-letter variable names (`n`, `x`, `n_1`)
- Use exponent notation like `6*10^14`, never `6e14`
- Use named constants (`speed of light`) rather than substituting numbers
- Make separate calls per property
- Re-send the same input with the `assumption` parameter when Wolfram offers disambiguation options

These constraints come directly from Wolfram's [recommended prompt](https://products.wolframalpha.com/llm-api/documentation) for LLM clients.

## Requirements

- Python 3.10+ (compatible with OpenWebUI's 3.10/3.11 runtime)
- `httpx` (installed automatically via tool frontmatter, or pre-install in production)

## License

MIT