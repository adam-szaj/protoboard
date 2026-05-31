# SPDX-License-Identifier: MIT
"""Tests for wire_utils — insert_pad_nodes and board pad enumeration."""

from __future__ import annotations

import pytest

from protoboard.domain.board.board import Board
from protoboard.domain.board.boxes import BoardGrid, PadAtom, SizePolicy
from protoboard.domain.board.pads import PadLayers
from protoboard.domain.layout.wire import Fragment, Wire
from protoboard.domain.layout.wire_utils import (
    board_pads_for_side,
    coalesce_same_side_fragments,
    find_skewed_wires,
    insert_pad_nodes,
    plated_through_pads,
    wire_has_skewed_segment,
)
from protoboard.geometry import Point


def _perfboard(rows: int, cols: int) -> Board:
    grid = BoardGrid(
        width_policy=SizePolicy.HUG,
        height_policy=SizePolicy.HUG,
        cell=PadAtom(
            width_policy=SizePolicy.HUG,
            height_policy=SizePolicy.HUG,
            layers=PadLayers(top=True, bottom=True, plated_through=True),
        ),
        rows=rows,
        cols=cols,
        row_gap=0.0,
        col_gap=0.0,
    )
    return Board(root=grid)


class TestInsertPadNodes:
    def test_no_intermediate_pads(self) -> None:
        path = [Point(0.5, 0.5), Point(2.5, 0.5)]
        pads: set[Point] = set()
        assert insert_pad_nodes(path, pads) == path

    def test_single_interior_pad_horizontal(self) -> None:
        path = [Point(0.5, 0.5), Point(4.5, 0.5)]
        pads = {Point(2.5, 0.5)}
        result = insert_pad_nodes(path, pads)
        assert result == [Point(0.5, 0.5), Point(2.5, 0.5), Point(4.5, 0.5)]

    def test_multiple_interior_pads_ordered(self) -> None:
        path = [Point(0.5, 0.5), Point(4.5, 0.5)]
        pads = {Point(3.5, 0.5), Point(1.5, 0.5), Point(2.5, 0.5)}
        result = insert_pad_nodes(path, pads)
        assert result == [
            Point(0.5, 0.5), Point(1.5, 0.5), Point(2.5, 0.5),
            Point(3.5, 0.5), Point(4.5, 0.5),
        ]

    def test_vertical_segment(self) -> None:
        path = [Point(0.5, 0.5), Point(0.5, 3.5)]
        pads = {Point(0.5, 1.5), Point(0.5, 2.5)}
        result = insert_pad_nodes(path, pads)
        assert result == [
            Point(0.5, 0.5), Point(0.5, 1.5), Point(0.5, 2.5), Point(0.5, 3.5),
        ]

    def test_diagonal_segment(self) -> None:
        path = [Point(0.5, 0.5), Point(3.5, 3.5)]
        pads = {Point(1.5, 1.5), Point(2.5, 2.5)}
        result = insert_pad_nodes(path, pads)
        assert result == [
            Point(0.5, 0.5), Point(1.5, 1.5), Point(2.5, 2.5), Point(3.5, 3.5),
        ]

    def test_pad_at_endpoint_not_duplicated(self) -> None:
        path = [Point(0.5, 0.5), Point(2.5, 0.5)]
        pads = {Point(0.5, 0.5), Point(2.5, 0.5)}
        result = insert_pad_nodes(path, pads)
        assert result == [Point(0.5, 0.5), Point(2.5, 0.5)]

    def test_off_axis_pad_not_inserted(self) -> None:
        path = [Point(0.5, 0.5), Point(4.5, 0.5)]
        pads = {Point(2.5, 1.5)}  # Off the line
        result = insert_pad_nodes(path, pads)
        assert result == [Point(0.5, 0.5), Point(4.5, 0.5)]

    def test_multi_segment_path(self) -> None:
        path = [Point(0.5, 0.5), Point(4.5, 0.5), Point(4.5, 3.5)]
        pads = {Point(2.5, 0.5), Point(4.5, 1.5), Point(4.5, 2.5)}
        result = insert_pad_nodes(path, pads)
        assert result == [
            Point(0.5, 0.5), Point(2.5, 0.5), Point(4.5, 0.5),
            Point(4.5, 1.5), Point(4.5, 2.5), Point(4.5, 3.5),
        ]

    def test_single_point_path_unchanged(self) -> None:
        path = [Point(0.5, 0.5)]
        result = insert_pad_nodes(path, {Point(0.5, 0.5)})
        assert result == [Point(0.5, 0.5)]

    def test_empty_path_unchanged(self) -> None:
        result = insert_pad_nodes([], set())
        assert result == []

    def test_pad_already_in_path_not_duplicated(self) -> None:
        path = [Point(0.5, 0.5), Point(2.5, 0.5), Point(4.5, 0.5)]
        pads = {Point(2.5, 0.5)}
        result = insert_pad_nodes(path, pads)
        assert result == path  # unchanged — pad already a node


