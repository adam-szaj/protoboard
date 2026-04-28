# SPDX-License-Identifier: MIT
"""Tests for Box constructors and field validation.

Covers HBox, VBox, BoardGrid and atoms (PadAtom, StripAtom, GapAtom).
"""

import dataclasses

import pytest
from protoboard.domain.board.boxes import (
    BoardGrid,
    GapAtom,
    HBox,
    PadAtom,
    SizePolicy,
    StripAtom,
    VBox,
)
from protoboard.domain.board.pads import PadLayers
from protoboard.geometry import Insets


def _pad() -> PadAtom:
    return PadAtom(
        width_policy=SizePolicy.HUG,
        height_policy=SizePolicy.HUG,
        layers=PadLayers(),
    )


class TestSizePolicy:
    def test_enum_values(self) -> None:
        assert SizePolicy.FIXED.value == "fixed"
        assert SizePolicy.HUG.value == "hug"
        assert SizePolicy.FILL.value == "fill"


class TestPadAtom:
    def test_defaults(self) -> None:
        layers = PadLayers()
        pad = PadAtom(
            width_policy=SizePolicy.HUG,
            height_policy=SizePolicy.HUG,
            layers=layers,
        )
        assert pad.layers is layers
        assert pad.label is None

    def test_with_label(self) -> None:
        pad = PadAtom(
            width_policy=SizePolicy.HUG,
            height_policy=SizePolicy.HUG,
            layers=PadLayers(),
            label="VCC",
        )
        assert pad.label == "VCC"

    def test_fixed_requires_dimensions(self) -> None:
        with pytest.raises(ValueError):
            PadAtom(
                width_policy=SizePolicy.FIXED,
                height_policy=SizePolicy.HUG,
                layers=PadLayers(),
            )

    def test_hug_with_explicit_size_raises(self) -> None:
        with pytest.raises(ValueError):
            PadAtom(
                width_policy=SizePolicy.HUG,
                height_policy=SizePolicy.HUG,
                width=1.0,
                layers=PadLayers(),
            )

    def test_frozen(self) -> None:
        pad = _pad()
        with pytest.raises(dataclasses.FrozenInstanceError):
            pad.label = "X"  # type: ignore[misc]


class TestStripAtom:
    def test_horizontal(self) -> None:
        strip = StripAtom(
            width_policy=SizePolicy.HUG,
            height_policy=SizePolicy.HUG,
            length=10,
            orientation="horizontal",
            layers=PadLayers(),
        )
        assert strip.length == 10
        assert strip.orientation == "horizontal"

    def test_vertical(self) -> None:
        strip = StripAtom(
            width_policy=SizePolicy.HUG,
            height_policy=SizePolicy.HUG,
            length=5,
            orientation="vertical",
            layers=PadLayers(),
        )
        assert strip.orientation == "vertical"

    def test_fill_length(self) -> None:
        strip = StripAtom(
            width_policy=SizePolicy.FILL,
            height_policy=SizePolicy.HUG,
            length="fill",
            orientation="horizontal",
            layers=PadLayers(),
        )
        assert strip.length == "fill"

    def test_zero_length_raises(self) -> None:
        with pytest.raises(ValueError):
            StripAtom(
                width_policy=SizePolicy.HUG,
                height_policy=SizePolicy.HUG,
                length=0,
                orientation="horizontal",
                layers=PadLayers(),
            )

    def test_negative_length_raises(self) -> None:
        with pytest.raises(ValueError):
            StripAtom(
                width_policy=SizePolicy.HUG,
                height_policy=SizePolicy.HUG,
                length=-3,
                orientation="horizontal",
                layers=PadLayers(),
            )

    def test_invalid_orientation_raises(self) -> None:
        with pytest.raises(ValueError):
            StripAtom(
                width_policy=SizePolicy.HUG,
                height_policy=SizePolicy.HUG,
                length=5,
                orientation="diagonal",  # type: ignore[arg-type]
                layers=PadLayers(),
            )

    def test_default_cuttable_is_true(self) -> None:
        strip = StripAtom(
            width_policy=SizePolicy.HUG,
            height_policy=SizePolicy.HUG,
            length=5,
            orientation="horizontal",
            layers=PadLayers(),
        )
        assert strip.cuttable is True

    def test_fill_length_with_hug_policy_allowed(self) -> None:
        # length="fill" is independent from width_policy — the layout engine
        # decides during the layout pass how to size the atom.
        strip = StripAtom(
            width_policy=SizePolicy.HUG,
            height_policy=SizePolicy.HUG,
            length="fill",
            orientation="horizontal",
            layers=PadLayers(),
        )
        assert strip.length == "fill"


