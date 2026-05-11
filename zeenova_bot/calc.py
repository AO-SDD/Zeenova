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

# Numeric magnitude suffixes: ``1k`` = 1 000, ``2.5m`` = 2 500 000, etc. The
# negative lookahead prevents matching inside longer letter runs (e.g. ``1mb``
# stays unparseable rather than expanding to ``(1*1_000_000)b``).
_SUFFIX_MULTIPLIERS: Final[dict[str, int]] = {
    "k": 1_000,
    "m": 1_000_000,
    "b": 1_000_000_000,
    "t": 1_000_000_000_000,
}
_SUFFIX_RE = re.compile(r"(\d+(?:\.\d+)?)([kmbtKMBT])(?![A-Za-z])")

# Marker used while pre-processing percent literals. ``5%`` is rewritten
# to ``__pct__(5)`` so the AST evaluator can recognise it as a percent
# (and apply calculator-style semantics like ``100 + 10%`` → 110)
# without confusing it for a literal user-typed division. The exact
# spelling doesn't matter as long as it can't appear in a real input —
# the parse_input regex only allows digits / arithmetic punctuation, so
# users cannot type ``__pct__`` directly.
_PERCENT_MARKER: Final[str] = "__pct__"

# Matches ``<num>`` or ``<num><kmbt-suffix>`` immediately followed by ``%``
# (and not by another digit, so ``10%3`` stays as a modulo). The optional
# magnitude suffix is captured so it survives the rewrite and gets
# expanded later by :func:`_expand_suffixes`.
_PERCENT_RE = re.compile(r"(\d+(?:\.\d+)?[kmbtKMBT]?)%(?!\d)")


def _expand_suffixes(expr: str) -> str:
    return _SUFFIX_RE.sub(
        lambda m: f"({m.group(1)}*{_SUFFIX_MULTIPLIERS[m.group(2).lower()]})",
        expr,
    )


def _expand_percents(expr: str) -> str:
    return _PERCENT_RE.sub(rf"{_PERCENT_MARKER}(\1)", expr)


# Matches a run of one or more leading zeros followed by at least one
# more digit. Lookbehind rejects digits and dots so we don't touch the
# fractional part of ``1.02`` or alphanumeric tokens like ``0x10``;
# lookahead allows a trailing ``.`` so we can normalise ``01.5`` →
# ``1.5`` while still keeping ``0.5`` (zero followed by ``.``) intact.
_LEADING_ZERO_RE = re.compile(r"(?<![\w.])0+(\d+)")


def _strip_leading_zeros(expr: str) -> str:
    """Normalise integer literals so Python doesn't reject them as octals.

    Python 3 forbids leading zeros on decimal integer literals (``0294882``
    raises ``SyntaxError``), but users typing arithmetic in chat reach
    for things like phone numbers, padded counters, or arbitrary digit
    runs all the time. Strip the leading zeros before handing the
    expression to :func:`ast.parse`.
    """
    return _LEADING_ZERO_RE.sub(r"\1", expr)


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
    # Strip thousands separators inside numeric literals so ``37,632.00``
    # parses as ``37632.00`` and ``1,000,000`` as ``1000000``. We only
    # remove a comma when it sits between two digits, which avoids
    # touching Python tuple syntax (``1,2`` would be a tuple, but
    # ``1,000`` is clearly a number) — though tuples aren't valid in
    # the AST evaluator anyway.
    expr = re.sub(r"(?<=\d),(?=\d)", "", expr)
    # Allow ``^`` as a friendlier alias for power; users who type ``2^10``
    # almost always mean ``2**10`` rather than the bitwise XOR Python gives
    # them by default.
    expr = expr.replace("^", "**")
    # Strip leading zeros from integer literals so ``0294882`` parses as
    # 294 882 instead of erroring out as a malformed octal.
    expr = _strip_leading_zeros(expr)
    # Rewrite ``<num>%`` literals into a marker call (``__pct__(<num>)``)
    # *before* suffix expansion, so the optional ``k/m/b/t`` suffix is
    # captured inside the marker and then expanded normally on the next
    # pass. The AST evaluator special-cases the marker to provide
    # calculator-style percent semantics (``100 + 10%`` → 110).
    expr = _expand_percents(expr)
    # Then expand magnitude suffixes (``1k`` → ``(1*1000)``, ``2.5m`` →
    # ``(2.5*1000000)``) so the AST parser only has to deal with plain
    # numeric literals.
    expr = _expand_suffixes(expr)
    try:
        tree = ast.parse(expr, mode="eval")
    except SyntaxError as exc:
        # ``exc.msg`` is usually already ``"invalid syntax"``; don't prefix
        # again or the user sees ``"invalid syntax: invalid syntax"``.
        raise CalcError(exc.msg or "invalid syntax") from exc
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
        # Calculator-style percent: when the right-hand side is a ``<num>%``
        # marker, interpret +/- as additive percent and *,/ as direct scale.
        # This matches Windows / iOS calculator behaviour:
        #   100 + 10%  -> 110   (add 10% of 100)
        #   100 - 10%  -> 90    (subtract 10% of 100)
        #   100 * 10%  -> 10    (10% of 100)
        #   100 / 10%  -> 1000  (divide by 0.10)
        # When *both* sides are percent literals (``50% + 50%``) we fall
        # through to plain arithmetic (``0.5 + 0.5 = 1.0``) instead, since
        # there's no obvious "base" to apply the % to.
        if _is_percent_marker(node.right) and not _is_percent_marker(node.left):
            pct = _percent_value(node.right)
            base = _eval(node.left)
            if isinstance(node.op, ast.Add):
                return base * (1.0 + pct)
            if isinstance(node.op, ast.Sub):
                return base * (1.0 - pct)
            if isinstance(node.op, ast.Mult):
                return base * pct
            if isinstance(node.op, ast.Div):
                return base / pct
            # Fallthrough for ops where percent context doesn't apply
            # (e.g. ``5 ** 10%``); evaluate the percent as its raw value.
        op = _BIN_OPS.get(type(node.op))
        if op is None:
            raise CalcError(f"unsupported binary op: {type(node.op).__name__}")
        return op(_eval(node.left), _eval(node.right))
    if isinstance(node, ast.UnaryOp):
        un = _UN_OPS.get(type(node.op))
        if un is None:
            raise CalcError(f"unsupported unary op: {type(node.op).__name__}")
        return un(_eval(node.operand))
    if _is_percent_marker(node):
        # Standalone ``5%`` (no enclosing binary op) → 0.05.
        return _percent_value(node)
    raise CalcError(f"unsupported expression node: {type(node).__name__}")