class TestBoardPadsForSide:
    def test_perfboard_top_pads(self) -> None:
        board = _perfboard(3, 3)
        pads = board_pads_for_side(board, "top")
        assert len(pads) == 9
        assert Point(0.5, 0.5) in pads
        assert Point(2.5, 2.5) in pads

    def test_perfboard_bottom_pads(self) -> None:
        board = _perfboard(3, 3)
        pads = board_pads_for_side(board, "bottom")
        assert len(pads) == 9

    def test_plated_through_pads(self) -> None:
        board = _perfboard(2, 2)
        pads = plated_through_pads(board)
        assert len(pads) == 4
        assert Point(0.5, 0.5) in pads


def _wire(path: list[Point]) -> Wire:
    import uuid as _uuid
    return Wire(id=_uuid.uuid4(), path=path, insulated=True, side="top")


class TestWireHasSkewedSegment:
    def test_orthogonal_horizontal_clear(self) -> None:
        w = _wire([Point(0.5, 0.5), Point(3.5, 0.5)])
        assert wire_has_skewed_segment(w) is False

    def test_orthogonal_vertical_clear(self) -> None:
        w = _wire([Point(0.5, 0.5), Point(0.5, 4.5)])
        assert wire_has_skewed_segment(w) is False

    def test_diagonal_45_clear(self) -> None:
        w = _wire([Point(0.5, 0.5), Point(3.5, 3.5)])
        assert wire_has_skewed_segment(w) is False

    def test_diagonal_negative_slope_clear(self) -> None:
        w = _wire([Point(0.5, 3.5), Point(3.5, 0.5)])
        assert wire_has_skewed_segment(w) is False

    def test_multi_segment_orthogonal_diagonal_mix_clear(self) -> None:
        # L-shape with diagonal continuation: ortho → diag is fine
        w = _wire([Point(0.5, 0.5), Point(0.5, 2.5), Point(3.5, 5.5)])
        assert wire_has_skewed_segment(w) is False

    def test_near_45_within_rounding_tol_clear(self) -> None:
        # dx = 3.0, dy = 3.0 + 1e-9 → within tol, considered diagonal
        w = _wire([Point(0.5, 0.5), Point(3.5, 3.5 + 1e-9)])
        assert wire_has_skewed_segment(w) is False

    def test_skewed_2_to_3_detected(self) -> None:
        # Classic post-drag artifact: dx=2, dy=3
        w = _wire([Point(0.5, 0.5), Point(2.5, 3.5)])
        assert wire_has_skewed_segment(w) is True

    def test_skewed_fractional_detected(self) -> None:
        w = _wire([Point(0.5, 0.5), Point(2.5, 1.5)])
        assert wire_has_skewed_segment(w) is True

    def test_skewed_in_middle_of_path_detected(self) -> None:
        # First segment ortho, second segment skewed
        w = _wire([Point(0.5, 0.5), Point(0.5, 2.5), Point(2.5, 5.5)])
        assert wire_has_skewed_segment(w) is True

    def test_zero_length_segment_ignored(self) -> None:
        # Duplicate point — zero-length segment, neither skewed nor ortho-required
        w = _wire([Point(0.5, 0.5), Point(0.5, 0.5 + 1e-12), Point(3.5, 0.5)])
        assert wire_has_skewed_segment(w) is False


