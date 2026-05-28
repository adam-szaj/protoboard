# SPDX-License-Identifier: MIT
"""AddWireCommand endpoint-merge behaviour.

When a new wire's endpoint lands on an off-pin pad already occupied by
exactly one other wire and they share ``insulated``, ``AddWireCommand``
fuses them into a single Wire so the underlying model matches what the
user sees on screen (one continuous solderable artifact).
"""

from __future__ import annotations

import uuid

from protoboard.domain.board.board import Board
from protoboard.domain.board.boxes import BoardGrid, PadAtom, SizePolicy
from protoboard.domain.board.pads import PadLayers
from protoboard.domain.commands.component_cmds import (
    AddWireCommand,
    RemoveWireCommand,
)
from protoboard.domain.components.definition import ComponentDefinition
from protoboard.domain.components.instance import ComponentInstance
from protoboard.domain.layout.knot import JointKnot, PinKnot
from protoboard.domain.layout.layout import Layout
from protoboard.domain.layout.wire import Wire
from protoboard.geometry import Point


def _perfboard(rows: int = 5, cols: int = 5) -> Board:
    return Board(root=BoardGrid(
        width_policy=SizePolicy.HUG,
        height_policy=SizePolicy.HUG,
        cell=PadAtom(
            width_policy=SizePolicy.HUG,
            height_policy=SizePolicy.HUG,
            layers=PadLayers(top=True, bottom=True, plated_through=True),
        ),
        rows=rows, cols=cols, row_gap=0.0, col_gap=0.0,
    ))


class _State:
    """Minimal ComponentState stub for AddWireCommand."""

    def __init__(self) -> None:
        self.board = _perfboard()
        self.layout = Layout(placements={}, wires=[])
        self.instances: dict[uuid.UUID, ComponentInstance] = {}
        self.components: dict[str, ComponentDefinition] = {}


def _add(state: _State, path: list[tuple[float, float]],
         *, insulated: bool = True, side: str = "top") -> AddWireCommand:
    wire = Wire(
        id=uuid.uuid4(),
        path=[Point(x, y) for x, y in path],
        insulated=insulated,
        side=side,  # type: ignore[arg-type]
    )
    cmd = AddWireCommand(wire=wire)
    cmd.apply(state)
    return cmd


# ---------------------------------------------------------------------------
# Same-side, same-insulated → fuse
# ---------------------------------------------------------------------------


class TestEndpointMergeSameAttrs:
    def test_two_same_side_wires_meeting_fuse(self) -> None:
        state = _State()
        _add(state, [(0.5, 0.5), (2.5, 0.5)])
        _add(state, [(2.5, 0.5), (4.5, 0.5)])
        # Both wires share endpoint at (2.5, 0.5) which is *not* a pin —
        # they fuse into one Wire.  Insulated wires also get pad-vertex
        # enrichment, so the fused path includes every intermediate pad.
        assert len(state.layout.wires) == 1
        w = list(state.layout.wires)[0]
        path_pts = [(p.x, p.y) for p in w.path]
        # Endpoints survive (in either order, since the collapse may flip
        # orientation); pad vertices appear in between.
        assert {path_pts[0], path_pts[-1]} == {(0.5, 0.5), (4.5, 0.5)}
        assert (2.5, 0.5) in path_pts
        assert len(w.fragments) == 1  # same-side → single fragment

    def test_joint_at_fusion_pad_is_consumed(self) -> None:
        state = _State()
        _add(state, [(0.5, 0.5), (2.5, 0.5)])
        _add(state, [(2.5, 0.5), (4.5, 0.5)])
        # The joint at (2.5, 0.5) should no longer exist — the two wires
        # collapsed into one and the central joint was consumed.
        joint_positions = [
            k.position for k in state.layout.knots.values()
            if isinstance(k, JointKnot)
        ]
        assert all(
            not (abs(p.x - 2.5) < 1e-9 and abs(p.y - 0.5) < 1e-9)
            for p in joint_positions
        )

    def test_three_wires_in_a_row_fuse(self) -> None:
        state = _State()
        _add(state, [(0.5, 0.5), (1.5, 0.5)])
        _add(state, [(1.5, 0.5), (2.5, 0.5)])
        _add(state, [(2.5, 0.5), (3.5, 0.5)])
        # Three wires meeting end-to-end at (1.5,…) and (2.5,…) — both
        # joints are degree-2 in sequence; both should collapse.
        assert len(state.layout.wires) == 1
        w = list(state.layout.wires)[0]
        path_pts = [(p.x, p.y) for p in w.path]
        assert {path_pts[0], path_pts[-1]} == {(0.5, 0.5), (3.5, 0.5)}
        assert (1.5, 0.5) in path_pts
        assert (2.5, 0.5) in path_pts


# ---------------------------------------------------------------------------
# Cross-side via plated-through → fuse into multi-fragment Wire
# ---------------------------------------------------------------------------


