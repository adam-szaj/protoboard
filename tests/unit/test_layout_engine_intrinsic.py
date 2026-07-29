# SPDX-License-Identifier: MIT
"""Tests for the intrinsic-size pass of the layout engine.

The intrinsic pass walks the box tree bottom-up and computes natural sizes
for HUG nodes. FILL nodes report their intrinsic size as 0 in the FILL
dimension (they will be sized in the layout pass).
"""

from protoboard.domain.board.boxes import (
    BoardGrid,
    GapAtom,
    HBox,
    PadAtom,
    SizePolicy,
    StripAtom,
    VBox,
)
from protoboard.domain.board.layout_engine import intrinsic_size
from protoboard.domain.board.pads import PadLayers
from protoboard.geometry import Size


def _pad() -> PadAtom:
    return PadAtom(
        width_policy=SizePolicy.HUG,
        height_policy=SizePolicy.HUG,
        layers=PadLayers(),
    )


class TestAtomIntrinsicSize:
    def test_pad_atom_is_one_by_one(self) -> None:
        size = intrinsic_size(_pad())
        assert size == Size(1.0, 1.0)

    def test_strip_horizontal(self) -> None:
        strip = StripAtom(
            width_policy=SizePolicy.HUG,
            height_policy=SizePolicy.HUG,
            length=5,
            orientation="horizontal",
            layers=PadLayers(),
        )
        size = intrinsic_size(strip)
        assert size == Size(5.0, 1.0)

    def test_strip_vertical(self) -> None:
        strip = StripAtom(
            width_policy=SizePolicy.HUG,
            height_policy=SizePolicy.HUG,
            length=5,
            orientation="vertical",
            layers=PadLayers(),
        )
        size = intrinsic_size(strip)
        assert size == Size(1.0, 5.0)

    def test_strip_fill_horizontal_has_zero_width(self) -> None:
        strip = StripAtom(
            width_policy=SizePolicy.FILL,
            height_policy=SizePolicy.HUG,
            length="fill",
            orientation="horizontal",
            layers=PadLayers(),
        )
        size = intrinsic_size(strip)
        assert size.lx == 0.0
        assert size.ly == 1.0

    def test_strip_fill_vertical_has_zero_height(self) -> None:
        strip = StripAtom(
            width_policy=SizePolicy.HUG,
            height_policy=SizePolicy.FILL,
            length="fill",
            orientation="vertical",
            layers=PadLayers(),
        )
        size = intrinsic_size(strip)
        assert size.lx == 1.0
        assert size.ly == 0.0

    def test_gap_no_dims_is_zero(self) -> None:
        gap = GapAtom(
            width_policy=SizePolicy.HUG,
            height_policy=SizePolicy.HUG,
        )
        size = intrinsic_size(gap)
        assert size == Size(0.0, 0.0)

    def test_gap_fixed(self) -> None:
        gap = GapAtom(
            width_policy=SizePolicy.FIXED,
            height_policy=SizePolicy.FIXED,
            width=2.5,
            height=1.0,
        )
        size = intrinsic_size(gap)
        assert size == Size(2.5, 1.0)


class TestHBoxIntrinsic:
    def test_three_pads_default_gap(self) -> None:
        hbox = HBox(
            width_policy=SizePolicy.HUG,
            height_policy=SizePolicy.HUG,
            children=[_pad(), _pad(), _pad()],
            gap=1.0,
        )
        # 3 pads * 1.0 + 2 gaps * 1.0 = 5.0
        assert intrinsic_size(hbox) == Size(5.0, 1.0)

    def test_zero_gap(self) -> None:
        hbox = HBox(
            width_policy=SizePolicy.HUG,
            height_policy=SizePolicy.HUG,
            children=[_pad(), _pad(), _pad()],
            gap=0.0,
        )
        assert intrinsic_size(hbox) == Size(3.0, 1.0)

    def test_fractional_gap(self) -> None:
        hbox = HBox(
            width_policy=SizePolicy.HUG,
            height_policy=SizePolicy.HUG,
            children=[_pad(), _pad(), _pad()],
            gap=0.5,
        )
        # 3 + 2*0.5 = 4
        assert intrinsic_size(hbox) == Size(4.0, 1.0)

    def test_negative_gap(self) -> None:
        hbox = HBox(
            width_policy=SizePolicy.HUG,
            height_policy=SizePolicy.HUG,
            children=[_pad(), _pad(), _pad()],
            gap=-0.5,
        )
        # 3 + 2*(-0.5) = 2
        assert intrinsic_size(hbox) == Size(2.0, 1.0)

    def test_height_is_max_of_children(self) -> None:
        tall = StripAtom(
            width_policy=SizePolicy.HUG,
            height_policy=SizePolicy.HUG,
            length=3,
            orientation="vertical",
            layers=PadLayers(),
        )
        hbox = HBox(
            width_policy=SizePolicy.HUG,
            height_policy=SizePolicy.HUG,
            children=[_pad(), tall, _pad()],
            gap=1.0,
        )
        # widths: 1+1+1 + 2 gaps = 5; heights: max(1, 3, 1) = 3
        assert intrinsic_size(hbox) == Size(5.0, 3.0)

    def test_empty_children_intrinsic_zero(self) -> None:
        hbox = HBox(
            width_policy=SizePolicy.HUG,
            height_policy=SizePolicy.HUG,
            children=[],
        )
        assert intrinsic_size(hbox) == Size(0.0, 0.0)