class TestFindSkewedWires:
    def test_filters_by_moved_ids(self) -> None:
        good = _wire([Point(0.5, 0.5), Point(3.5, 0.5)])
        bad = _wire([Point(0.5, 0.5), Point(2.5, 3.5)])
        # bad wire skewed but not in moved set
        ids = find_skewed_wires([good, bad], moved_wire_ids={good.id})
        assert ids == []

    def test_returns_only_skewed_within_moved_set(self) -> None:
        good = _wire([Point(0.5, 0.5), Point(3.5, 0.5)])
        bad = _wire([Point(0.5, 0.5), Point(2.5, 3.5)])
        ids = find_skewed_wires([good, bad], moved_wire_ids={good.id, bad.id})
        assert ids == [bad.id]

    def test_none_filter_inspects_all(self) -> None:
        good = _wire([Point(0.5, 0.5), Point(3.5, 0.5)])
        bad = _wire([Point(0.5, 0.5), Point(2.5, 3.5)])
        ids = find_skewed_wires([good, bad], moved_wire_ids=None)
        assert ids == [bad.id]


class TestCoalesceSameSideFragments:
    """Pure helper: fuse consecutive same-side fragments, keep via boundaries.

    Never invoked by domain constructors or commit paths — callers (an
    explicit user action, an algorithm step) decide when to apply it.
    """

    def test_empty_input_returns_empty(self) -> None:
        assert coalesce_same_side_fragments([]) == []

    def test_single_fragment_passes_through(self) -> None:
        f = Fragment(path=[Point(0.5, 0.5), Point(2.5, 0.5)], side="top")
        out = coalesce_same_side_fragments([f])
        assert len(out) == 1
        assert out[0].path == [Point(0.5, 0.5), Point(2.5, 0.5)]
        assert out[0].side == "top"

    def test_two_same_side_fragments_fuse(self) -> None:
        a = Fragment(path=[Point(0.5, 0.5), Point(2.5, 0.5)], side="top")
        b = Fragment(path=[Point(2.5, 0.5), Point(4.5, 0.5)], side="top")
        out = coalesce_same_side_fragments([a, b])
        assert len(out) == 1
        # Boundary vertex (2.5, 0.5) appears once, not twice.
        assert out[0].path == [
            Point(0.5, 0.5), Point(2.5, 0.5), Point(4.5, 0.5)
        ]
        assert out[0].side == "top"

    def test_three_same_side_fragments_fuse_into_one(self) -> None:
        a = Fragment(path=[Point(0.5, 0.5), Point(1.5, 0.5)], side="top")
        b = Fragment(path=[Point(1.5, 0.5), Point(2.5, 0.5)], side="top")
        c = Fragment(path=[Point(2.5, 0.5), Point(3.5, 0.5)], side="top")
        out = coalesce_same_side_fragments([a, b, c])
        assert len(out) == 1
        assert out[0].path == [
            Point(0.5, 0.5), Point(1.5, 0.5),
            Point(2.5, 0.5), Point(3.5, 0.5),
        ]

    def test_via_boundary_preserved(self) -> None:
        # top → bottom: real via, must not collapse.
        a = Fragment(path=[Point(0.5, 0.5), Point(2.5, 0.5)], side="top")
        b = Fragment(path=[Point(2.5, 0.5), Point(4.5, 0.5)], side="bottom")
        out = coalesce_same_side_fragments([a, b])
        assert len(out) == 2
        assert out[0].side == "top"
        assert out[1].side == "bottom"
        # Boundary point present on both sides of the via.
        assert out[0].path[-1] == Point(2.5, 0.5)
        assert out[1].path[0] == Point(2.5, 0.5)

    def test_same_side_around_via_fuse_around_unchanged_boundary(self) -> None:
        # top → bottom → top: only the via (top→bottom and bottom→top) survives,
        # but two top fragments on either side of a single bottom fragment
        # must NOT fuse — there is a real bottom fragment between them.
        a = Fragment(path=[Point(0.5, 0.5), Point(1.5, 0.5)], side="top")
        b = Fragment(path=[Point(1.5, 0.5), Point(2.5, 0.5)], side="bottom")
        c = Fragment(path=[Point(2.5, 0.5), Point(3.5, 0.5)], side="top")
        out = coalesce_same_side_fragments([a, b, c])
        assert len(out) == 3
        assert [f.side for f in out] == ["top", "bottom", "top"]

    def test_mixed_run_fuses_only_consecutive_same_side(self) -> None:
        # top, top, bottom, top, top — expect top-fused, bottom, top-fused.
        f1 = Fragment(path=[Point(0.5, 0.5), Point(1.5, 0.5)], side="top")
        f2 = Fragment(path=[Point(1.5, 0.5), Point(2.5, 0.5)], side="top")
        f3 = Fragment(path=[Point(2.5, 0.5), Point(3.5, 0.5)], side="bottom")
        f4 = Fragment(path=[Point(3.5, 0.5), Point(4.5, 0.5)], side="top")
        f5 = Fragment(path=[Point(4.5, 0.5), Point(5.5, 0.5)], side="top")
        out = coalesce_same_side_fragments([f1, f2, f3, f4, f5])
        assert [f.side for f in out] == ["top", "bottom", "top"]
        assert [len(f.path) for f in out] == [3, 2, 3]

    def test_does_not_mutate_input(self) -> None:
        a = Fragment(path=[Point(0.5, 0.5), Point(2.5, 0.5)], side="top")
        b = Fragment(path=[Point(2.5, 0.5), Point(4.5, 0.5)], side="top")
        original_a_path = list(a.path)
        original_b_path = list(b.path)
        _ = coalesce_same_side_fragments([a, b])
        assert a.path == original_a_path
        assert b.path == original_b_path

    def test_tuple_input_accepted(self) -> None:
        a = Fragment(path=[Point(0.5, 0.5), Point(2.5, 0.5)], side="top")
        b = Fragment(path=[Point(2.5, 0.5), Point(4.5, 0.5)], side="top")
        out = coalesce_same_side_fragments((a, b))
        assert len(out) == 1
        assert out[0].path == [
            Point(0.5, 0.5), Point(2.5, 0.5), Point(4.5, 0.5)
        ]


