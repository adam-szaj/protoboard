# SPDX-License-Identifier: MIT
"""Value-string decoding — resistance/inductance parsing and the chip code."""

from __future__ import annotations

import pytest

from protoboard.gui.renderers.value_codes import (
    code_digits,
    parse_inductance,
    parse_resistance,
    smd_code,
)


class TestParseResistance:

    @pytest.mark.parametrize(("text", "ohms"), [
        ("10k", 10_000.0),
        ("4.7k", 4_700.0),
        ("4k7", 4_700.0),
        ("470", 470.0),
        ("470R", 470.0),
        ("4R7", 4.7),
        ("1R2", 1.2),
        ("0.22", 0.22),
        ("0,22", 0.22),
        ("1M", 1_000_000.0),
        ("2G2", 2.2e9),
        ("22Ω", 22.0),
        ("22 k", 22_000.0),
        ("10kΩ", 10_000.0),
    ])
    def test_parses(self, text: str, ohms: float) -> None:
        assert parse_resistance(text) == pytest.approx(ohms)

    @pytest.mark.parametrize("text", ["?", "", "abc", "10k5k", "k10"])
    def test_rejects(self, text: str) -> None:
        assert parse_resistance(text) is None


class TestParseInductance:

    @pytest.mark.parametrize(("text", "uh"), [
        ("10uH", 10.0),
        ("10µH", 10.0),
        ("10µ", 10.0),
        ("10", 10.0),          # bare number = µH (the code's unit)
        ("4u7", 4.7),
        ("4n7", 0.0047),
        ("100nH", 0.1),
        ("2.2mH", 2_200.0),
        ("1H", 1_000_000.0),   # full henries only with the explicit H
    ])
    def test_parses(self, text: str, uh: float) -> None:
        assert parse_inductance(text) == pytest.approx(uh)

    @pytest.mark.parametrize("text", ["?", "", "abc"])
    def test_rejects(self, text: str) -> None:
        assert parse_inductance(text) is None


class TestCodeDigits:

    def test_two_digit_e_series(self) -> None:
        assert code_digits(10_000.0) == ([1, 0], 3)
        assert code_digits(4_700.0) == ([4, 7], 2)
        assert code_digits(4.7) == ([4, 7], -1)
        assert code_digits(0.22) == ([2, 2], -2)

    def test_three_digit_when_two_do_not_fit(self) -> None:
        assert code_digits(12_400.0) == ([1, 2, 4], 2)

    def test_unrepresentable_value_is_none(self) -> None:
        assert code_digits(3.14159) is None
        assert code_digits(0.0) is None
        assert code_digits(-10.0) is None


class TestSmdCode:

    @pytest.mark.parametrize(("value", "code"), [
        (22_000.0, "223"),     # the user's canonical example: 22k → 223
        (4_700.0, "472"),
        (470.0, "471"),
        (10.0, "100"),
        (12_400.0, "1242"),
        (4.7, "4R7"),
        (4.75, "4R75"),
        (1.0, "1R0"),
        (0.22, "R22"),
    ])
    def test_codes(self, value: float, code: str) -> None:
        assert smd_code(value) == code

    def test_unrepresentable_value_is_none(self) -> None:
        assert smd_code(3.14159) is None
