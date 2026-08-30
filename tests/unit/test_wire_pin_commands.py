# SPDX-License-Identifier: MIT
"""Tests for ConvertEndpointToWirePinCommand / ConvertWirePinToEndpointCommand.

The half-orphan endpoint of a wire can be promoted to a deliberate
terminal (wire-pin) and back.  See docs/archive/wire-pin-implementation.md §3.6.
"""

from __future__ import annotations

import uuid
from dataclasses import dataclass, field

import pytest

from protoboard.domain.board.board import Board
from protoboard.domain.board.boxes import BoardGrid, PadAtom, SizePolicy
from protoboard.domain.board.pads import PadLayers
from protoboard.domain.commands.component_cmds import (
    ConvertEndpointToWirePinCommand,
    ConvertWirePinToEndpointCommand,
)
from protoboard.domain.components.definition import ComponentDefinition
from protoboard.domain.components.instance import ComponentInstance
from protoboard.domain.layout.knot import JointKnot, PinKnot, WirePinKnot
from protoboard.domain.layout.layout import Layout
from protoboard.domain.layout.wire import KnotRef, Wire
from protoboard.domain.schematic.net import PinRef
from protoboard.geometry import Point


@dataclass
class _State:
    board: Board
    layout: Layout = field(default_factory=Layout)
    instances: dict[uuid.UUID, ComponentInstance] = field(default_factory=dict)
    components: dict[str, ComponentDefinition] = field(default_factory=dict)


def _board() -> Board:
    return Board(
        root=BoardGrid(
            width_policy=SizePolicy.HUG,
            height_policy=SizePolicy.HUG,
            cell=PadAtom(
                width_policy=SizePolicy.HUG,
                height_policy=SizePolicy.HUG,
                layers=PadLayers(top=True, bottom=True, plated_through=True),
            ),
            rows=1,
            cols=2,
            row_gap=0.0,
            col_gap=0.0,
        )
    )


def _make_half_orphan_wire(
    state: _State,
    pos_a: Point,
    pos_b: Point,
) -> tuple[Wire, uuid.UUID, uuid.UUID]:
    """Create a wire with two free JointKnot endpoints (full-orphan).

    Returns (wire, knot_a_id, knot_b_id).  For half-orphan tests, the
    caller can replace one of the joints with a PinKnot afterwards if
    needed — wire-pin conversion only cares about the chosen endpoint.
    """
    ka = state.layout.create_joint_knot(pos_a)
    kb = state.layout.create_joint_knot(pos_b)
    wire = Wire(
        id=uuid.uuid4(),
        path=[pos_a, pos_b],
        side="top",
        a=KnotRef(id=ka.id),
        b=KnotRef(id=kb.id),
    )
    state.layout.add_wire(wire)
    return wire, ka.id, kb.id


# ---------------------------------------------------------------------------
# Apply
# ---------------------------------------------------------------------------


