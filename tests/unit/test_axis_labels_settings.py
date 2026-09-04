# SPDX-License-Identifier: MIT
"""Tests for AxisLabelFormat / AxisLabelSettings and format_index()."""

from __future__ import annotations

import pytest

from protoboard.domain.axis_labels_settings import (
    AxisLabelFormat,
    AxisLabelSettings,
    format_index,
)


class TestFormatIndex:
    def test_disabled_returns_empty(self) -> None:
        for i in (0, 1, 10, 99):
            assert format_index(AxisLabelFormat.DISABLED, i) == ""

    def test_numeric_zero_based(self) -> None:
        assert format_index(AxisLabelFormat.NUMERIC_0, 0) == "0"
        assert format_index(AxisLabelFormat.NUMERIC_0, 1) == "1"
        assert format_index(AxisLabelFormat.NUMERIC_0, 42) == "42"

    def test_numeric_one_based(self) -> None:
        assert format_index(AxisLabelFormat.NUMERIC_1, 0) == "1"
        assert format_index(AxisLabelFormat.NUMERIC_1, 1) == "2"
        assert format_index(AxisLabelFormat.NUMERIC_1, 41) == "42"

    @pytest.mark.parametrize(
        "i,expected",
        [
            (0, "a"),
            (1, "b"),
            (25, "z"),
            (26, "a"),  # cycles back to 'a' after 'z'
            (27, "b"),
            (51, "z"),
            (52, "a"),  # second wrap
            (701, "z"),  # 701 % 26 == 25
            (702, "a"),  # 702 % 26 == 0
        ],
    )
    def test_alphabetic_cycles_a_to_z(self, i: int, expected: str) -> None:
        assert format_index(AxisLabelFormat.ALPHABETIC, i) == expected

    def test_alphabetic_negative_raises(self) -> None:
        with pytest.raises(ValueError):
            format_index(AxisLabelFormat.ALPHABETIC, -1)


class TestAxisLabelSettings:
    def test_any_enabled_false_when_both_disabled(self) -> None:
        s = AxisLabelSettings(
            columns=AxisLabelFormat.DISABLED,
            rows=AxisLabelFormat.DISABLED,
        )
        assert not s.any_enabled()

    def test_any_enabled_on_columns(self) -> None:
        s = AxisLabelSettings(
            columns=AxisLabelFormat.NUMERIC_0,
            rows=AxisLabelFormat.DISABLED,
        )
        assert s.any_enabled()

    def test_any_enabled_on_rows(self) -> None:
        s = AxisLabelSettings(
            columns=AxisLabelFormat.DISABLED,
            rows=AxisLabelFormat.ALPHABETIC,
        )
        assert s.any_enabled()

    def test_frozen_dataclass(self) -> None:
        s = AxisLabelSettings()
        with pytest.raises(
            Exception
        ):  # dataclasses.FrozenInstanceError or AttributeError
            s.columns = AxisLabelFormat.NUMERIC_0  # type: ignore[misc]
