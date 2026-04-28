# SPDX-License-Identifier: MIT
"""Tests for FILL space distribution in the layout pass.

Rules (per project plan):
- FILL requires a bounded parent (FIXED, or FILL inside a bounded ancestor).
- At most one FILL sibling per dimension within a single HBox/VBox/BoardGrid;
  multiple FILL siblings in the same dimension is ambiguous → LayoutError.
- FILL in the perpendicular dimension can repeat (each child gets full size).
"""

import pytest
from protoboard.domain.board.boxes import (
    BoardGrid,
    HBox,
    PadAtom,
    SizePolicy,
    StripAtom,
    VBox,
)
from protoboard.domain.board.layout_engine import LayoutError, layout
from protoboard.domain.board.pads import PadLayers


def _pad() -> PadAtom:
    return PadAtom(
        width_policy=SizePolicy.HUG,
        height_policy=SizePolicy.HUG,
        layers=PadLayers(),
    )


def _approx_equal(a: float, b: float, tol: float = 1e-9) -> bool:
    return abs(a - b) <= tol


class TestSingleFillWidth:
    def test_fill_strip_after_pad(self) -> None:
        # HBox FIXED width=10: [Pad(HUG=1), FILL strip], gap=1
        # FILL strip gets 10 - 1 - 1(gap) = 8.
        strip = StripAtom(
            width_policy=SizePolicy.FILL,
            height_policy=SizePolicy.HUG,
            length="fill",
            orientation="horizontal",
            layers=PadLayers(),
        )
        hbox = HBox(
            width_policy=SizePolicy.FIXED,
            height_policy=SizePolicy.HUG,
            width=10.0,
            children=[_pad(), strip],
            gap=1.0,
        )
        result = layout(hbox)
        pad_node = result.children[0]
        strip_node = result.children[1]
        assert _approx_equal(pad_node.x, 0.5)
        # Strip top-left at x=2 (after pad@1 + gap@1), width=8, center x=6.
        assert _approx_equal(strip_node.width, 8.0)
        assert _approx_equal(strip_node.x, 6.0)

    def test_fill_strip_between_pads(self) -> None:
        # HBox FIXED 12: [Pad, FILL strip, Pad], gap=1
        # FILL gets 12 - 1 - 1 - 2(gaps) = 8.
        strip = StripAtom(
            width_policy=SizePolicy.FILL,
            height_policy=SizePolicy.HUG,
            length="fill",
            orientation="horizontal",
            layers=PadLayers(),
        )
        hbox = HBox(
            width_policy=SizePolicy.FIXED,
            height_policy=SizePolicy.HUG,
            width=12.0,
            children=[_pad(), strip, _pad()],
            gap=1.0,
        )
        result = layout(hbox)
        strip_node = result.children[1]
        assert _approx_equal(strip_node.width, 8.0)
        # Strip top-left at x=2 (pad@1 + gap@1), so center at 2 + 4 = 6.
        assert _approx_equal(strip_node.x, 6.0)


class TestMultipleFillSameDimensionForbidden:
    def test_two_fill_widths_in_hbox_raises(self) -> None:
        a = StripAtom(
            width_policy=SizePolicy.FILL,
            height_policy=SizePolicy.HUG,
            length="fill",
            orientation="horizontal",
            layers=PadLayers(),
        )
        b = StripAtom(
            width_policy=SizePolicy.FILL,
            height_policy=SizePolicy.HUG,
            length="fill",
            orientation="horizontal",
            layers=PadLayers(),
        )
        hbox = HBox(
            width_policy=SizePolicy.FIXED,
            height_policy=SizePolicy.HUG,
            width=12.0,
            children=[a, b],
            gap=1.0,
        )
        with pytest.raises(LayoutError):
            layout(hbox)

    def test_two_fill_heights_in_vbox_raises(self) -> None:
        a = StripAtom(
            width_policy=SizePolicy.HUG,
            height_policy=SizePolicy.FILL,
            length="fill",
            orientation="vertical",
            layers=PadLayers(),
        )
        b = StripAtom(
            width_policy=SizePolicy.HUG,
            height_policy=SizePolicy.FILL,
            length="fill",
            orientation="vertical",
            layers=PadLayers(),
        )
        vbox = VBox(
            width_policy=SizePolicy.HUG,
            height_policy=SizePolicy.FIXED,
            height=10.0,
            children=[a, b],
            gap=1.0,
        )
        with pytest.raises(LayoutError):
            layout(vbox)


