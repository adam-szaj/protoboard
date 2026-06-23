# SPDX-License-Identifier: MIT
"""Tests for FuseTouchingWiresCommand — runtime equivalent of the
save-time ``endpoint_merge_on_save`` pass.

Two wires that touch endpoint-to-endpoint on an off-pin pad and share
``insulated`` represent one physical solderable conductor.  At save
time ``merge_endpoint_wires`` fuses them; in runtime this happens only
when ``AddWireCommand`` lands a new wire next to them.  After autoroute
or partial drag, two wires can co-exist as separate objects until the
user explicitly runs the merge action.
"""

from __future__ import annotations

import uuid
from dataclasses import dataclass, field

from protoboard.domain.board.board import Board
from protoboard.domain.board.boxes import BoardGrid, PadAtom, SizePolicy
from protoboard.domain.board.pads import PadLayers
from protoboard.domain.commands.component_cmds import (
    FuseTouchingWiresCommand,
)
from protoboard.domain.components.definition import ComponentDefinition
from protoboard.domain.components.footprints import SIL
from protoboard.domain.components.instance import ComponentInstance
from protoboard.domain.layout.layout import Layout
from protoboard.domain.layout.placement import (
    ComponentPlacement,
    pin_world_position,
)
from protoboard.domain.layout.wire import KnotRef, Wire, WireCollection
from protoboard.domain.schematic.net import PinRef
from protoboard.domain.schematic.schematic import Schematic
from protoboard.geometry import Point


def _board() -> Board:
    return Board(root=BoardGrid(
        width_policy=SizePolicy.HUG, height_policy=SizePolicy.HUG,
        cell=PadAtom(
            width_policy=SizePolicy.HUG, height_policy=SizePolicy.HUG,
            layers=PadLayers(top=True, bottom=True, plated_through=True),
        ),
        rows=20, cols=20, row_gap=0.0, col_gap=0.0,
    ))


@dataclass
class _State:
    layout: Layout
    board: Board = field(default_factory=_board)
    instances: dict[uuid.UUID, ComponentInstance] = field(default_factory=dict)
    components: dict[str, ComponentDefinition] = field(default_factory=dict)
    schematic: Schematic = field(default_factory=lambda: Schematic(nets={}))


def _two_wires_sharing_joint(
    *, insulated_a: bool, insulated_b: bool, side: str = "top",
) -> tuple[_State, uuid.UUID, uuid.UUID, uuid.UUID]:
    """Build a layout with two wires sharing a single 2-degree JointKnot
    at an off-pin pad.

    Pin A endpoint → joint → Pin B endpoint, but split as TWO wires
    meeting at the joint (the situation an autoroute commit can leave).

    Returns (state, wire1_id, wire2_id, joint_id).
    """
    defn = ComponentDefinition(
        name="CONN", footprint=SIL(2), pin_names={1: "1", 2: "2"},
    )
    layout = Layout(placements={}, wires=WireCollection([]))
    inst_a = ComponentInstance.new(definition_ref="CONN", refdes="J1", value="")
    inst_b = ComponentInstance.new(definition_ref="CONN", refdes="J2", value="")
    instances = {inst_a.id: inst_a, inst_b.id: inst_b}
    layout.placements[inst_a.id] = ComponentPlacement(
        component_id=inst_a.id, position=Point(3.0, 5.0), rotation=0, side="top",
    )
    layout.placements[inst_b.id] = ComponentPlacement(
        component_id=inst_b.id, position=Point(8.0, 5.0), rotation=0, side="top",
    )
    pa = pin_world_position(defn.footprint, layout.placements[inst_a.id], 1)
    pb = pin_world_position(defn.footprint, layout.placements[inst_b.id], 1)
    # Off-pin meeting point right between J1.pin1 and J2.pin1.
    hub = Point(round((pa.x + pb.x) / 2), pa.y)

    pka = layout.get_or_create_pin_knot(PinRef(inst_a.id, 1))
    pkb = layout.get_or_create_pin_knot(PinRef(inst_b.id, 1))
    jk = layout.create_joint_knot(hub)

    w1 = Wire(
        id=uuid.uuid4(),
        path=[pa, hub], side=side,  # type: ignore[arg-type]
        insulated=insulated_a,
        a=KnotRef(id=pka.id), b=KnotRef(id=jk.id),
    )
    layout.add_wire(w1)
    w2 = Wire(
        id=uuid.uuid4(),
        path=[hub, pb], side=side,  # type: ignore[arg-type]
        insulated=insulated_b,
        a=KnotRef(id=jk.id), b=KnotRef(id=pkb.id),
    )
    layout.add_wire(w2)

    state = _State(
        layout=layout, instances=instances, components={"CONN": defn},
    )
    return state, w1.id, w2.id, jk.id


