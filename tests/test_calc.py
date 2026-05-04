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
