# SPDX-License-Identifier: MIT
"""Tests for the main layout pass — assigns positions (atom centers) and
propagates resolved sizes downward through the tree.

Convention: a node's (x, y) is the **center** of its bounding box.
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
from protoboard.domain.board.layout_engine import LayoutNode, layout
from protoboard.domain.board.pads import PadLayers


def _pad(label: str | None = None) -> PadAtom:
    return PadAtom(
        width_policy=SizePolicy.HUG,
        height_policy=SizePolicy.HUG,
        layers=PadLayers(),
        label=label,
    )


def _approx_equal(a: float, b: float, tol: float = 1e-9) -> bool:
    return abs(a - b) <= tol


def _pos_equal(node: LayoutNode, x: float, y: float, tol: float = 1e-9) -> bool:
    return _approx_equal(node.x, x, tol) and _approx_equal(node.y, y, tol)


class TestSinglePadLayout:
    def test_single_pad(self) -> None:
        result = layout(_pad())
        # Center of a 1×1 pad placed at origin is (0.5, 0.5).
        assert _pos_equal(result, 0.5, 0.5)
        assert result.width == 1.0
        assert result.height == 1.0


class TestHBoxLayout:
    def test_three_pads_gap_one(self) -> None:
        hbox = HBox(
            width_policy=SizePolicy.HUG,
            height_policy=SizePolicy.HUG,
            children=[_pad(), _pad(), _pad()],
            gap=1.0,
        )
        result = layout(hbox)
        assert result.width == 5.0
        assert result.height == 1.0
        assert len(result.children) == 3
        # Centers along x: 0.5, 2.5, 4.5
        assert _pos_equal(result.children[0], 0.5, 0.5)
        assert _pos_equal(result.children[1], 2.5, 0.5)
        assert _pos_equal(result.children[2], 4.5, 0.5)

    def test_two_pads_fractional_gap(self) -> None:
        hbox = HBox(
            width_policy=SizePolicy.HUG,
            height_policy=SizePolicy.HUG,
            children=[_pad(), _pad()],
            gap=0.5,
        )
        result = layout(hbox)
        # First pad center: 0.5; gap=0.5 means next pad starts at 1.5; center 2.0.
        assert _pos_equal(result.children[0], 0.5, 0.5)
        assert _pos_equal(result.children[1], 2.0, 0.5)

    def test_two_pads_negative_gap_overlap(self) -> None:
        hbox = HBox(
            width_policy=SizePolicy.HUG,
            height_policy=SizePolicy.HUG,
            children=[_pad(), _pad()],
            gap=-0.5,
        )
        result = layout(hbox)
        # First pad center: 0.5; gap=-0.5 means next pad starts at 0.5; center 1.0.
        assert _pos_equal(result.children[0], 0.5, 0.5)
        assert _pos_equal(result.children[1], 1.0, 0.5)

    def test_zero_gap_pads_touch(self) -> None:
        hbox = HBox(
            width_policy=SizePolicy.HUG,
            height_policy=SizePolicy.HUG,
            children=[_pad(), _pad()],
            gap=0.0,
        )
        result = layout(hbox)
        assert _pos_equal(result.children[0], 0.5, 0.5)
        assert _pos_equal(result.children[1], 1.5, 0.5)


class TestVBoxLayout:
    def test_three_pads(self) -> None:
        vbox = VBox(
            width_policy=SizePolicy.HUG,
            height_policy=SizePolicy.HUG,
            children=[_pad(), _pad(), _pad()],
            gap=1.0,
        )
        result = layout(vbox)
        assert result.width == 1.0
        assert result.height == 5.0
        assert _pos_equal(result.children[0], 0.5, 0.5)
        assert _pos_equal(result.children[1], 0.5, 2.5)
        assert _pos_equal(result.children[2], 0.5, 4.5)


class TestBoardGridLayout:
    def test_3x3_pad_grid(self) -> None:
        grid = BoardGrid(
            width_policy=SizePolicy.HUG,
            height_policy=SizePolicy.HUG,
            cell=_pad(),
            rows=3,
            cols=3,
            row_gap=1.0,
            col_gap=1.0,
        )
        result = layout(grid)
        # Grid has 9 children (one per cell), arranged row-major.
        assert len(result.children) == 9
        expected_centers = [(x + 0.5, y + 0.5) for y in (0, 2, 4) for x in (0, 2, 4)]
        actual_centers = [(c.x, c.y) for c in result.children]
        for expected, actual in zip(expected_centers, actual_centers, strict=True):
            assert _approx_equal(expected[0], actual[0])
            assert _approx_equal(expected[1], actual[1])

    def test_perfboard_10x10_corner_positions(self) -> None:
        # Perfboard convention: pads on a 1-raster pitch (no extra gap between
        # pad bounding boxes). row_gap/col_gap = 0 → pad centers at integer + 0.5.
        grid = BoardGrid(
            width_policy=SizePolicy.HUG,
            height_policy=SizePolicy.HUG,
            cell=_pad(),
            rows=10,
            cols=10,
            row_gap=0.0,
            col_gap=0.0,
        )
        result = layout(grid)
        assert len(result.children) == 100
        # Top-left pad center
        assert _pos_equal(result.children[0], 0.5, 0.5)
        # Top-right (col 9, row 0)
        assert _pos_equal(result.children[9], 9.5, 0.5)
        # Bottom-left (col 0, row 9)
        assert _pos_equal(result.children[90], 0.5, 9.5)
        # Bottom-right (col 9, row 9)
        assert _pos_equal(result.children[99], 9.5, 9.5)


class TestStripboardLayout:
    def test_10x10_horizontal_strips(self) -> None:
        strip = StripAtom(
            width_policy=SizePolicy.HUG,
            height_policy=SizePolicy.HUG,
            length=10,
            orientation="horizontal",
            layers=PadLayers(),
        )
        # 10 horizontal strips stacked vertically.
        vbox = VBox(
            width_policy=SizePolicy.HUG,
            height_policy=SizePolicy.HUG,
            children=[strip] * 10,
            gap=1.0,
        )
        result = layout(vbox)
        assert result.width == 10.0
        assert result.height == 19.0  # 10 + 9 gaps
        # First strip center: x = 5 (mid of 10), y = 0.5
        assert _pos_equal(result.children[0], 5.0, 0.5)
        # Last strip center: y = 18.5
        assert _pos_equal(result.children[9], 5.0, 18.5)


class TestNestedLayout:
    def test_vbox_of_hboxes_relative_positions(self) -> None:
        row1 = HBox(
            width_policy=SizePolicy.HUG,
            height_policy=SizePolicy.HUG,
            children=[_pad(), _pad()],
            gap=1.0,
        )
        row2 = HBox(
            width_policy=SizePolicy.HUG,
            height_policy=SizePolicy.HUG,
            children=[_pad(), _pad()],
            gap=1.0,
        )
        vbox = VBox(
            width_policy=SizePolicy.HUG,
            height_policy=SizePolicy.HUG,
            children=[row1, row2],
            gap=1.0,
        )
        result = layout(vbox)
        # Inspect that pad centers are absolute (world) coordinates.
        # row1 pads: (0.5, 0.5), (2.5, 0.5)
        # row2 pads: (0.5, 2.5), (2.5, 2.5)
        row1_node = result.children[0]
        row2_node = result.children[1]
        assert _pos_equal(row1_node.children[0], 0.5, 0.5)
        assert _pos_equal(row1_node.children[1], 2.5, 0.5)
        assert _pos_equal(row2_node.children[0], 0.5, 2.5)
        assert _pos_equal(row2_node.children[1], 2.5, 2.5)


class TestChildrenOrderPreserved:
    def test_layout_children_match_box_children_order(self) -> None:
        labels = ["a", "b", "c", "d"]
        hbox = HBox(
            width_policy=SizePolicy.HUG,
            height_policy=SizePolicy.HUG,
            children=[_pad(label=lbl) for lbl in labels],
            gap=1.0,
        )
        result = layout(hbox)
        for expected_label, child in zip(labels, result.children, strict=True):
            assert isinstance(child.box, PadAtom)
            assert child.box.label == expected_label


class TestGapAtomInLayout:
    def test_gap_atom_takes_space_but_no_children(self) -> None:
        gap = GapAtom(
            width_policy=SizePolicy.FIXED,
            height_policy=SizePolicy.FIXED,
            width=2.0,
            height=1.0,
        )
        hbox = HBox(
            width_policy=SizePolicy.HUG,
            height_policy=SizePolicy.HUG,
            children=[_pad(), gap, _pad()],
            gap=0.0,
        )
        result = layout(hbox)
        # widths: 1 + 2 + 1 = 4
        assert result.width == 4.0
        # Pad center 0.5, gap center 2.0, pad center 3.5
        assert _pos_equal(result.children[0], 0.5, 0.5)
        assert _pos_equal(result.children[1], 2.0, 0.5)
        assert _pos_equal(result.children[2], 3.5, 0.5)


class TestDeterminism:
    def test_same_tree_yields_same_layout(self) -> None:
        def make() -> HBox:
            return HBox(
                width_policy=SizePolicy.HUG,
                height_policy=SizePolicy.HUG,
                children=[_pad(), _pad(), _pad()],
                gap=0.5,
            )

        a = layout(make())
        b = layout(make())
        # Compare positions of all children pairwise.
        for child_a, child_b in zip(a.children, b.children, strict=True):
            assert child_a.x == child_b.x
            assert child_a.y == child_b.y
            assert child_a.width == child_b.width
            assert child_a.height == child_b.height


class TestFixedRootDimensions:
    def test_fixed_root_uses_declared_size(self) -> None:
        hbox = HBox(
            width_policy=SizePolicy.FIXED,
            height_policy=SizePolicy.FIXED,
            width=20.0,
            height=10.0,
            children=[_pad(), _pad(), _pad()],
            gap=1.0,
        )
        result = layout(hbox)
        # Root reports the FIXED size, even though children only need 5×1.
        assert result.width == 20.0
        assert result.height == 10.0


class TestVerticalAlignmentInHBox:
    def test_short_pad_in_tall_hbox_centers_or_top_aligns(self) -> None:
        # A pad (1×1) inside an HBox containing a vertical strip (1×3).
        # Expectation: each child is positioned starting at y=0 and gets its
        # intrinsic height; child y-center is height/2 of the child itself.
        # (We don't auto-center; layout pass simply places top-left at parent's
        # content origin.)
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
            children=[_pad(), tall],
            gap=1.0,
        )
        result = layout(hbox)
        assert _pos_equal(result.children[0], 0.5, 0.5)
        # Tall strip top-left starts at (2, 0); center at (2.5, 1.5).
        assert _pos_equal(result.children[1], 2.5, 1.5)