class TestFuseTouchingWires:
    def test_fuses_two_bare_wires_meeting_at_joint(self) -> None:
        state, w1_id, w2_id, joint_id = _two_wires_sharing_joint(
            insulated_a=False, insulated_b=False,
        )
        assert len(state.layout.wires) == 2

        cmd = FuseTouchingWiresCommand()
        cmd.apply(state)

        # Exactly one wire remains; the shared joint is gone.
        assert len(state.layout.wires) == 1
        assert joint_id not in state.layout.knots
        # Recorded exactly one collapse.
        assert len(cmd._records) == 1

    def test_fuses_two_insulated_wires(self) -> None:
        state, _, _, joint_id = _two_wires_sharing_joint(
            insulated_a=True, insulated_b=True,
        )
        cmd = FuseTouchingWiresCommand()
        cmd.apply(state)
        assert len(state.layout.wires) == 1
        assert joint_id not in state.layout.knots

    def test_mismatched_insulation_not_fused(self) -> None:
        state, _, _, joint_id = _two_wires_sharing_joint(
            insulated_a=False, insulated_b=True,
        )
        cmd = FuseTouchingWiresCommand()
        cmd.apply(state)
        # Wires stayed apart; joint still has 2 owners.
        assert len(state.layout.wires) == 2
        assert joint_id in state.layout.knots
        assert len(cmd._records) == 0

    def test_revert_reinstates_both_source_wires(self) -> None:
        state, w1_id, w2_id, joint_id = _two_wires_sharing_joint(
            insulated_a=False, insulated_b=False,
        )
        original_ids = {w1_id, w2_id}

        cmd = FuseTouchingWiresCommand()
        cmd.apply(state)
        assert len(state.layout.wires) == 1

        cmd.revert(state)
        # Both source wires reinstated with their original ids.
        wire_ids = {w.id for w in state.layout.wires}
        assert wire_ids == original_ids
        # Shared joint reappears as 2-degree.
        assert joint_id in state.layout.knots
        from protoboard.domain.layout.knot import JointKnot
        jk = state.layout.knots[joint_id]
        assert isinstance(jk, JointKnot)
        assert len(jk.wire_ids) == 2

    def test_scope_filter_restricts_to_supplied_ids(self) -> None:
        """When ``wire_ids`` is set, only joints between two scope-member
        wires get fused — unrelated touching pairs stay apart."""
        state, w1_id, w2_id, joint_id = _two_wires_sharing_joint(
            insulated_a=False, insulated_b=False,
        )

        # Scope excludes wire2 — fuse must not happen.
        cmd = FuseTouchingWiresCommand(wire_ids=frozenset({w1_id}))
        cmd.apply(state)
        assert len(state.layout.wires) == 2
        assert joint_id in state.layout.knots

        # Now with both — fuse fires.
        cmd2 = FuseTouchingWiresCommand(wire_ids=frozenset({w1_id, w2_id}))
        cmd2.apply(state)
        assert len(state.layout.wires) == 1

    def test_chain_of_three_collapses_via_multiple_passes(self) -> None:
        """Three touching bare wires fuse all the way to one — needs
        the internal ``progress`` loop to fire more than once."""
        # Build three wires in a row: P1—J1—J2—P2 split as
        #   w_a: P1→J1, w_b: J1→J2, w_c: J2→P2
        defn = ComponentDefinition(
            name="CONN", footprint=SIL(2), pin_names={1: "1", 2: "2"},
        )
        layout = Layout(placements={}, wires=WireCollection([]))
        inst_a = ComponentInstance.new(definition_ref="CONN", refdes="J1", value="")
        inst_b = ComponentInstance.new(definition_ref="CONN", refdes="J2", value="")
        instances = {inst_a.id: inst_a, inst_b.id: inst_b}
        layout.placements[inst_a.id] = ComponentPlacement(
            component_id=inst_a.id, position=Point(2.0, 5.0), rotation=0, side="top",
        )
        layout.placements[inst_b.id] = ComponentPlacement(
            component_id=inst_b.id, position=Point(10.0, 5.0), rotation=0, side="top",
        )
        pa = pin_world_position(defn.footprint, layout.placements[inst_a.id], 1)
        pb = pin_world_position(defn.footprint, layout.placements[inst_b.id], 1)
        hub1 = Point(5.0, pa.y)
        hub2 = Point(8.0, pa.y)
        pka = layout.get_or_create_pin_knot(PinRef(inst_a.id, 1))
        pkb = layout.get_or_create_pin_knot(PinRef(inst_b.id, 1))
        j1 = layout.create_joint_knot(hub1)
        j2 = layout.create_joint_knot(hub2)

        for path, a, b in (
            ([pa, hub1], pka.id, j1.id),
            ([hub1, hub2], j1.id, j2.id),
            ([hub2, pb], j2.id, pkb.id),
        ):
            w = Wire(
                id=uuid.uuid4(), path=path, side="top",
                insulated=False,
                a=KnotRef(id=a), b=KnotRef(id=b),
            )
            layout.add_wire(w)

        state = _State(
            layout=layout, instances=instances, components={"CONN": defn},
        )
        cmd = FuseTouchingWiresCommand()
        cmd.apply(state)
        assert len(state.layout.wires) == 1
        # Both intermediate joints gone.
        assert j1.id not in state.layout.knots
        assert j2.id not in state.layout.knots

    def test_pin_joints_not_collapsed(self) -> None:
        """A pin endpoint is a physical hub — never fused even with
        only one neighbouring wire.  The wire stays distinct."""
        # Single wire pin1 → pin2.  No 2-degree off-pin joint exists,
        # so the command should record zero collapses.
        defn = ComponentDefinition(
            name="CONN", footprint=SIL(2), pin_names={1: "1", 2: "2"},
        )
        layout = Layout(placements={}, wires=WireCollection([]))
        inst = ComponentInstance.new(
            definition_ref="CONN", refdes="J1", value="",
        )
        instances = {inst.id: inst}
        layout.placements[inst.id] = ComponentPlacement(
            component_id=inst.id, position=Point(3.0, 5.0), rotation=0, side="top",
        )
        p1 = pin_world_position(defn.footprint, layout.placements[inst.id], 1)
        p2 = pin_world_position(defn.footprint, layout.placements[inst.id], 2)
        pk1 = layout.get_or_create_pin_knot(PinRef(inst.id, 1))
        pk2 = layout.get_or_create_pin_knot(PinRef(inst.id, 2))
        w = Wire(
            id=uuid.uuid4(), path=[p1, p2], side="top",
            insulated=False,
            a=KnotRef(id=pk1.id), b=KnotRef(id=pk2.id),
        )
        layout.add_wire(w)
        state = _State(
            layout=layout, instances=instances, components={"CONN": defn},
        )
        cmd = FuseTouchingWiresCommand()
        cmd.apply(state)
        assert len(state.layout.wires) == 1
        assert len(cmd._records) == 0