class TestMergeEndpointWires:
    """Pure helper: fuse pairs of wires meeting end-to-end on off-pin pads
    with matching ``insulated``.  Used by load_project / save_project to
    keep on-disk format canonical."""

    @staticmethod
    def _make_layout_with_two_meeting_wires(
        *, insulated_a: bool = True, insulated_b: bool = True,
    ):
        """Build a Layout with two single-fragment wires meeting at (2.5, 0.5).

        Each gets its own JointKnot at the meeting pad — exactly the
        shape the autoroute / legacy load path produces before this
        helper runs.
        """
        from protoboard.domain.layout.knot import JointKnot
        from protoboard.domain.layout.layout import Layout
        from protoboard.domain.layout.wire import KnotRef, Wire
        layout = Layout(placements={}, wires=[])
        # Outer endpoints (left & right) each get a degree-1 joint.
        import uuid as _uuid
        ja = JointKnot(id=_uuid.uuid4(), position=Point(0.5, 0.5))
        jb_w1 = JointKnot(id=_uuid.uuid4(), position=Point(2.5, 0.5))
        jb_w2 = JointKnot(id=_uuid.uuid4(), position=Point(2.5, 0.5))
        jc = JointKnot(id=_uuid.uuid4(), position=Point(4.5, 0.5))
        for k in (ja, jb_w1, jb_w2, jc):
            layout.knots[k.id] = k
        w1 = Wire(
            id=_uuid.uuid4(),
            path=[Point(0.5, 0.5), Point(2.5, 0.5)],
            insulated=insulated_a, side="top",
            a=KnotRef(id=ja.id), b=KnotRef(id=jb_w1.id),
        )
        w2 = Wire(
            id=_uuid.uuid4(),
            path=[Point(2.5, 0.5), Point(4.5, 0.5)],
            insulated=insulated_b, side="top",
            a=KnotRef(id=jb_w2.id), b=KnotRef(id=jc.id),
        )
        layout.add_wire(w1)
        layout.add_wire(w2)
        return layout, _perfboard(5, 5)

    def test_matching_insulated_fuses(self) -> None:
        from protoboard.domain.layout.wire_utils import merge_endpoint_wires
        layout, board = self._make_layout_with_two_meeting_wires()
        assert len(layout.wires) == 2
        fused = merge_endpoint_wires(layout, board)
        assert fused == 1
        assert len(layout.wires) == 1

    def test_mismatched_insulated_keeps_separate(self) -> None:
        from protoboard.domain.layout.wire_utils import merge_endpoint_wires
        layout, board = self._make_layout_with_two_meeting_wires(
            insulated_a=True, insulated_b=False,
        )
        fused = merge_endpoint_wires(layout, board)
        assert fused == 0
        assert len(layout.wires) == 2

    def test_idempotent_after_first_pass(self) -> None:
        from protoboard.domain.layout.wire_utils import merge_endpoint_wires
        layout, board = self._make_layout_with_two_meeting_wires()
        merge_endpoint_wires(layout, board)
        # Second invocation must do nothing — already canonical.
        fused2 = merge_endpoint_wires(layout, board)
        assert fused2 == 0

    def test_colocated_joints_coalesce(self) -> None:
        from protoboard.domain.layout.wire_utils import merge_colocated_joints
        layout, _board = self._make_layout_with_two_meeting_wires()
        # Before: two separate joints sitting at (2.5, 0.5).
        from protoboard.domain.layout.knot import JointKnot
        joints_at_pad = [
            k for k in layout.knots.values()
            if isinstance(k, JointKnot)
            and abs(k.position.x - 2.5) < 1e-9
            and abs(k.position.y - 0.5) < 1e-9
        ]
        assert len(joints_at_pad) == 2
        dropped = merge_colocated_joints(layout)
        assert dropped == 1
        joints_at_pad_after = [
            k for k in layout.knots.values()
            if isinstance(k, JointKnot)
            and abs(k.position.x - 2.5) < 1e-9
            and abs(k.position.y - 0.5) < 1e-9
        ]
        assert len(joints_at_pad_after) == 1
        # The surviving joint has both wires as back-refs.
        assert len(joints_at_pad_after[0].wire_ids) == 2