def _is_percent_marker(node: ast.AST) -> bool:
    return (
        isinstance(node, ast.Call)
        and isinstance(node.func, ast.Name)
        and node.func.id == _PERCENT_MARKER
        and len(node.args) == 1
        and not node.keywords
    )


def _percent_value(node: ast.AST) -> float:
    """Return ``arg / 100`` for a percent-marker call. Caller must check."""
    assert isinstance(node, ast.Call)  # guarded by _is_percent_marker
    return _eval(node.args[0]) / 100.0


# Matches an arithmetic expression optionally followed by 1 or 2 alphabetic
# currency tokens, e.g. ``2+2/4``, ``2+2/4 btc``, ``1 usd egp``.
#
# - The expression must contain at least one digit so that bare symbols
#   like ``BTC`` keep flowing through to the existing price-card path.
# - The k/m/b/t magnitude suffixes are part of the body so ``1k+1`` parses
#   as a single arithmetic expression rather than ``1`` + currency ``k+1``.
# - Currency tokens are 2-8 ASCII letters and must be separated from the
#   expression by whitespace, so a trailing currency stays distinct from a
#   suffix glued to a number.
_CALC_RE = re.compile(
    r"""
    ^\s*
    (?P<expr>[\d+\-*/.,()\s%^kmbtKMBT]+?) # arithmetic body
    (?:\s+(?P<ccy1>[A-Za-z]{2,8}))?      # optional from-currency
    (?:\s+(?P<ccy2>[A-Za-z]{2,8}))?      # optional to-currency
    \s*$
    """,
    re.VERBOSE,
)

# Tokens that make a free-text message *clearly* an arithmetic expression
# rather than e.g. a number quoted in a sentence ("got 50% on the test").
# Used by :func:`looks_like_calc` so handlers can stay silent on bare
# numbers in group chats and only reply when the user actually meant to
# do math. ``%`` counts as math because the bot supports it as a percent
# operator.
_CALC_OP_RE = re.compile(r"[+\-*/^%]")


def looks_like_calc(expr: str) -> bool:
    """Return True when ``expr`` contains an arithmetic operator.

    A bare number followed by a currency (``300 btc``) is *not* a
    calculator expression on its own — it's a price query. But once
    there's a ``+``, ``-``, ``*``, ``/``, ``^``, or ``%`` in the body
    the user is doing math and we should always reply (success or error).

    Used by the message handler to decide whether to surface parse
    errors. In group chats this prevents the bot from blurting
    "invalid syntax" at every stray ``50%`` someone types.
    """
    return bool(_CALC_OP_RE.search(expr))


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
