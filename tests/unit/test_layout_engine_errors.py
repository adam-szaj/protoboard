# SPDX-License-Identifier: MIT
"""Tests for LayoutError conditions in the layout engine."""

import pytest
from protoboard.domain.board.boxes import (
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


class TestFixedTooSmall:
    def test_fixed_parent_smaller_than_hug_children_raises(self) -> None:
        hbox = HBox(
            width_policy=SizePolicy.FIXED,
            height_policy=SizePolicy.HUG,
            width=2.0,
            children=[_pad(), _pad()],
            gap=1.0,
        )
        # Children intrinsic = 2 (pads) + 1 (gap) = 3 > 2.
        with pytest.raises(LayoutError):
            layout(hbox)

    def test_fixed_height_too_small_in_vbox_raises(self) -> None:
        vbox = VBox(
            width_policy=SizePolicy.HUG,
            height_policy=SizePolicy.FIXED,
            height=1.0,
            children=[_pad(), _pad()],
            gap=1.0,
        )
        # Children intrinsic height = 3, FIXED height = 1.
        with pytest.raises(LayoutError):
            layout(vbox)


class TestFillOnRoot:
    def test_fill_root_hbox_raises(self) -> None:
        hbox = HBox(
            width_policy=SizePolicy.FILL,
            height_policy=SizePolicy.HUG,
            children=[_pad()],
        )
        with pytest.raises(LayoutError):
            layout(hbox)

    def test_fill_root_vbox_raises(self) -> None:
        vbox = VBox(
            width_policy=SizePolicy.HUG,
            height_policy=SizePolicy.FILL,
            children=[_pad()],
        )
        with pytest.raises(LayoutError):
            layout(vbox)

    def test_fill_root_strip_raises(self) -> None:
        strip = StripAtom(
            width_policy=SizePolicy.FILL,
            height_policy=SizePolicy.HUG,
            length="fill",
            orientation="horizontal",
            layers=PadLayers(),
        )
        with pytest.raises(LayoutError):
            layout(strip)


class TestMultipleFillSiblings:
    def test_two_width_fill_in_hbox_raises(self) -> None:
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
            width=10.0,
            children=[a, b],
        )
        with pytest.raises(LayoutError):
            layout(hbox)


class TestFillWithoutBoundedParent:
    def test_fill_in_hug_parent_raises(self) -> None:
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
            children=[strip],
        )
        with pytest.raises(LayoutError):
            layout(hbox)


class TestEmptyContainersAreLegal:
    def test_empty_hbox_layout_is_zero_size(self) -> None:
        hbox = HBox(
            width_policy=SizePolicy.HUG,
            height_policy=SizePolicy.HUG,
            children=[],
        )
        result = layout(hbox)
        assert result.size.lx == 0.0
        assert result.size.ly == 0.0

    def test_empty_vbox_layout_is_zero_size(self) -> None:
        vbox = VBox(
            width_policy=SizePolicy.HUG,
            height_policy=SizePolicy.HUG,
            children=[],
        )
        result = layout(vbox)
        assert result.size.lx == 0.0
        assert result.size.ly == 0.0


class TestErrorMessages:
    def test_layout_error_inherits_from_protoboard_error(self) -> None:
        # LayoutError must be catchable as ProtoboardError per CLAUDE.md §14.
        from protoboard.errors import ProtoboardError

        assert issubclass(LayoutError, ProtoboardError)