class TestSplitWiresAtEndpointCollisions:
    """T-junction normalisation: every W1 endpoint that lands on an
    interior vertex of W2 becomes a degree-3 hub (W1, W2a, W2b).

    The atomic ``make_t_junction`` path avoids the split→merge livelock
    a two-step pipeline produces."""

    @staticmethod
    def _make_layout_with_t_junction():
        """W2 runs horizontally past (2.5, 0.5); W1 ends at that pad."""
        from protoboard.domain.layout.knot import JointKnot
        from protoboard.domain.layout.layout import Layout
        from protoboard.domain.layout.wire import KnotRef, Wire
        import uuid as _uuid
        layout = Layout(placements={}, wires=[])
        # Outer joints for both wires.
        j_w2_a = JointKnot(id=_uuid.uuid4(), position=Point(0.5, 0.5))
        j_w2_b = JointKnot(id=_uuid.uuid4(), position=Point(4.5, 0.5))
        j_w1_a = JointKnot(id=_uuid.uuid4(), position=Point(2.5, 2.5))
        j_w1_b = JointKnot(id=_uuid.uuid4(), position=Point(2.5, 0.5))
        for k in (j_w2_a, j_w2_b, j_w1_a, j_w1_b):
            layout.knots[k.id] = k
        w2 = Wire(
            id=_uuid.uuid4(),
            path=[Point(0.5, 0.5), Point(2.5, 0.5), Point(4.5, 0.5)],
            insulated=True, side="top",
            a=KnotRef(id=j_w2_a.id), b=KnotRef(id=j_w2_b.id),
        )
        w1 = Wire(
            id=_uuid.uuid4(),
            path=[Point(2.5, 2.5), Point(2.5, 0.5)],
            insulated=True, side="top",
            a=KnotRef(id=j_w1_a.id), b=KnotRef(id=j_w1_b.id),
        )
        layout.add_wire(w2)
        layout.add_wire(w1)
        return layout, w1, w2

    def test_splits_w2_and_anchors_w1(self) -> None:
        from protoboard.domain.layout.knot import JointKnot
        from protoboard.domain.layout.wire_utils import (
            split_wires_at_endpoint_collisions,
        )
        layout, w1, w2 = self._make_layout_with_t_junction()
        fixes = split_wires_at_endpoint_collisions(layout)
        assert fixes == 1
        # W2 became two wires; W1 stayed.  Total: 3.
        assert len(layout.wires) == 3
        # Find the hub at (2.5, 0.5).
        hub_candidates = [
            k for k in layout.knots.values()
            if isinstance(k, JointKnot)
            and abs(k.position.x - 2.5) < 1e-9
            and abs(k.position.y - 0.5) < 1e-9
        ]
        assert len(hub_candidates) == 1
        hub = hub_candidates[0]
        # Three wires meet at the hub.
        assert len(hub.wire_ids) == 3

    def test_idempotent(self) -> None:
        from protoboard.domain.layout.wire_utils import (
            split_wires_at_endpoint_collisions,
        )
        layout, _w1, _w2 = self._make_layout_with_t_junction()
        split_wires_at_endpoint_collisions(layout)
        # Second pass on a now-canonical layout — no further fixes.
        assert split_wires_at_endpoint_collisions(layout) == 0

    def test_merge_does_not_undo_split(self) -> None:
        """Regression: the two-step (split, merge) pipeline produced an
        infinite livelock because merge_endpoint_wires saw the new hub
        as a degree-2 joint and collapsed it.  ``make_t_junction``
        builds the hub at degree-3 in one atomic step."""
        from protoboard.domain.layout.wire_utils import (
            merge_endpoint_wires,
            split_wires_at_endpoint_collisions,
        )
        board = _perfboard(5, 5)
        layout, _w1, _w2 = self._make_layout_with_t_junction()
        split_wires_at_endpoint_collisions(layout)
        # Merge pass — must NOT recombine the split halves.
        merge_endpoint_wires(layout, board)
        assert len(layout.wires) == 3  # still T-junction
        # And a second split pass finds nothing.
        assert split_wires_at_endpoint_collisions(layout) == 0