class TestEndpointMergeCrossSide:
    def test_top_bottom_via_pad_fuses_into_two_fragments(self) -> None:
        state = _State()  # perfboard pads are plated-through
        _add(state, [(0.5, 0.5), (2.5, 0.5)], side="top")
        _add(state, [(2.5, 0.5), (4.5, 0.5)], side="bottom")
        # Plated-through pad at (2.5, 0.5) lets the two sides bridge
        # electrically — the wires fuse, but the resulting Wire has TWO
        # fragments (one per side) so the via boundary survives.
        assert len(state.layout.wires) == 1
        w = list(state.layout.wires)[0]
        assert len(w.fragments) == 2
        assert {f.side for f in w.fragments} == {"top", "bottom"}


# ---------------------------------------------------------------------------
# Mismatched attrs → no fuse
# ---------------------------------------------------------------------------


class TestEndpointMergeMismatchedAttrs:
    def test_mismatched_insulated_does_not_fuse(self) -> None:
        state = _State()
        _add(state, [(0.5, 0.5), (2.5, 0.5)], insulated=True)
        _add(state, [(2.5, 0.5), (4.5, 0.5)], insulated=False)
        # bare ≠ insulated — each wire is a physically distinct artifact
        # (bare bus + insulated jumper) so they stay separate, joined by
        # a shared JointKnot.
        assert len(state.layout.wires) == 2

    def test_shared_joint_persists_when_no_fuse(self) -> None:
        state = _State()
        _add(state, [(0.5, 0.5), (2.5, 0.5)], insulated=True)
        _add(state, [(2.5, 0.5), (4.5, 0.5)], insulated=False)
        joint_at_pad = [
            k for k in state.layout.knots.values()
            if isinstance(k, JointKnot)
            and abs(k.position.x - 2.5) < 1e-9
            and abs(k.position.y - 0.5) < 1e-9
        ]
        assert len(joint_at_pad) == 1
        # The single joint references both wires.
        assert len(joint_at_pad[0].wire_ids) == 2


# ---------------------------------------------------------------------------
# Undo / redo
# ---------------------------------------------------------------------------


class TestEndpointMergeUndo:
    def test_undo_after_fusion_restores_pre_cmd_state(self) -> None:
        state = _State()
        cmd1 = _add(state, [(0.5, 0.5), (2.5, 0.5)])
        cmd2 = _add(state, [(2.5, 0.5), (4.5, 0.5)])
        assert len(state.layout.wires) == 1  # fused
        cmd2.revert(state)
        # Pre-cmd2 state: only the first wire (no fuse, no second wire).
        assert len(state.layout.wires) == 1
        assert cmd1.wire.id in state.layout.wires

    def test_redo_re_fuses_after_undo(self) -> None:
        state = _State()
        _add(state, [(0.5, 0.5), (2.5, 0.5)])
        cmd2 = _add(state, [(2.5, 0.5), (4.5, 0.5)])
        cmd2.revert(state)
        cmd2.apply(state)
        assert len(state.layout.wires) == 1

    def test_no_fusion_no_collapse_state(self) -> None:
        state = _State()
        _add(state, [(0.5, 0.5), (2.5, 0.5)], insulated=True)
        cmd2 = _add(
            state, [(2.5, 0.5), (4.5, 0.5)], insulated=False,
        )
        # No fusion happened → _collapses is empty → revert simply
        # removes the second wire.
        assert cmd2._collapses == []
        cmd2.revert(state)
        assert len(state.layout.wires) == 1


# ---------------------------------------------------------------------------
# Pin endpoints stay separate
# ---------------------------------------------------------------------------


def _state_with_pin_at(pos: Point) -> _State:
    """Helper that places a 1-pin SIL component at ``pos`` so the pad
    becomes a real PinKnot site."""
    from protoboard.domain.components.footprints import SIL
    from protoboard.domain.layout.placement import ComponentPlacement
    state = _State()
    defn = ComponentDefinition(
        name="STUB", footprint=SIL(1), pin_names={1: "P1"},
    )
    state.components["STUB"] = defn
    comp_id = uuid.uuid4()
    state.instances[comp_id] = ComponentInstance(
        id=comp_id, definition_ref="STUB", refdes="X1", value="",
    )
    state.layout.placements[comp_id] = ComponentPlacement(
        component_id=comp_id,
        position=pos,
        rotation=0,
        side="top",
    )
    return state


class TestEndpointMergeOnPin:
    def test_two_wires_at_pin_stay_separate(self) -> None:
        state = _state_with_pin_at(Point(2.5, 0.5))
        _add(state, [(0.5, 0.5), (2.5, 0.5)])
        _add(state, [(2.5, 0.5), (4.5, 0.5)])
        # Both wires end at the same pin — pin is a physical hub, no fuse.
        assert len(state.layout.wires) == 2
        # PinKnot exists at the pin's position.
        pin_knots = [
            k for k in state.layout.knots.values()
            if isinstance(k, PinKnot)
        ]
        assert len(pin_knots) == 1