class TestConvertEndpointToWirePinApply:
    def test_replaces_joint_with_wire_pin_preserving_id(self) -> None:
        state = _State(board=_board())
        wire, ka_id, _kb_id = _make_half_orphan_wire(
            state,
            Point(0.5, 0.5),
            Point(1.5, 0.5),
        )
        net_id = uuid.uuid4()
        cmd = ConvertEndpointToWirePinCommand(
            wire_id=wire.id,
            endpoint="a",
            net_id=net_id,
        )
        cmd.apply(state)
        knot = state.layout.knots[ka_id]
        assert isinstance(knot, WirePinKnot)
        assert knot.id == ka_id
        assert knot.position == Point(0.5, 0.5)
        assert knot.net_id == net_id
        assert knot.wire_ids == frozenset({wire.id})

    def test_wire_knot_ref_unchanged(self) -> None:
        # The whole reason for id-preservation: wire.a/b stay valid
        # after the swap.  No fragment edits, no path mutation.
        state = _State(board=_board())
        wire, ka_id, kb_id = _make_half_orphan_wire(
            state,
            Point(0.5, 0.5),
            Point(1.5, 0.5),
        )
        original_path = list(wire.path)
        ConvertEndpointToWirePinCommand(
            wire_id=wire.id,
            endpoint="b",
            net_id=None,
        ).apply(state)
        assert wire.a is not None and wire.a.id == ka_id
        assert wire.b is not None and wire.b.id == kb_id
        assert wire.path == original_path

    def test_rejects_unknown_wire(self) -> None:
        state = _State(board=_board())
        cmd = ConvertEndpointToWirePinCommand(
            wire_id=uuid.uuid4(),
            endpoint="a",
            net_id=None,
        )
        with pytest.raises(KeyError):
            cmd.apply(state)

    def test_rejects_bad_endpoint_name(self) -> None:
        state = _State(board=_board())
        wire, _, _ = _make_half_orphan_wire(
            state,
            Point(0.5, 0.5),
            Point(1.5, 0.5),
        )
        cmd = ConvertEndpointToWirePinCommand(
            wire_id=wire.id,
            endpoint="z",
            net_id=None,
        )
        with pytest.raises(ValueError, match="endpoint must be"):
            cmd.apply(state)

    def test_rejects_pinknot_endpoint(self) -> None:
        # Endpoint anchored to a component pin is not a half-orphan —
        # the conversion must reject it.
        state = _State(board=_board())
        wire, ka_id, _ = _make_half_orphan_wire(
            state,
            Point(0.5, 0.5),
            Point(1.5, 0.5),
        )
        # Replace the 'a' joint with a PinKnot at the same id.
        pin_ref = PinRef(component_id=uuid.uuid4(), pin_number=1)
        state.layout.knots[ka_id] = PinKnot(
            id=ka_id,
            pin=pin_ref,
            wire_ids=state.layout.knots[ka_id].wire_ids,
        )
        cmd = ConvertEndpointToWirePinCommand(
            wire_id=wire.id,
            endpoint="a",
            net_id=None,
        )
        with pytest.raises(ValueError, match="expected JointKnot"):
            cmd.apply(state)

    def test_rejects_t_junction_hub(self) -> None:
        # JointKnot shared with another wire (t-junction hub) must not
        # be promoted to wire-pin — the wire-pin invariant is
        # "exactly one wire owns the knot".
        state = _State(board=_board())
        ka = state.layout.create_joint_knot(Point(0.5, 0.5))
        kb = state.layout.create_joint_knot(Point(1.5, 0.5))
        kc = state.layout.create_joint_knot(Point(0.5, 0.5))  # shared with w1
        w1 = Wire(
            id=uuid.uuid4(),
            path=[Point(0.5, 0.5), Point(1.5, 0.5)],
            side="top",
            a=KnotRef(id=ka.id),
            b=KnotRef(id=kb.id),
        )
        w2 = Wire(
            id=uuid.uuid4(),
            path=[Point(0.5, 0.5), Point(0.5, 1.5)],
            side="top",
            a=KnotRef(id=ka.id),
            b=KnotRef(id=kc.id),
        )
        state.layout.add_wire(w1)
        state.layout.add_wire(w2)
        # ka now owns both wires — promoting it to wire-pin must fail.
        cmd = ConvertEndpointToWirePinCommand(
            wire_id=w1.id,
            endpoint="a",
            net_id=None,
        )
        with pytest.raises(ValueError, match="sole-owner"):
            cmd.apply(state)


# ---------------------------------------------------------------------------
# Revert (round-trip)
# ---------------------------------------------------------------------------


class TestConvertEndpointToWirePinRevert:
    def test_apply_then_revert_restores_joint(self) -> None:
        state = _State(board=_board())
        wire, ka_id, _ = _make_half_orphan_wire(
            state,
            Point(0.5, 0.5),
            Point(1.5, 0.5),
        )
        cmd = ConvertEndpointToWirePinCommand(
            wire_id=wire.id,
            endpoint="a",
            net_id=uuid.uuid4(),
        )
        cmd.apply(state)
        assert isinstance(state.layout.knots[ka_id], WirePinKnot)
        cmd.revert(state)
        restored = state.layout.knots[ka_id]
        assert isinstance(restored, JointKnot)
        assert restored.id == ka_id
        assert restored.position == Point(0.5, 0.5)