class TestTJunctionNetGate:
    """``split_wires_at_endpoint_collisions`` and ``AddWireCommand`` must
    refuse to splice a T-junction between wires of different schematic
    nets — collapsing them into a shared hub would short the two nets.

    Regression: the postprocess pass in ``save_project`` / ``load_project``
    treated every endpoint-on-interior-vertex collision as a T-junction
    regardless of intent, which on ``reference_big_project`` chained 22
    nets into one 150-pin electric cluster.
    """

    @staticmethod
    def _two_resistor_setup():
        """Build (board, layout, schematic, components, instances) with
        two single-pin resistors R1 and R2 each on their own net, plus
        a third resistor R3 on a third net far away — the geometry is
        irrelevant; only the net membership of the wire endpoints is
        what the gate inspects."""
        from protoboard.domain.components.definition import ComponentDefinition
        from protoboard.domain.components.footprints import SIL
        from protoboard.domain.components.instance import ComponentInstance
        from protoboard.domain.layout.layout import Layout
        from protoboard.domain.layout.placement import ComponentPlacement
        from protoboard.domain.schematic.net import Net, PinRef
        from protoboard.domain.schematic.schematic import Schematic
        import uuid as _uuid

        board = _perfboard(3, 10)
        defn = ComponentDefinition(name="R", footprint=SIL(2), pin_names={1: "A", 2: "B"})

        r1 = ComponentInstance.new(definition_ref="R", refdes="R1", value="10k")
        r2 = ComponentInstance.new(definition_ref="R", refdes="R2", value="10k")
        # SIL(2) rot=90: pin1=(x, 0.5), pin2=(x+1, 0.5).
        placements = {
            r1.id: ComponentPlacement(
                component_id=r1.id, position=Point(0.5, 0.5),
                rotation=90, side="top",
            ),
            r2.id: ComponentPlacement(
                component_id=r2.id, position=Point(0.5, 2.5),
                rotation=90, side="top",
            ),
        }
        net_a = Net(
            id=_uuid.uuid4(), name="NET_A",
            pins={PinRef(component_id=r1.id, pin_number=1)},
        )
        net_b = Net(
            id=_uuid.uuid4(), name="NET_B",
            pins={PinRef(component_id=r2.id, pin_number=1)},
        )
        schematic = Schematic(nets={net_a.id: net_a, net_b.id: net_b})
        layout = Layout(placements=placements, wires=[])
        components = {defn.name: defn}
        instances = {r1.id: r1, r2.id: r2}
        return board, layout, schematic, components, instances, r1, r2, net_a, net_b

    @staticmethod
    def _add_two_crossing_wires(layout, p_a, p_b):
        """W_through: horizontal NET_A wire at y=0.5 passing through
        (interior) point ``p_b``.  W_incoming: vertical NET_B wire whose
        endpoint b lands on ``p_b``.  Both insulated.
        """
        from protoboard.domain.layout.knot import JointKnot
        from protoboard.domain.layout.wire import KnotRef, Wire
        import uuid as _uuid

        # W_through: NET_A endpoints at R1/pin1 (0.5, 0.5) and far right
        # (5.5, 0.5), passing through interior vertex p_b on its path.
        j_th_a = JointKnot(id=_uuid.uuid4(), position=Point(0.5, 0.5))
        j_th_b = JointKnot(id=_uuid.uuid4(), position=Point(5.5, 0.5))
        layout.knots[j_th_a.id] = j_th_a
        layout.knots[j_th_b.id] = j_th_b
        w_through = Wire(
            id=_uuid.uuid4(),
            path=[Point(0.5, 0.5), p_b, Point(5.5, 0.5)],
            insulated=True, side="top",
            a=KnotRef(id=j_th_a.id), b=KnotRef(id=j_th_b.id),
        )
        # W_incoming: NET_B endpoints — pin2 of R2 at (0.5, 2.5) and the
        # collision point p_b.
        j_in_a = JointKnot(id=_uuid.uuid4(), position=p_a)
        j_in_b = JointKnot(id=_uuid.uuid4(), position=p_b)
        layout.knots[j_in_a.id] = j_in_a
        layout.knots[j_in_b.id] = j_in_b
        w_incoming = Wire(
            id=_uuid.uuid4(),
            path=[p_a, p_b],
            insulated=True, side="top",
            a=KnotRef(id=j_in_a.id), b=KnotRef(id=j_in_b.id),
        )
        layout.add_wire(w_through)
        layout.add_wire(w_incoming)
        return w_through, w_incoming

    def test_split_skipped_when_nets_differ(self) -> None:
        """Two insulated wires of different nets crossing at an interior
        vertex must stay as two separate wires; no T-junction is built."""
        from protoboard.domain.layout.wire_utils import (
            split_wires_at_endpoint_collisions,
        )
        board, layout, schematic, components, instances, r1, r2, net_a, net_b = (
            self._two_resistor_setup()
        )
        # W_through anchors at R1/pin1 → NET_A.
        # W_incoming anchors at R2/pin1 (a) and at (2.5, 0.5) (b) — the
        # collision point sits on W_through's interior vertex.
        self._add_two_crossing_wires(
            layout, p_a=Point(0.5, 2.5), p_b=Point(2.5, 0.5),
        )
        fixes = split_wires_at_endpoint_collisions(
            layout,
            schematic=schematic,
            instances=instances,
            components=components,
        )
        assert fixes == 0
        assert len(layout.wires) == 2  # no split happened
        # NET_B did not steal NET_A's pin and vice versa.
        assert {p.component_id for p in net_a.pins} == {r1.id}
        assert {p.component_id for p in net_b.pins} == {r2.id}

    def test_split_proceeds_when_nets_match(self) -> None:
        """Same setup but both wires on NET_A — the T-junction is legal."""
        from protoboard.domain.layout.wire_utils import (
            split_wires_at_endpoint_collisions,
        )
        from protoboard.domain.schematic.net import PinRef
        board, layout, schematic, components, instances, r1, r2, net_a, net_b = (
            self._two_resistor_setup()
        )
        # Move R2's pin into NET_A so both wires resolve to the same net.
        net_a.pins.add(PinRef(component_id=r2.id, pin_number=1))
        net_b.pins.clear()
        self._add_two_crossing_wires(
            layout, p_a=Point(0.5, 2.5), p_b=Point(2.5, 0.5),
        )
        fixes = split_wires_at_endpoint_collisions(
            layout,
            schematic=schematic,
            instances=instances,
            components=components,
        )
        assert fixes == 1
        assert len(layout.wires) == 3  # w_through got split

    def test_legacy_callers_without_schematic_keep_old_behaviour(self) -> None:
        """When no schematic/instances/components are passed the gate is
        permissive — pure geometry tests still produce a T-junction."""
        from protoboard.domain.layout.wire_utils import (
            split_wires_at_endpoint_collisions,
        )
        board, layout, _sch, _comps, _insts, _r1, _r2, _na, _nb = (
            self._two_resistor_setup()
        )
        self._add_two_crossing_wires(
            layout, p_a=Point(0.5, 2.5), p_b=Point(2.5, 0.5),
        )
        # No schematic context → old geometry-only behaviour.
        fixes = split_wires_at_endpoint_collisions(layout)
        assert fixes == 1
        assert len(layout.wires) == 3

    def test_can_form_t_junction_rejects_incompatible_sides(self) -> None:
        """A wire on ``top`` and a wire on ``bottom`` cannot share one
        solder joint — verdict is INCOMPATIBLE_GEOMETRY regardless of
        net membership."""
        from protoboard.domain.layout.wire import KnotRef, Wire
        from protoboard.domain.layout.knot import JointKnot
        from protoboard.domain.layout.wire_utils import (
            TJunctionVerdict,
            can_form_t_junction,
        )
        import uuid as _uuid
        j1 = JointKnot(id=_uuid.uuid4(), position=Point(0.5, 0.5))
        j2 = JointKnot(id=_uuid.uuid4(), position=Point(2.5, 0.5))
        j3 = JointKnot(id=_uuid.uuid4(), position=Point(1.5, 0.5))
        j4 = JointKnot(id=_uuid.uuid4(), position=Point(1.5, 2.5))
        w_top = Wire(
            id=_uuid.uuid4(),
            path=[Point(0.5, 0.5), Point(1.5, 0.5), Point(2.5, 0.5)],
            insulated=True, side="top",
            a=KnotRef(id=j1.id), b=KnotRef(id=j2.id),
        )
        w_bottom = Wire(
            id=_uuid.uuid4(),
            path=[Point(1.5, 2.5), Point(1.5, 0.5)],
            insulated=True, side="bottom",
            a=KnotRef(id=j4.id), b=KnotRef(id=j3.id),
        )
        verdict, orphan, net_id = can_form_t_junction(
            w_top, w_bottom, "b",
        )
        assert verdict is TJunctionVerdict.INCOMPATIBLE_GEOMETRY
        assert orphan is None and net_id is None
