"""Tests for the safe arithmetic evaluator."""

from __future__ import annotations

import math

import pytest

from zeenova_bot.calc import CalcError, parse_input, safe_eval


class TestSafeEval:
    @pytest.mark.parametrize(
        ("expr", "expected"),
        [
            ("2+2", 4.0),
            ("2+2/4", 2.5),
            ("(1+2)*3", 9.0),
            ("10 - 5 - 3", 2.0),
            ("2**8", 256.0),
            ("2^10", 1024.0),  # Friendlier ``^`` alias.
            ("100 / 4 + 1", 26.0),
            ("-5 + 10", 5.0),
            ("(-3) * 2", -6.0),
            ("1.5 + 0.5", 2.0),
            ("100 % 7", 2.0),
            (" 1  +  2 ", 3.0),
        ],
    )
    def test_evaluates(self, expr: str, expected: float) -> None:
        assert safe_eval(expr) == pytest.approx(expected)

    @pytest.mark.parametrize(
        "expr",
        [
            "import os",
            "__import__('os')",
            "os.system('ls')",
            "x + 1",
            "2 ** 1000000",  # Pow that overflows to inf.
            "1/0",  # Div-by-zero → inf.
            "1+",
            "",
            "    ",
            "a" * 300,  # Too long.
            "True + 1",
            '"abc" + "def"',
        ],
    )
    def test_rejects(self, expr: str) -> None:
        with pytest.raises(CalcError):
            safe_eval(expr)

    def test_inf_rejected(self) -> None:
        with pytest.raises(CalcError):
            safe_eval("1/0")

    def test_nan_rejected(self) -> None:
        # 0**0 is 1; 0/0 raises ZeroDivisionError. We want both to either
        # produce a finite number or a CalcError, never a NaN/inf leak.
        with pytest.raises(CalcError):
            safe_eval("0/0")

    def test_returns_float(self) -> None:
        result = safe_eval("2 + 2")
        assert isinstance(result, float)
        assert math.isfinite(result)

    @pytest.mark.parametrize(
        ("expr", "expected"),
        [
            ("1k", 1_000.0),
            ("1K", 1_000.0),
            ("2.5k", 2_500.0),
            ("1m", 1_000_000.0),
            ("1.5M", 1_500_000.0),
            ("1b", 1_000_000_000.0),
            ("1t", 1_000_000_000_000.0),
            ("1k+1", 1_001.0),
            ("1k + 1", 1_001.0),
            ("2k * 3", 6_000.0),
            ("(1k+500) / 2", 750.0),
            ("1m - 1k", 999_000.0),
        ],
    )
    def test_magnitude_suffixes(self, expr: str, expected: float) -> None:
        assert safe_eval(expr) == pytest.approx(expected)

    def test_invalid_suffix_combo_rejected(self) -> None:
        # ``1mb`` is not a meaningful suffix; the negative lookahead in the
        # suffix regex leaves it un-expanded so the AST parser rejects it.
        with pytest.raises(CalcError):
            safe_eval("1mb")

    def test_syntax_error_not_duplicated(self) -> None:
        # Regression: when ``5%`` *did* fail (before percent support
        # was added) the error surfaced as
        # ``"invalid syntax: invalid syntax"`` because we prefixed the
        # already-prefixed ``SyntaxError.msg``. ``5%`` now evaluates to
        # 0.05 instead, but other genuine syntax errors must still come
        # through clean.
        with pytest.raises(CalcError) as exc_info:
            safe_eval("3++")
        assert "invalid syntax: invalid syntax" not in str(exc_info.value)


class TestParseInput:
    def test_pure_math(self) -> None:
        assert parse_input("2+2") == ("2+2", None, None)
        assert parse_input("(1+2)*3") == ("(1+2)*3", None, None)

    def test_math_plus_one_currency(self) -> None:
        assert parse_input("2+2/4 btc") == ("2+2/4", "btc", None)
        assert parse_input("100 EGP") == ("100", "EGP", None)

    def test_math_plus_two_currencies(self) -> None:
        assert parse_input("1 usd egp") == ("1", "usd", "egp")
        assert parse_input("2+8+9 USD EGP") == ("2+8+9", "USD", "EGP")

    def test_no_digit_returns_none(self) -> None:
        # Bare symbols must fall through to the price-card path.
        assert parse_input("BTC") is None
        assert parse_input("$ETH") is None
        assert parse_input("hello world") is None

    def test_empty_returns_none(self) -> None:
        assert parse_input("") is None
        assert parse_input("   ") is None

    def test_trailing_operator_parses_but_fails_eval(self) -> None:
        # ``parse_input`` only extracts segments; ``safe_eval`` is the
        # source of truth on whether the math is well-formed. A trailing
        # operator is captured and then rejected on evaluation.
        parsed = parse_input("2 + 2 +")
        assert parsed is not None
        with pytest.raises(CalcError):
            safe_eval(parsed[0])

    def test_suffix_in_expression(self) -> None:
        # ``1k+1`` keeps the ``k`` glued to the digit so it parses as one
        # arithmetic expression, not as expression ``1`` plus a bogus
        # currency ``k+1``.
        assert parse_input("1k+1") == ("1k+1", None, None)
        assert parse_input("2.5m") == ("2.5m", None, None)

    def test_suffix_with_currency(self) -> None:
        assert parse_input("1k+1 EGP") == ("1k+1", "EGP", None)
        assert parse_input("1k+1 ETH") == ("1k+1", "ETH", None)
        assert parse_input("2.5m USD EGP") == ("2.5m", "USD", "EGP")
        assert parse_input("1k egp") == ("1k", "egp", None)


class TestPercent:
    """Calculator-style percent semantics."""

    @pytest.mark.parametrize(
        ("expr", "expected"),
        [
            # Standalone percent → fraction.
            ("5%", 0.05),
            ("100%", 1.0),
            ("0.1%", 0.001),
            # +/- with a base on the left → percent of the base.
            ("100+10%", 110.0),
            ("100-10%", 90.0),
            ("200+25%", 250.0),
            ("200-25%", 150.0),
            ("1000-0.1%", 999.0),  # tiny fee
            # *,/ → direct scale by fraction.
            ("100*10%", 10.0),
            ("200*15%", 30.0),
            ("100/10%", 1000.0),
            # Both sides percent → plain arithmetic on the fractions.
            ("50%+50%", 1.0),
            ("5%+5%", 0.1),
            # Chained percent ops cascade left-to-right.
            ("100+10%+5%", 115.5),  # 110 then +5% of 110
            ("100-10%-5%", 85.5),
            # k/m/b/t suffixes inside percent literals.
            ("1k+10%", 1100.0),
            ("100+1k%", 1100.0),  # 1000% of 100 = 1000 added
        ],
    )
    def test_percent(self, expr: str, expected: float) -> None:
        assert safe_eval(expr) == pytest.approx(expected)

    def test_modulo_still_works_when_followed_by_digit(self) -> None:
        # ``10%3`` is unambiguously the modulo operator (no whitespace, a
        # digit immediately after ``%``) so the percent rewrite skips it.
        assert safe_eval("10%3") == pytest.approx(1.0)
        assert safe_eval("10 % 3") == pytest.approx(1.0)