# ---------------------------------------------------------------------------
# Inverse command
# ---------------------------------------------------------------------------


class TestConvertWirePinToEndpoint:
    def test_apply_replaces_wire_pin_with_joint(self) -> None:
        state = _State(board=_board())
        wire, ka_id, _ = _make_half_orphan_wire(
            state,
            Point(0.5, 0.5),
            Point(1.5, 0.5),
        )
        ConvertEndpointToWirePinCommand(
            wire_id=wire.id,
            endpoint="a",
            net_id=uuid.uuid4(),
        ).apply(state)
        # Now convert back via the inverse command.
        inv = ConvertWirePinToEndpointCommand(wire_pin_id=ka_id)
        inv.apply(state)
        knot = state.layout.knots[ka_id]
        assert isinstance(knot, JointKnot)
        assert knot.id == ka_id
        assert knot.wire_ids == frozenset({wire.id})

    def test_revert_restores_wire_pin(self) -> None:
        state = _State(board=_board())
        wire, ka_id, _ = _make_half_orphan_wire(
            state,
            Point(0.5, 0.5),
            Point(1.5, 0.5),
        )
        ConvertEndpointToWirePinCommand(
            wire_id=wire.id,
            endpoint="a",
            net_id=uuid.uuid4(),
        ).apply(state)
        inv = ConvertWirePinToEndpointCommand(wire_pin_id=ka_id)
        inv.apply(state)
        inv.revert(state)
        assert isinstance(state.layout.knots[ka_id], WirePinKnot)

    def test_rejects_non_wire_pin(self) -> None:
        state = _State(board=_board())
        wire, ka_id, _ = _make_half_orphan_wire(
            state,
            Point(0.5, 0.5),
            Point(1.5, 0.5),
        )
        # ka is still a JointKnot — inverse command should reject.
        inv = ConvertWirePinToEndpointCommand(wire_pin_id=ka_id)
        with pytest.raises(ValueError, match="expected WirePinKnot"):
            inv.apply(state)


# ---------------------------------------------------------------------------
# Delete wire after conversion ⇒ wire-pin disappears; undo brings it back.
# ---------------------------------------------------------------------------


class TestWirePinDeletedWithWire:
    def test_remove_wire_capturing_orphans_saves_wire_pin(self) -> None:
        from protoboard.domain.layout.wire_utils import (
            remove_wire_capturing_orphans,
            restore_wire_with_orphans,
        )

        state = _State(board=_board())
        wire, ka_id, kb_id = _make_half_orphan_wire(
            state,
            Point(0.5, 0.5),
            Point(1.5, 0.5),
        )
        # Promote both endpoints to wire-pins.
        ConvertEndpointToWirePinCommand(
            wire_id=wire.id,
            endpoint="a",
            net_id=uuid.uuid4(),
        ).apply(state)
        ConvertEndpointToWirePinCommand(
            wire_id=wire.id,
            endpoint="b",
            net_id=uuid.uuid4(),
        ).apply(state)
        assert isinstance(state.layout.knots[ka_id], WirePinKnot)
        assert isinstance(state.layout.knots[kb_id], WirePinKnot)

        saved_wire, saved_orphans = remove_wire_capturing_orphans(
            state.layout,
            wire.id,
        )
        # Both wire-pins captured as orphans (sole owners gone).
        assert ka_id not in state.layout.knots
        assert kb_id not in state.layout.knots
        captured_ids = {k.id for k in saved_orphans}
        assert captured_ids == {ka_id, kb_id}
        assert all(isinstance(k, WirePinKnot) for k in saved_orphans)

        # Undo restores wire AND both wire-pins at their original ids.
        assert saved_wire is not None
        restore_wire_with_orphans(state.layout, saved_wire, saved_orphans)
        assert isinstance(state.layout.knots[ka_id], WirePinKnot)
        assert isinstance(state.layout.knots[kb_id], WirePinKnot)