class TestVBoxIntrinsic:
    def test_three_pads_default_gap(self) -> None:
        vbox = VBox(
            width_policy=SizePolicy.HUG,
            height_policy=SizePolicy.HUG,
            children=[_pad(), _pad(), _pad()],
            gap=1.0,
        )
        assert intrinsic_size(vbox) == Size(1.0, 5.0)

    def test_width_is_max_of_children(self) -> None:
        wide = StripAtom(
            width_policy=SizePolicy.HUG,
            height_policy=SizePolicy.HUG,
            length=10,
            orientation="horizontal",
            layers=PadLayers(),
        )
        vbox = VBox(
            width_policy=SizePolicy.HUG,
            height_policy=SizePolicy.HUG,
            children=[_pad(), wide, _pad()],
            gap=0.0,
        )
        # heights: 3, widths: max(1, 10, 1) = 10
        assert intrinsic_size(vbox) == Size(10.0, 3.0)


class TestBoardGridIntrinsic:
    def test_simple_grid(self) -> None:
        grid = BoardGrid(
            width_policy=SizePolicy.HUG,
            height_policy=SizePolicy.HUG,
            cell=_pad(),
            rows=20,
            cols=30,
            row_gap=1.0,
            col_gap=1.0,
        )
        # cols * cell.width + (cols-1) * col_gap = 30 + 29 = 59
        # rows * cell.height + (rows-1) * row_gap = 20 + 19 = 39
        assert intrinsic_size(grid) == Size(59.0, 39.0)

    def test_grid_zero_gap(self) -> None:
        grid = BoardGrid(
            width_policy=SizePolicy.HUG,
            height_policy=SizePolicy.HUG,
            cell=_pad(),
            rows=10,
            cols=10,
            row_gap=0.0,
            col_gap=0.0,
        )
        assert intrinsic_size(grid) == Size(10.0, 10.0)

    def test_grid_one_cell(self) -> None:
        grid = BoardGrid(
            width_policy=SizePolicy.HUG,
            height_policy=SizePolicy.HUG,
            cell=_pad(),
            rows=1,
            cols=1,
        )
        # No gaps with single cell.
        assert intrinsic_size(grid) == Size(1.0, 1.0)


class TestNestedIntrinsic:
    def test_vbox_of_hboxes(self) -> None:
        row3 = HBox(
            width_policy=SizePolicy.HUG,
            height_policy=SizePolicy.HUG,
            children=[_pad(), _pad(), _pad()],
            gap=1.0,
        )
        row5 = HBox(
            width_policy=SizePolicy.HUG,
            height_policy=SizePolicy.HUG,
            children=[_pad()] * 5,
            gap=1.0,
        )
        vbox = VBox(
            width_policy=SizePolicy.HUG,
            height_policy=SizePolicy.HUG,
            children=[row3, row5],
            gap=1.0,
        )
        # row3 width = 5, row5 width = 9; vbox width = max = 9
        # heights: 1 + 1 + 1 (gap) = 3
        assert intrinsic_size(vbox) == Size(9.0, 3.0)


class TestFixedNodeIntrinsic:
    def test_fixed_box_reports_its_fixed_size(self) -> None:
        # A FIXED node's intrinsic equals its declared size, regardless of children.
        hbox = HBox(
            width_policy=SizePolicy.FIXED,
            height_policy=SizePolicy.FIXED,
            width=10.0,
            height=5.0,
            children=[_pad()],
        )
        assert intrinsic_size(hbox) == Size(10.0, 5.0)


class TestMarginIntrinsicEffect:
    def test_positive_margin_does_not_change_intrinsic(self) -> None:
        # Per CLAUDE.md §4.2 + plan §10.7: margins do not affect intrinsic size
        # of the bounding box; they only shift positions during the layout pass.
        from protoboard.geometry import Insets

        pad_with_margin = PadAtom(
            width_policy=SizePolicy.HUG,
            height_policy=SizePolicy.HUG,
            layers=PadLayers(),
            margin=Insets.all(0.5),
        )
        # Intrinsic of the pad itself is still 1×1.
        assert intrinsic_size(pad_with_margin) == Size(1.0, 1.0)
