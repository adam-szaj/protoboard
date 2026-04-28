# SPDX-License-Identifier: MIT
"""Tests for margin handling in the layout pass.

Per CLAUDE.md §4.2: margins may be negative. They shift the rendered position
but do not change the intrinsic size of the box (the parent's layout reasons
as if the margin were zero, then applies the margin offset to the final
position).
"""

from protoboard.domain.board.boxes import (
    HBox,
    PadAtom,
    SizePolicy,
    StripAtom,
    VBox,
)
from protoboard.domain.board.layout_engine import layout
from protoboard.domain.board.pads import PadLayers
from protoboard.geometry import Insets


def _pad(margin: Insets | None = None) -> PadAtom:
    return PadAtom(
        width_policy=SizePolicy.HUG,
        height_policy=SizePolicy.HUG,
        layers=PadLayers(),
        margin=margin if margin is not None else Insets.zero(),
    )


def _approx_equal(a: float, b: float, tol: float = 1e-9) -> bool:
    return abs(a - b) <= tol


class TestPositiveMargin:
    def test_left_margin_shifts_right(self) -> None:
        # Pad with left=0.5 in HBox HUG: pad effectively occupies x=[0.5, 1.5].
        # Parent's intrinsic width should account for the margin.
        pad = _pad(margin=Insets(top=0.0, right=0.0, bottom=0.0, left=0.5))
        hbox = HBox(
            width_policy=SizePolicy.HUG,
            height_policy=SizePolicy.HUG,
            children=[pad],
        )
        result = layout(hbox)
        # Pad center: 0.5 + 0.5 = 1.0
        assert _approx_equal(result.children[0].x, 1.0)
        # Parent intrinsic width: 1 (pad) + 0.5 (left) = 1.5.
        assert _approx_equal(result.width, 1.5)

    def test_top_margin_shifts_down(self) -> None:
        pad = _pad(margin=Insets(top=0.5, right=0.0, bottom=0.0, left=0.0))
        vbox = VBox(
            width_policy=SizePolicy.HUG,
            height_policy=SizePolicy.HUG,
            children=[pad],
        )
        result = layout(vbox)
        assert _approx_equal(result.children[0].y, 1.0)
        assert _approx_equal(result.height, 1.5)


class TestNegativeMargin:
    def test_negative_top_margin_overlaps_previous(self) -> None:
        # Two strips in VBox(gap=0); second has top=-0.5 → overlaps the first by 0.5.
        strip1 = StripAtom(
            width_policy=SizePolicy.HUG,
            height_policy=SizePolicy.HUG,
            length=10,
            orientation="horizontal",
            layers=PadLayers(),
        )
        strip2 = StripAtom(
            width_policy=SizePolicy.HUG,
            height_policy=SizePolicy.HUG,
            length=10,
            orientation="horizontal",
            layers=PadLayers(),
            margin=Insets(top=-0.5, right=0.0, bottom=0.0, left=0.0),
        )
        vbox = VBox(
            width_policy=SizePolicy.HUG,
            height_policy=SizePolicy.HUG,
            children=[strip1, strip2],
            gap=0.0,
        )
        result = layout(vbox)
        # First strip center: y = 0.5.
        assert _approx_equal(result.children[0].y, 0.5)
        # Second strip top-left would be at y=1; margin.top=-0.5 shifts to 0.5;
        # height=1 gives center y = 1.0 (overlapping the first strip by 0.5).
        assert _approx_equal(result.children[1].y, 1.0)


class TestBreadboardRailExample:
    def test_rails_with_negative_margin(self) -> None:
        # Per CLAUDE.md §4.2 breadboard example: second rail with margin.top=-0.5
        # should overlap the first by 0.5.
        rail1 = StripAtom(
            width_policy=SizePolicy.HUG,
            height_policy=SizePolicy.HUG,
            length=30,
            orientation="horizontal",
            layers=PadLayers(),
            label="+",
        )
        rail2 = StripAtom(
            width_policy=SizePolicy.HUG,
            height_policy=SizePolicy.HUG,
            length=30,
            orientation="horizontal",
            layers=PadLayers(),
            label="-",
            margin=Insets(top=-0.5, right=0.0, bottom=0.0, left=0.0),
        )
        vbox = VBox(
            width_policy=SizePolicy.HUG,
            height_policy=SizePolicy.HUG,
            children=[rail1, rail2],
            gap=0.0,
        )
        result = layout(vbox)
        # Rail 1 center y = 0.5; rail 2 normally at y=1, but margin pulls to 0.5,
        # so center y = 1.0.
        assert _approx_equal(result.children[0].y, 0.5)
        assert _approx_equal(result.children[1].y, 1.0)


class TestMarginsInAllFourDirections:
    def test_each_direction_independently(self) -> None:
        pad = _pad(margin=Insets(top=0.25, right=0.5, bottom=0.75, left=1.0))
        hbox = HBox(
            width_policy=SizePolicy.HUG,
            height_policy=SizePolicy.HUG,
            children=[pad],
        )
        result = layout(hbox)
        # Pad center x = 1.0(left) + 0.5(half-pad) = 1.5
        # Pad center y = 0.25(top) + 0.5(half-pad) = 0.75
        assert _approx_equal(result.children[0].x, 1.5)
        assert _approx_equal(result.children[0].y, 0.75)
        # Parent intrinsic width = 1 + 1.0(left) + 0.5(right) = 2.5
        # Parent intrinsic height = 1 + 0.25(top) + 0.75(bottom) = 2.0
        assert _approx_equal(result.width, 2.5)
        assert _approx_equal(result.height, 2.0)
