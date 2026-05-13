"""
title: Calculator
description: >
    Evaluates mathematical expressions and returns the result.
    Supports arithmetic (+, -, *, /, //, %, **), parentheses, and common math
    functions like sqrt, sin, cos, tan, log, exp, abs, round, floor, ceil,
    factorial, plus the constants pi and e.
    Example commands:
      - "What is 2 + 2?"
      - "Calculate (15 * 23) / 7"
      - "What's sqrt(144) + log(100, 10)?"
      - "Compute sin(pi / 4)"
      - "What is 5 factorial?"
author: mdelponte
version: 1.0.0
license: MIT
"""

import ast
import math
import operator
from typing import Optional, Awaitable, Callable
from fastapi.responses import HTMLResponse


# --- Safe expression evaluator ---------------------------------------------

# Allowed binary operators
_BIN_OPS = {
    ast.Add: operator.add,
    ast.Sub: operator.sub,
    ast.Mult: operator.mul,
    ast.Div: operator.truediv,
    ast.FloorDiv: operator.floordiv,
    ast.Mod: operator.mod,
    ast.Pow: operator.pow,
}

# Allowed unary operators
_UNARY_OPS = {
    ast.UAdd: operator.pos,
    ast.USub: operator.neg,
}

# Allowed names (constants and functions)
_NAMES = {
    # Constants
    "pi": math.pi,
    "e": math.e,
    "tau": math.tau,
    "inf": math.inf,
    "nan": math.nan,
    # Functions
    "sqrt": math.sqrt,
    "cbrt": lambda x: x ** (1 / 3) if x >= 0 else -((-x) ** (1 / 3)),
    "abs": abs,
    "round": round,
    "floor": math.floor,
    "ceil": math.ceil,
    "trunc": math.trunc,
    "factorial": math.factorial,
    "gcd": math.gcd,
    "lcm": getattr(math, "lcm", lambda a, b: abs(a * b) // math.gcd(a, b) if a and b else 0),
    "exp": math.exp,
    "log": math.log,        # log(x) or log(x, base)
    "log2": math.log2,
    "log10": math.log10,
    "sin": math.sin,
    "cos": math.cos,
    "tan": math.tan,
    "asin": math.asin,
    "acos": math.acos,
    "atan": math.atan,
    "atan2": math.atan2,
    "sinh": math.sinh,
    "cosh": math.cosh,
    "tanh": math.tanh,
    "degrees": math.degrees,
    "radians": math.radians,
    "hypot": math.hypot,
    "pow": math.pow,
    "min": min,
    "max": max,
    "sum": sum,
}


def _safe_eval(node):
    """Recursively evaluate an AST node, allowing only safe math operations."""
    if isinstance(node, ast.Expression):
        return _safe_eval(node.body)

    # Numbers (int, float, complex)
    if isinstance(node, ast.Constant):
        if isinstance(node.value, (int, float, complex)):
            return node.value
        raise ValueError(f"Unsupported constant: {node.value!r}")

    # Binary operations: a + b, etc.
    if isinstance(node, ast.BinOp):
        op_type = type(node.op)
        if op_type not in _BIN_OPS:
            raise ValueError(f"Operator {op_type.__name__} is not allowed")
        left = _safe_eval(node.left)
        right = _safe_eval(node.right)
        return _BIN_OPS[op_type](left, right)

    # Unary operations: -a, +a
    if isinstance(node, ast.UnaryOp):
        op_type = type(node.op)
        if op_type not in _UNARY_OPS:
            raise ValueError(f"Unary operator {op_type.__name__} is not allowed")
        return _UNARY_OPS[op_type](_safe_eval(node.operand))

    # Names: pi, e, etc.
    if isinstance(node, ast.Name):
        if node.id in _NAMES:
            val = _NAMES[node.id]
            if callable(val):
                raise ValueError(f"'{node.id}' is a function — call it like {node.id}(...)")
            return val
        raise ValueError(f"Unknown name: {node.id}")

    # Function calls: sqrt(2), log(10, 2), etc.
    if isinstance(node, ast.Call):
        if not isinstance(node.func, ast.Name):
            raise ValueError("Only direct function calls are allowed")
        fname = node.func.id
        if fname not in _NAMES or not callable(_NAMES[fname]):
            raise ValueError(f"Unknown function: {fname}")
        if node.keywords:
            raise ValueError("Keyword arguments are not supported")
        args = [_safe_eval(arg) for arg in node.args]
        return _NAMES[fname](*args)

    # Tuples / lists (e.g. for sum([1,2,3]) or min(1,2,3))
    if isinstance(node, (ast.Tuple, ast.List)):
        return [_safe_eval(elt) for elt in node.elts]

    raise ValueError(f"Disallowed expression element: {type(node).__name__}")


def _evaluate(expression: str):
    """Parse and safely evaluate a math expression string."""
    if not expression or not expression.strip():
        raise ValueError("Expression is empty")

    # Convenience: people often write "^" for exponent
    cleaned = expression.replace("^", "**").strip()

    try:
        tree = ast.parse(cleaned, mode="eval")
    except SyntaxError as e:
        raise ValueError(f"Could not parse expression: {e.msg}")

    result = _safe_eval(tree)
    return result, cleaned


def _format_result(value) -> str:
    """Format a numeric result for display."""
    if isinstance(value, bool):
        return str(value)
    if isinstance(value, complex):
        return str(value)
    if isinstance(value, float):
        if math.isnan(value):
            return "NaN"
        if math.isinf(value):
            return "∞" if value > 0 else "-∞"
        # If it's a whole number, drop the trailing .0
        if value.is_integer() and abs(value) < 1e16:
            return str(int(value))
        # Otherwise show up to 12 significant digits, stripped of trailing zeros
        formatted = f"{value:.12g}"
        return formatted
    if isinstance(value, int):
        # Add thousands separators for readability on large ints
        if abs(value) >= 10000:
            return f"{value:,}"
        return str(value)
    return str(value)


# --- Event emitter helper --------------------------------------------------

async def _emit(emitter, description: str, done: bool = False, hidden: bool = False):
    if emitter is None:
        return
    await emitter({
        "type": "status",
        "data": {"description": description, "done": done, "hidden": hidden},
    })


# --- HTML card -------------------------------------------------------------

def _build_card(expression_display: str, result_display: str) -> str:
    # Escape user-facing strings to avoid breaking HTML
    import html as _html
    expr_safe = _html.escape(expression_display)
    result_safe = _html.escape(result_display)

    return f"""<!DOCTYPE html>
<html lang="en">
<head>
<meta charset="utf-8">
<meta name="viewport" content="width=device-width,initial-scale=1">
<style>
*,*::before,*::after{{box-sizing:border-box;margin:0;padding:0}}
html,body{{background:transparent;font-family:-apple-system,BlinkMacSystemFont,'Segoe UI',sans-serif;padding:6px;color:#e6edf3}}
.card{{
    max-width:560px;
    background:linear-gradient(135deg,#161b22 0%,#1a2332 100%);
    border:1px solid rgba(255,255,255,0.08);
    border-radius:14px;
    padding:18px 20px;
    box-shadow:0 4px 16px rgba(0,0,0,0.25);
}}
.header{{
    display:flex;align-items:center;gap:10px;
    font-size:12px;letter-spacing:0.08em;text-transform:uppercase;
    color:#8b949e;margin-bottom:14px;
}}
.icon{{
    width:24px;height:24px;border-radius:7px;
    background:linear-gradient(135deg,#58a6ff,#a371f7);
    display:flex;align-items:center;justify-content:center;
    font-size:14px;color:#fff;font-weight:700;
}}
.expr{{
    font-family:'SF Mono','Monaco','Consolas',monospace;
    font-size:14px;color:#8b949e;
    background:rgba(255,255,255,0.03);
    border:1px solid rgba(255,255,255,0.05);
    border-radius:8px;
    padding:10px 12px;
    word-break:break-all;
    margin-bottom:12px;
}}
.equals{{
    color:#6e7681;font-size:12px;text-align:center;margin:6px 0;
    letter-spacing:0.1em;
}}
.result{{
    font-family:'SF Mono','Monaco','Consolas',monospace;
    font-size:28px;font-weight:600;
    color:#58a6ff;
    background:rgba(88,166,255,0.08);
    border:1px solid rgba(88,166,255,0.2);
    border-radius:8px;
    padding:14px 16px;
    word-break:break-all;
    text-align:center;
}}
</style>
</head>
<body>
<div class="card">
    <div class="header">
        <div class="icon">∑</div>
        <span>Calculator</span>
    </div>
    <div class="expr">{expr_safe}</div>
    <div class="equals">= equals =</div>
    <div class="result">{result_safe}</div>
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


# --- Tool class ------------------------------------------------------------

class Tools:
    def __init__(self):
        pass

    async def calculate(
        self,
        expression: str,
        __event_emitter__: Optional[Callable[[dict], Awaitable[None]]] = None,
    ) -> "HTMLResponse | str":
        """
        Evaluate a mathematical expression and return the numeric result rendered
        as an inline card.

        USE THIS when the user asks you to compute, calculate, evaluate, solve,
        or work out the value of any arithmetic or mathematical expression —
        e.g. "what is 17 * 23", "calculate sqrt(2) + 1", "compute log(1000)",
        "(15% of 240) + 12", "5 factorial", etc.

        DO NOT use this for: symbolic algebra, equation solving with unknowns,
        calculus (derivatives/integrals), matrix operations, or unit conversions.

        Supported:
          - Arithmetic: +  -  *  /  //  %  **  (and ^ as a synonym for **)
          - Parentheses for grouping
          - Constants: pi, e, tau, inf, nan
          - Functions: sqrt, cbrt, abs, round, floor, ceil, trunc, factorial,
            gcd, lcm, exp, log (natural; or log(x, base)), log2, log10,
            sin, cos, tan, asin, acos, atan, atan2, sinh, cosh, tanh,
            degrees, radians, hypot, pow, min, max, sum

        :param expression: The math expression as a string, e.g. "2 + 2",
            "sqrt(16) * (3 + 4)", "log(100, 10)", "sin(pi/4)".
        :return: An HTML card showing the expression and its result. On error,
            a plain-text error message is returned instead.
        """
        await _emit(__event_emitter__, f"🧮 Evaluating: {expression}")

        try:
            result, cleaned_expr = _evaluate(expression)
        except ValueError as ve:
            msg = f"❌ Could not evaluate expression: {ve}"
            await _emit(__event_emitter__, msg, done=True)
            return msg
        except ZeroDivisionError:
            msg = "❌ Division by zero."
            await _emit(__event_emitter__, msg, done=True)
            return msg
        except OverflowError:
            msg = "❌ Result is too large to compute."
            await _emit(__event_emitter__, msg, done=True)
            return msg
        except Exception as exc:
            msg = f"❌ Error evaluating expression: {exc}"
            await _emit(__event_emitter__, msg, done=True)
            return msg

        result_display = _format_result(result)
        # Show the cleaned expression (with ** instead of ^ if user used ^)
        display_expr = expression.strip() if expression.strip() == cleaned_expr else cleaned_expr

        card = _build_card(display_expr, result_display)
        await _emit(__event_emitter__, f"✅ {display_expr} = {result_display}", done=True, hidden=True)

        return HTMLResponse(content=card, headers={"content-disposition": "inline"})