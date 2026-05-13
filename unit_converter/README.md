# Unit Converter — OpenWebUI Tool

A lightweight unit conversion tool for [Open WebUI](https://github.com/open-webui/open-webui). Converts **length**, **weight**, **temperature**, and **currency** with a single tool call. Designed for models with native tool calling and a minimal context footprint.

## Features

- Single `convert(value, from_unit, to_unit)` method — one function schema, not four
- Plain-text output so the model can weave results naturally into its reply
- Forgiving aliases: `miles`, `Kilometers`, `lbs`, `°C`, `tonnes`, plurals, etc.
- Offline tables for length/weight/temperature (no API required)
- Currency via [exchangerate.host](https://exchangerate.host) (free tier)

## Installation

1. In Open WebUI, go to **Workspace → Tools → +**.
2. Paste the contents of `unit_converter.py`.
3. Save. The `httpx` dependency listed in the frontmatter installs automatically on most setups (see the production note below if you run with multiple workers).

## Configuration

Currency conversion needs a free access key from [exchangerate.host](https://exchangerate.host).

1. Sign up for a free account and grab your access key.
2. In Open WebUI, open the tool's settings (the gear icon next to the tool).
3. Paste the key into the **`exchangerate_access_key`** valve.

Length, weight, and temperature work without any configuration.

## Supported Units

### Length
`mm`, `cm`, `m`, `km`, `in`, `ft`, `yd`, `mi`, `nmi` (nautical mile)

Aliases: `millimeter`, `centimeter`, `meter`, `kilometer`, `inch`, `foot`/`feet`, `yard`, `mile`, plus British spellings (`metre`, `kilometre`, etc.) and plurals.

### Weight
`mg`, `g`, `kg`, `t` (metric tonne), `oz`, `lb`, `st` (stone), `ton_us` (short ton), `ton_uk` (long ton)

Aliases: `gram`, `kilogram`/`kilo`, `tonne`, `ounce`, `pound`/`lbs`, `stone`, plus plurals.

> Plain `ton` defaults to **metric tonne**. Use `ton_us` / `short ton` or `ton_uk` / `long ton` to disambiguate.

### Temperature
`C`, `F`, `K`, `R` (Celsius, Fahrenheit, Kelvin, Rankine) — full names and `°C`/`°F` symbols also work.

### Currency
Any [ISO 4217](https://en.wikipedia.org/wiki/ISO_4217) 3-letter code: `USD`, `EUR`, `GBP`, `JPY`, `CAD`, `AUD`, `CHF`, `CNY`, `INR`, etc. Rates are fetched live from exchangerate.host.

## Example Prompts

- "Convert 5 miles to km"
- "How many pounds is 2.5 kg?"
- "350°F in Celsius"
- "100 USD to EUR"
- "What's 1 stone in pounds?"

## Output

The tool returns a short string like:

```
2.5 kg = 5.5116 lbs
350 F = 176.67 C
100 USD = 92.34 EUR
```

The model integrates this into its reply naturally.

## Notes & Caveats

- **`nm` means nautical mile**, not nanometer. If you need nanometer support, let me know — the alias table can be adjusted (with the collision resolved by picking a different shorthand).
- **Category mismatches fail cleanly**: trying to convert kg → miles returns an error message instead of guessing.
- **Currency requires the access key**. Without it, currency conversions return a helpful error; everything else still works.
- **Temperature uses exact constants**: 0°C = 273.15 K, -40°C = -40°F, 100°C = 212°F all round-trip correctly.

## Production Deployments

If running Open WebUI with `UVICORN_WORKERS > 1`, the runtime `pip install` of `httpx` from the tool's frontmatter can race. Either:

- Set `ENABLE_PIP_INSTALL_FRONTMATTER_REQUIREMENTS=False` and pre-install `httpx` in your Docker image, or
- Stick with a single worker.

`httpx` is also a common dependency, so it may already be installed.

## License

MIT