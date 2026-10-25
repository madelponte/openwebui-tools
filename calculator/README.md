# Calculator — Open WebUI Tool

A safe arithmetic calculator for Open WebUI. It evaluates a restricted Python expression AST rather than calling `eval`, and can render the result as an inline Rich UI card.

## Compatibility

- Open WebUI **0.11.0 or later**
- A model with native tool calling
- No third-party packages beyond those bundled with Open WebUI

## Installation

1. Go to **Workspace → Tools**.
2. Open **Create** and create a tool.
3. Paste the contents of `calculator.py`, then save it.
4. Add it to a model under **Workspace → Models → Tools**, or enable it per-chat from the composer’s **Integrations** menu.

Native function calling is the default in Open WebUI 0.11. If a model has an explicit override, use **Native**, not Legacy.

## Supported Expressions

- Operators: `+`, `-`, `*`, `/`, `//`, `%`, `**`; `^` is accepted as an exponent alias
- Constants: `pi`, `e`, `tau`, `inf`, `nan`
- Functions: `sqrt`, `cbrt`, `abs`, `round`, `floor`, `ceil`, `trunc`, `factorial`, `gcd`, `lcm`, `exp`, `log`, `log2`, `log10`, trigonometric and hyperbolic functions, `degrees`, `radians`, `hypot`, `pow`, `min`, `max`, and `sum`
- Lists and tuples as arguments to supported functions, such as `sum([1, 2, 3])`

The tool intentionally does not support symbolic algebra, equation solving, calculus, matrices, variable assignment, attribute access, imports, or arbitrary function calls.

## Rich UI Result

On success, Open WebUI displays an inline HTML card. The tool returns `(HTMLResponse, context)`, so Open WebUI 0.11 sends the structured expression and result back to the model while showing the card to the user.

On invalid input or calculation errors, it returns a plain-text error and finalizes the status event.

## Example Prompts

- “What is 17 * 23?”
- “Calculate sqrt(144) + log(100, 10).”
- “Compute sin(pi / 4).”
- “What is factorial(10)?”

## License

MIT
