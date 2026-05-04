"""Safe arithmetic expression parser.

The bot accepts free-text math like ``2+2/4`` or ``(1+2)*3``. We can't pass
those to :func:`eval` directly without exposing arbitrary code execution, so
we parse the string with :mod:`ast` and only walk a tiny subset of nodes:
numeric literals plus the standard binary/unary arithmetic operators.

Anything else — names, calls, comprehensions, attribute access, strings —
raises :class:`CalcError`.
"""

from __future__ import annotations

import ast
import math
import operator
import re
from collections.abc import Callable
from typing import Final

_BIN_OPS: Final[dict[type[ast.operator], Callable[[float, float], float]]] = {
    ast.Add: operator.add,
    ast.Sub: operator.sub,
    ast.Mult: operator.mul,
    ast.Div: operator.truediv,
    ast.Mod: operator.mod,
    ast.Pow: operator.pow,
    ast.FloorDiv: operator.floordiv,
}
_UN_OPS: Final[dict[type[ast.unaryop], Callable[[float], float]]] = {
    ast.UAdd: operator.pos,
    ast.USub: operator.neg,
}

# Reject expressions longer than this — keeps the parser fast and immune to
# pathological inputs like deeply-nested parens.
_MAX_EXPR_LEN = 200


class CalcError(ValueError):
    """Raised when an expression isn't a pure arithmetic computation."""


def safe_eval(expr: str) -> float:
    """Evaluate ``expr`` as pure arithmetic. Returns a finite float.

    Raises :class:`CalcError` for syntax errors, unsupported nodes, or
    non-finite results (``inf`` / ``nan`` from e.g. division by zero).
    """
    expr = expr.strip()
    if not expr:
        raise CalcError("empty expression")
    if len(expr) > _MAX_EXPR_LEN:
        raise CalcError("expression too long")
    # Allow ``^`` as a friendlier alias for power; users who type ``2^10``
    # almost always mean ``2**10`` rather than the bitwise XOR Python gives
    # them by default.
    expr = expr.replace("^", "**")
    try:
        tree = ast.parse(expr, mode="eval")
    except SyntaxError as exc:
        raise CalcError(f"invalid syntax: {exc.msg}") from exc
    try:
        value = _eval(tree.body)
    except ZeroDivisionError as exc:
        raise CalcError("division by zero") from exc
    except OverflowError as exc:
        raise CalcError("result too large") from exc
    except ValueError as exc:
        raise CalcError(str(exc)) from exc
    if not math.isfinite(value):
        raise CalcError("result is not finite")
    return value


def _eval(node: ast.AST) -> float:
    if isinstance(node, ast.Constant):
        if isinstance(node.value, bool):
            raise CalcError("booleans are not allowed")
        if isinstance(node.value, int | float):
            return float(node.value)
        raise CalcError(f"unsupported constant: {node.value!r}")
    if isinstance(node, ast.BinOp):
        op = _BIN_OPS.get(type(node.op))
        if op is None:
            raise CalcError(f"unsupported binary op: {type(node.op).__name__}")
        return op(_eval(node.left), _eval(node.right))
    if isinstance(node, ast.UnaryOp):
        un = _UN_OPS.get(type(node.op))
        if un is None:
            raise CalcError(f"unsupported unary op: {type(node.op).__name__}")
        return un(_eval(node.operand))
    raise CalcError(f"unsupported expression node: {type(node).__name__}")


# Matches an arithmetic expression optionally followed by 1 or 2 alphabetic
# currency tokens, e.g. ``2+2/4``, ``2+2/4 btc``, ``1 usd egp``.
#
# - The expression must contain at least one digit so that bare symbols
#   like ``BTC`` keep flowing through to the existing price-card path.
# - Currency tokens are 2-8 ASCII letters.
_CALC_RE = re.compile(
    r"""
    ^\s*
    (?P<expr>[\d+\-*/.()\s%^]+?)         # arithmetic body
    (?:\s+(?P<ccy1>[A-Za-z]{2,8}))?      # optional from-currency
    (?:\s+(?P<ccy2>[A-Za-z]{2,8}))?      # optional to-currency
    \s*$
    """,
    re.VERBOSE,
)


def parse_input(text: str) -> tuple[str, str | None, str | None] | None:
    """Try to interpret ``text`` as a calculator/conversion message.

    Returns ``(expr, ccy1, ccy2)`` on success, or ``None`` if the message
    doesn't look like one (e.g. plain text, or a bare ticker symbol).
    """
    text = text.strip()
    if not text or not any(c.isdigit() for c in text):
        return None
    match = _CALC_RE.match(text)
    if not match:
        return None
    expr = match.group("expr").strip()
    if not expr:
        return None
    return expr, match.group("ccy1"), match.group("ccy2")
