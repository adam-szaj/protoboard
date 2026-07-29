# SPDX-License-Identifier: MIT
"""Property-based tests for the layout engine using hypothesis.

Invariants checked (per project plan §5):
1. All atom positions non-negative when gaps and margins are non-negative.
2. Atoms do not overlap when gaps >= 0.
3. Sum of children + gaps <= parent size for FIXED HBox without FILL.
4. layout() is deterministic.
"""

from hypothesis import given, settings
from hypothesis import strategies as st

from protoboard.domain.board.boxes import (
    Box,
    HBox,
    PadAtom,
    SizePolicy,
    VBox,
)
from protoboard.domain.board.layout_engine import LayoutError, LayoutNode, layout
from protoboard.domain.board.pads import PadLayers


def _pad() -> PadAtom:
    return PadAtom(
        width_policy=SizePolicy.HUG,
        height_policy=SizePolicy.HUG,
        layers=PadLayers(),
    )


# ---------------------------------------------------------------------------
# Strategies
# ---------------------------------------------------------------------------


@st.composite
def _box_tree(draw: st.DrawFn, max_depth: int = 3) -> Box:
    """Generate a HUG-only tree of HBox/VBox/PadAtom with non-negative gaps."""
    if max_depth == 0 or draw(st.booleans()):
        return _pad()

    container_type = draw(st.sampled_from([HBox, VBox]))
    n_children = draw(st.integers(min_value=0, max_value=4))
    children: list[Box] = [draw(_box_tree(max_depth - 1)) for _ in range(n_children)]
    gap = draw(st.floats(min_value=0.0, max_value=3.0, allow_nan=False))
    return container_type(
        width_policy=SizePolicy.HUG,
        height_policy=SizePolicy.HUG,
        children=children,
        gap=gap,
    )


@st.composite
def _box_tree_negative_gaps(draw: st.DrawFn, max_depth: int = 3) -> Box:
    """Tree allowing negative gaps — for determinism check only."""
    if max_depth == 0 or draw(st.booleans()):
        return _pad()

    container_type = draw(st.sampled_from([HBox, VBox]))
    n_children = draw(st.integers(min_value=0, max_value=4))
    children: list[Box] = [
        draw(_box_tree_negative_gaps(max_depth - 1)) for _ in range(n_children)
    ]
    gap = draw(st.floats(min_value=-1.0, max_value=3.0, allow_nan=False))
    return container_type(
        width_policy=SizePolicy.HUG,
        height_policy=SizePolicy.HUG,
        children=children,
        gap=gap,
    )


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def _all_atom_nodes(node: LayoutNode) -> list[LayoutNode]:
    """Return a flat list of leaf (atom) nodes in DFS order."""
    if not node.children:
        return [node]
    out: list[LayoutNode] = []
    for child in node.children:
        out.extend(_all_atom_nodes(child))
    return out


def _bbox(node: LayoutNode) -> tuple[float, float, float, float]:
    """(x_min, y_min, x_max, y_max) — node.x/y are centers."""
    half_w = node.size.lx / 2.0
    half_h = node.size.ly / 2.0
    return (node.center.x - half_w, node.center.y - half_h, node.center.x + half_w, node.center.y + half_h)


def _rects_overlap(
    a: tuple[float, float, float, float],
    b: tuple[float, float, float, float],
    tol: float = 1e-9,
) -> bool:
    return (
        a[0] + tol < b[2]
        and b[0] + tol < a[2]
        and a[1] + tol < b[3]
        and b[1] + tol < a[3]
    )


def _layouts_equal(a: LayoutNode, b: LayoutNode, tol: float = 1e-9) -> bool:
    if abs(a.center.x - b.center.x) > tol or abs(a.center.y - b.center.y) > tol:
        return False
    if abs(a.size.lx - b.size.lx) > tol or abs(a.size.ly - b.size.ly) > tol:
        return False
    if len(a.children) != len(b.children):
        return False
    return all(
        _layouts_equal(ca, cb, tol)
        for ca, cb in zip(a.children, b.children, strict=True)
    )


# ---------------------------------------------------------------------------
# Properties
# ---------------------------------------------------------------------------


class TestLayoutInvariants:
    @given(_box_tree())
    @settings(max_examples=80, deadline=None)
    def test_atom_positions_non_negative(self, box: Box) -> None:
        result = layout(box)
        for atom in _all_atom_nodes(result):
            x_min, y_min, _, _ = _bbox(atom)
            assert x_min >= -1e-9
            assert y_min >= -1e-9

    @given(_box_tree())
    @settings(max_examples=80, deadline=None)
    def test_atoms_do_not_overlap(self, box: Box) -> None:
        result = layout(box)
        atoms = _all_atom_nodes(result)
        for i, a in enumerate(atoms):
            for b in atoms[i + 1 :]:
                assert not _rects_overlap(_bbox(a), _bbox(b))

    @given(_box_tree())
    @settings(max_examples=80, deadline=None)
    def test_layout_is_deterministic(self, box: Box) -> None:
        a = layout(box)
        b = layout(box)
        assert _layouts_equal(a, b)


class TestNegativeGapDeterminism:
    @given(_box_tree_negative_gaps())
    @settings(max_examples=60, deadline=None)
    def test_negative_gaps_layout_still_deterministic(self, box: Box) -> None:
        # Negative gaps may produce content sizes that are not representable
        # (e.g. negative outer width on an otherwise-empty container).
        # Determinism still holds: either both runs raise the same error, or
        # both succeed with identical layouts.
        try:
            a = layout(box)
        except (ValueError, LayoutError) as exc_a:
            try:
                layout(box)
            except (ValueError, LayoutError) as exc_b:
                assert type(exc_a) is type(exc_b)
                return
            raise AssertionError("first call raised, second did not") from None
        b = layout(box)
        assert _layouts_equal(a, b)


class TestFixedFitsChildren:
    @given(
        children=st.integers(min_value=1, max_value=6),
        gap=st.floats(min_value=0.0, max_value=2.0, allow_nan=False),
        slack=st.floats(min_value=0.0, max_value=5.0, allow_nan=False),
    )
    @settings(max_examples=60, deadline=None)
    def test_fixed_with_enough_space_fits(
        self, children: int, gap: float, slack: float
    ) -> None:
        # FIXED HBox sized to intrinsic + slack.
        intrinsic = children + (children - 1) * gap
        hbox = HBox(
            width_policy=SizePolicy.FIXED,
            height_policy=SizePolicy.HUG,
            width=intrinsic + slack,
            children=[_pad() for _ in range(children)],
            gap=gap,
        )
        result = layout(hbox)
        # Sum of child widths + gaps does not exceed parent width.
        total = sum(c.size.lx for c in result.children) + (children - 1) * gap
        assert total <= result.size.lx + 1e-9