class TestGapAtom:
    def test_no_dimensions(self) -> None:
        gap = GapAtom(
            width_policy=SizePolicy.HUG,
            height_policy=SizePolicy.HUG,
        )
        assert gap.width is None
        assert gap.height is None

    def test_with_dimensions(self) -> None:
        gap = GapAtom(
            width_policy=SizePolicy.FIXED,
            height_policy=SizePolicy.FIXED,
            width=2.5,
            height=1.0,
        )
        assert gap.width == 2.5
        assert gap.height == 1.0


class TestHBox:
    def test_construct_with_children(self) -> None:
        hbox = HBox(
            width_policy=SizePolicy.HUG,
            height_policy=SizePolicy.HUG,
            children=[_pad(), _pad()],
        )
        assert len(hbox.children) == 2

    def test_default_gap_is_one(self) -> None:
        hbox = HBox(
            width_policy=SizePolicy.HUG,
            height_policy=SizePolicy.HUG,
            children=[_pad()],
        )
        assert hbox.gap == 1.0

    def test_fractional_gap(self) -> None:
        hbox = HBox(
            width_policy=SizePolicy.HUG,
            height_policy=SizePolicy.HUG,
            children=[_pad()],
            gap=0.5,
        )
        assert hbox.gap == 0.5

    def test_negative_gap_allowed(self) -> None:
        hbox = HBox(
            width_policy=SizePolicy.HUG,
            height_policy=SizePolicy.HUG,
            children=[_pad()],
            gap=-0.5,
        )
        assert hbox.gap == -0.5

    def test_empty_children_allowed(self) -> None:
        hbox = HBox(
            width_policy=SizePolicy.HUG,
            height_policy=SizePolicy.HUG,
            children=[],
        )
        assert hbox.children == []

    def test_fixed_requires_width(self) -> None:
        with pytest.raises(ValueError):
            HBox(
                width_policy=SizePolicy.FIXED,
                height_policy=SizePolicy.HUG,
                children=[_pad()],
            )


class TestVBox:
    def test_construct_with_children(self) -> None:
        vbox = VBox(
            width_policy=SizePolicy.HUG,
            height_policy=SizePolicy.HUG,
            children=[_pad(), _pad(), _pad()],
        )
        assert len(vbox.children) == 3

    def test_fixed_requires_height(self) -> None:
        with pytest.raises(ValueError):
            VBox(
                width_policy=SizePolicy.HUG,
                height_policy=SizePolicy.FIXED,
                children=[_pad()],
            )


class TestBoardGrid:
    def test_construct(self) -> None:
        grid = BoardGrid(
            width_policy=SizePolicy.HUG,
            height_policy=SizePolicy.HUG,
            cell=_pad(),
            rows=20,
            cols=30,
        )
        assert grid.rows == 20
        assert grid.cols == 30
        assert grid.row_gap == 1.0
        assert grid.col_gap == 1.0

    def test_zero_rows_raises(self) -> None:
        with pytest.raises(ValueError):
            BoardGrid(
                width_policy=SizePolicy.HUG,
                height_policy=SizePolicy.HUG,
                cell=_pad(),
                rows=0,
                cols=10,
            )

    def test_negative_cols_raises(self) -> None:
        with pytest.raises(ValueError):
            BoardGrid(
                width_policy=SizePolicy.HUG,
                height_policy=SizePolicy.HUG,
                cell=_pad(),
                rows=10,
                cols=-1,
            )

    def test_custom_gaps(self) -> None:
        grid = BoardGrid(
            width_policy=SizePolicy.HUG,
            height_policy=SizePolicy.HUG,
            cell=_pad(),
            rows=2,
            cols=2,
            row_gap=0.5,
            col_gap=2.0,
        )
        assert grid.row_gap == 0.5
        assert grid.col_gap == 2.0


class TestMargins:
    def test_default_margin_is_zero(self) -> None:
        pad = _pad()
        assert pad.margin == Insets.zero()

    def test_custom_margin(self) -> None:
        pad = PadAtom(
            width_policy=SizePolicy.HUG,
            height_policy=SizePolicy.HUG,
            layers=PadLayers(),
            margin=Insets(top=0.5, right=0.0, bottom=0.0, left=0.0),
        )
        assert pad.margin.top == 0.5

    def test_negative_margin_allowed(self) -> None:
        pad = PadAtom(
            width_policy=SizePolicy.HUG,
            height_policy=SizePolicy.HUG,
            layers=PadLayers(),
            margin=Insets(top=-0.5, right=0.0, bottom=0.0, left=0.0),
        )
        assert pad.margin.top == -0.5
