# SPDX-License-Identifier: MIT
"""Tests for PadLayers — invariants on layer combinations."""

import pytest
from protoboard.domain.board.pads import PadLayers


class TestPadLayersDefaults:
    def test_defaults_are_top_only(self) -> None:
        layers = PadLayers()
        assert layers.top is True
        assert layers.bottom is False
        assert layers.plated_through is False


class TestPadLayersCombinations:
    def test_top_only_valid(self) -> None:
        layers = PadLayers(top=True, bottom=False)
        assert layers.top is True

    def test_bottom_only_valid(self) -> None:
        layers = PadLayers(top=False, bottom=True)
        assert layers.bottom is True

    def test_both_sides_no_plating_valid(self) -> None:
        # Two independent electrical nodes per CLAUDE.md §4.3.
        layers = PadLayers(top=True, bottom=True, plated_through=False)
        assert layers.plated_through is False

    def test_both_sides_plated_valid(self) -> None:
        layers = PadLayers(top=True, bottom=True, plated_through=True)
        assert layers.plated_through is True


class TestPadLayersInvariants:
    def test_no_layers_raises(self) -> None:
        # A pad with no electrical presence makes no sense.
        with pytest.raises(ValueError):
            PadLayers(top=False, bottom=False)

    def test_plated_without_top_raises(self) -> None:
        with pytest.raises(ValueError):
            PadLayers(top=False, bottom=True, plated_through=True)

    def test_plated_without_bottom_raises(self) -> None:
        with pytest.raises(ValueError):
            PadLayers(top=True, bottom=False, plated_through=True)

    def test_plated_with_neither_raises(self) -> None:
        # This is also caught by the "no layers" rule but gives a clearer error.
        with pytest.raises(ValueError):
            PadLayers(top=False, bottom=False, plated_through=True)


class TestPadLayersHashing:
    def test_equal_layers_are_equal(self) -> None:
        a = PadLayers(top=True, bottom=True, plated_through=True)
        b = PadLayers(top=True, bottom=True, plated_through=True)
        assert a == b

    def test_different_layers_are_unequal(self) -> None:
        a = PadLayers(top=True, bottom=False)
        b = PadLayers(top=True, bottom=True, plated_through=True)
        assert a != b

    def test_hashable_for_set_use(self) -> None:
        a = PadLayers(top=True, bottom=False)
        b = PadLayers(top=True, bottom=False)
        c = PadLayers(top=True, bottom=True, plated_through=True)
        s = {a, b, c}
        assert len(s) == 2