class TestPerpendicularFillIsAllowed:
    def test_two_height_fill_in_hbox_each_get_full_height(self) -> None:
        # HBox FIXED width=10, FIXED height=5.
        # Two children with height_policy=FILL — each gets the full height (5),
        # no competition (FILL in perpendicular dim doesn't divide).
        a = StripAtom(
            width_policy=SizePolicy.HUG,
            height_policy=SizePolicy.FILL,
            length="fill",
            orientation="vertical",
            layers=PadLayers(),
        )
        b = StripAtom(
            width_policy=SizePolicy.HUG,
            height_policy=SizePolicy.FILL,
            length="fill",
            orientation="vertical",
            layers=PadLayers(),
        )
        hbox = HBox(
            width_policy=SizePolicy.FIXED,
            height_policy=SizePolicy.FIXED,
            width=10.0,
            height=5.0,
            children=[a, b],
            gap=1.0,
        )
        result = layout(hbox)
        for child in result.children:
            assert _approx_equal(child.height, 5.0)


class TestFillBothDimensions:
    def test_grid_fill_in_both_dims(self) -> None:
        grid = BoardGrid(
            width_policy=SizePolicy.FILL,
            height_policy=SizePolicy.FILL,
            cell=PadAtom(
                width_policy=SizePolicy.HUG,
                height_policy=SizePolicy.HUG,
                layers=PadLayers(),
            ),
            rows=3,
            cols=3,
            row_gap=1.0,
            col_gap=1.0,
        )
        outer = HBox(
            width_policy=SizePolicy.FIXED,
            height_policy=SizePolicy.FIXED,
            width=30.0,
            height=20.0,
            children=[grid],
        )
        result = layout(outer)
        grid_node = result.children[0]
        assert _approx_equal(grid_node.width, 30.0)
        assert _approx_equal(grid_node.height, 20.0)


class TestFillWithoutBoundedParent:
    def test_fill_inside_hug_parent_raises(self) -> None:
        strip = StripAtom(
            width_policy=SizePolicy.FILL,
            height_policy=SizePolicy.HUG,
            length="fill",
            orientation="horizontal",
            layers=PadLayers(),
        )
        hbox = HBox(
            width_policy=SizePolicy.HUG,
            height_policy=SizePolicy.HUG,
            children=[_pad(), strip, _pad()],
            gap=1.0,
        )
        with pytest.raises(LayoutError):
            layout(hbox)

    def test_strip_fill_at_root_raises(self) -> None:
        strip = StripAtom(
            width_policy=SizePolicy.FILL,
            height_policy=SizePolicy.HUG,
            length="fill",
            orientation="horizontal",
            layers=PadLayers(),
        )
        with pytest.raises(LayoutError):
            layout(strip)


class TestBoardGridCellFill:
    def test_cell_fill_distributes_parent_size(self) -> None:
        # Cell width FILL — cell width = (parent_width - col_gap*(cols-1)) / cols
        # Parent FIXED width=10, cols=4, col_gap=1 → cell_width = (10 - 3) / 4 = 1.75
        cell = PadAtom(
            width_policy=SizePolicy.FILL,
            height_policy=SizePolicy.HUG,
            layers=PadLayers(),
        )
        grid = BoardGrid(
            width_policy=SizePolicy.FIXED,
            height_policy=SizePolicy.HUG,
            width=10.0,
            cell=cell,
            rows=1,
            cols=4,
            col_gap=1.0,
            row_gap=1.0,
        )
        result = layout(grid)
        for child in result.children:
            assert _approx_equal(child.width, 1.75)
