# SPDX-License-Identifier: MIT
"""Round-trip persistence test for wire side changes.

Scenario: two 1×2 connectors with nets pin1-pin1 and pin2-pin2, both
wires routed on the same side; then one wire is moved to the
opposite layer, the project is saved, reopened, and the per-wire
side state is asserted.

Exercises the 2×2 matrix of (first_side ∈ {top, bottom}) ×
(wire_kind ∈ {bare, insulated}) so any side serialisation bug is
caught on every combination — top/bottom symmetry, bare vs insulated
encoding.
"""

from __future__ import annotations

import uuid
from dataclasses import dataclass, field
from pathlib import Path

import pytest

from protoboard.domain.board.board import Board
from protoboard.domain.board.boxes import BoardGrid, PadAtom, SizePolicy
from protoboard.domain.board.pads import PadLayers
from protoboard.domain.commands.base import CommandStack
from protoboard.domain.commands.component_cmds import (
    SetWireInsulatedCommand,
    SetWireSideCommand,
)
from protoboard.domain.components.definition import ComponentDefinition
from protoboard.domain.components.footprints import SIL
from protoboard.domain.components.instance import ComponentInstance
from protoboard.domain.layout.layout import Layout
from protoboard.domain.layout.placement import (
    ComponentPlacement,
    pin_world_position,
    PlacementCollection,
)
from protoboard.domain.layout.wire import KnotRef, Wire, WireCollection
from protoboard.domain.schematic.net import Net, PinRef
from protoboard.domain.schematic.schematic import Schematic
from protoboard.geometry import Point
from protoboard.services.persistence.project_yaml import (
    load_project,
    save_project,
    set_migration_library,
)


# ---------------------------------------------------------------------------
# Library stub (round-trip needs ``set_migration_library`` for PinKnot resolution)
# ---------------------------------------------------------------------------


@dataclass
class _MiniLibrary:
    _by_name: dict[str, ComponentDefinition] = field(default_factory=dict)

    def names(self):  # type: ignore[no-untyped-def]
        return self._by_name.keys()

    def get(self, name: str) -> ComponentDefinition:
        return self._by_name[name]

    def __contains__(self, name: object) -> bool:
        return isinstance(name, str) and name in self._by_name


def _connector_lib() -> _MiniLibrary:
    """SIL2 connector definition keyed under ``CONN_1x2``."""
    defn = ComponentDefinition.from_footprint(
        name="CONN_1x2",
        footprint=SIL(2),
        pin_names={1: "1", 2: "2"},
    )
    lib = _MiniLibrary()
    lib._by_name["CONN_1x2"] = defn
    return lib


def _board() -> Board:
    """Double-sided board big enough to host two connectors + routed wires."""
    return Board(root=BoardGrid(
        width_policy=SizePolicy.HUG,
        height_policy=SizePolicy.HUG,
        cell=PadAtom(
            width_policy=SizePolicy.HUG,
            height_policy=SizePolicy.HUG,
            layers=PadLayers(top=True, bottom=True, plated_through=True),
        ),
        rows=20, cols=20, row_gap=0.0, col_gap=0.0,
    ))


# ---------------------------------------------------------------------------
# Build helper
# ---------------------------------------------------------------------------


def _build_two_connectors_with_wires(
    *, first_side: str, insulated: bool,
) -> tuple[Layout, dict, Schematic, uuid.UUID, uuid.UUID]:
    """Place two SIL(2) connectors, build pin1-pin1 and pin2-pin2 nets and
    wires.

    Returns (layout, instances, schematic, wire1_id, wire2_id) where
    wire1 connects pins #1 of both connectors and wire2 connects pins #2.
    Both wires are routed on *first_side* with the requested insulation.
    """
    defn = ComponentDefinition.from_footprint(
        name="CONN_1x2", footprint=SIL(2), pin_names={1: "1", 2: "2"},
    )
    layout = Layout(placements=PlacementCollection({}), wires=WireCollection())

    inst_a = ComponentInstance.new(
        definition_ref="CONN_1x2", refdes="J1", value="",
    )
    inst_b = ComponentInstance.new(
        definition_ref="CONN_1x2", refdes="J2", value="",
    )
    instances = {inst_a.id: inst_a, inst_b.id: inst_b}

    # Place J1 and J2 four columns apart so the wires run horizontally.
    # SIL(2) at (col, row) lays pin 1 at (col, row), pin 2 at (col, row+1).
    layout.placements[inst_a.id] = ComponentPlacement(
        component_id=inst_a.id, position=Point(3.0, 5.0),
        rotation=0, side="top",
    )
    layout.placements[inst_b.id] = ComponentPlacement(
        component_id=inst_b.id, position=Point(8.0, 5.0),
        rotation=0, side="top",
    )

    # Pin world positions.
    a_pin1 = pin_world_position(defn.default_footprint(), layout.placements[inst_a.id], 1)
    a_pin2 = pin_world_position(defn.default_footprint(), layout.placements[inst_a.id], 2)
    b_pin1 = pin_world_position(defn.default_footprint(), layout.placements[inst_b.id], 1)
    b_pin2 = pin_world_position(defn.default_footprint(), layout.placements[inst_b.id], 2)

    # PinKnots for endpoint anchoring.
    pka1 = layout.get_or_create_pin_knot(PinRef(inst_a.id, 1))
    pka2 = layout.get_or_create_pin_knot(PinRef(inst_a.id, 2))
    pkb1 = layout.get_or_create_pin_knot(PinRef(inst_b.id, 1))
    pkb2 = layout.get_or_create_pin_knot(PinRef(inst_b.id, 2))

    # Wire 1: pin#1 of both connectors.
    wire1 = Wire(
        id=uuid.uuid4(),
        path=[a_pin1, b_pin1],
        side=first_side,  # type: ignore[arg-type]
        insulated=insulated,
        a=KnotRef(id=pka1.id), b=KnotRef(id=pkb1.id),
    )
    layout.add_wire(wire1)

    # Wire 2: pin#2 of both connectors.
    wire2 = Wire(
        id=uuid.uuid4(),
        path=[a_pin2, b_pin2],
        side=first_side,  # type: ignore[arg-type]
        insulated=insulated,
        a=KnotRef(id=pka2.id), b=KnotRef(id=pkb2.id),
    )
    layout.add_wire(wire2)

    # Schematic nets — one per pair of pins.
    net1 = Net(
        id=uuid.uuid4(), name="N1",
        pins={PinRef(inst_a.id, 1), PinRef(inst_b.id, 1)},
    )
    net2 = Net(
        id=uuid.uuid4(), name="N2",
        pins={PinRef(inst_a.id, 2), PinRef(inst_b.id, 2)},
    )
    schematic = Schematic(nets={net1.id: net1, net2.id: net2})

    return layout, instances, schematic, wire1.id, wire2.id


# ---------------------------------------------------------------------------
# Fake state for SetWireSideCommand
# ---------------------------------------------------------------------------


@dataclass
class _State:
    layout: Layout
    board: Board = field(default_factory=_board)
    instances: dict[uuid.UUID, ComponentInstance] = field(default_factory=dict)
    components: dict[str, ComponentDefinition] = field(default_factory=dict)
    schematic: Schematic = field(default_factory=lambda: Schematic(nets={}))


# ---------------------------------------------------------------------------
# Test matrix
# ---------------------------------------------------------------------------


@pytest.mark.parametrize("first_side", ["top", "bottom"])
@pytest.mark.parametrize("insulated", [False, True],
                         ids=["bare", "insulated"])
def test_one_wire_moved_to_opposite_side_round_trips(
    tmp_path: Path, first_side: str, insulated: bool,
) -> None:
    """Move one of two wires to the opposite side, save+load twice,
    verify final sides per wire."""
    opposite = "bottom" if first_side == "top" else "top"

    # ── Build initial project ────────────────────────────────────────────
    layout, instances, schematic, wire1_id, wire2_id = (
        _build_two_connectors_with_wires(
            first_side=first_side, insulated=insulated,
        )
    )

    # Sanity: both wires start on first_side.
    side_by_id = {w.id: w.side for w in layout.wires}
    assert side_by_id[wire1_id] == first_side
    assert side_by_id[wire2_id] == first_side

    lib = _connector_lib()
    set_migration_library(lib)
    try:
        # ── Save → load (round-trip #1) ─────────────────────────────────
        save_project(
            directory=tmp_path,
            board=_board(),
            layout=layout,
            schematic=schematic,
            instances=instances,
        )
        _b1, l1, _s1, i1, *_ = load_project(tmp_path)

        # After round-trip wires still on first_side.
        side_by_id = {w.id: w.side for w in l1.wires}
        assert side_by_id[wire1_id] == first_side, (
            f"wire1 side lost during initial round-trip: {side_by_id}"
        )
        assert side_by_id[wire2_id] == first_side

        # ── Move wire1 to opposite side ─────────────────────────────────
        state = _State(
            layout=l1, instances=i1, components={"CONN_1x2": lib.get("CONN_1x2")},
            schematic=Schematic(nets={}),
        )
        stack: CommandStack = CommandStack()
        stack.execute(
            SetWireSideCommand(wire_id=wire1_id, side=opposite),
            state,  # type: ignore[arg-type]
        )

        # In-memory: wire1 flipped, wire2 untouched.
        side_by_id = {w.id: w.side for w in l1.wires}
        assert side_by_id[wire1_id] == opposite
        assert side_by_id[wire2_id] == first_side

        # ── Save → load (round-trip #2) ─────────────────────────────────
        save_project(
            directory=tmp_path,
            board=_board(),
            layout=l1,
            schematic=schematic,
            instances=i1,
        )
        _b2, l2, _s2, _i2, *_ = load_project(tmp_path)
    finally:
        set_migration_library(None)

    # ── Final state ─────────────────────────────────────────────────────
    final_sides = {w.id: w.side for w in l2.wires}
    final_insulated = {w.id: w.insulated for w in l2.wires}

    assert final_sides[wire1_id] == opposite, (
        f"wire1 ({first_side=}, {insulated=}) should be on {opposite!r} "
        f"after reload, got {final_sides[wire1_id]!r}"
    )
    assert final_sides[wire2_id] == first_side, (
        f"wire2 ({first_side=}, {insulated=}) should stay on "
        f"{first_side!r}, got {final_sides[wire2_id]!r}"
    )
    # Insulation must round-trip independently of side.
    assert final_insulated[wire1_id] is insulated
    assert final_insulated[wire2_id] is insulated


# ---------------------------------------------------------------------------
# Insulation-toggle variant — same fixture, but flip wire2's insulated
# ---------------------------------------------------------------------------


@pytest.mark.parametrize("first_side", ["top", "bottom"])
@pytest.mark.parametrize("insulated", [False, True],
                         ids=["bare_to_insulated", "insulated_to_bare"])
def test_one_wire_insulation_toggled_round_trips(
    tmp_path: Path, first_side: str, insulated: bool,
) -> None:
    """Flip the insulation of one of two wires, save+load twice,
    verify per-wire insulation state.

    Mirrors :func:`test_one_wire_moved_to_opposite_side_round_trips`
    but exercises ``SetWireInsulatedCommand`` on *wire2* instead of
    the side-flip on *wire1*.  Both sides keep their original layer;
    only the insulation of wire2 toggles.  The ``insulated`` matrix
    axis sets the *starting* insulation of every wire, so the toggle
    moves wire2 from ``insulated`` to ``not insulated``.
    """
    target_insulated = not insulated

    layout, instances, schematic, wire1_id, wire2_id = (
        _build_two_connectors_with_wires(
            first_side=first_side, insulated=insulated,
        )
    )

    # Sanity: both wires start with the same insulation.
    ins_by_id = {w.id: w.insulated for w in layout.wires}
    assert ins_by_id[wire1_id] is insulated
    assert ins_by_id[wire2_id] is insulated

    lib = _connector_lib()
    set_migration_library(lib)
    try:
        # ── Save → load (round-trip #1) ─────────────────────────────────
        save_project(
            directory=tmp_path,
            board=_board(),
            layout=layout,
            schematic=schematic,
            instances=instances,
        )
        _b1, l1, _s1, i1, *_ = load_project(tmp_path)

        ins_by_id = {w.id: w.insulated for w in l1.wires}
        assert ins_by_id[wire1_id] is insulated, (
            f"wire1 insulation lost during initial round-trip: {ins_by_id}"
        )
        assert ins_by_id[wire2_id] is insulated

        # ── Flip wire2's insulation ─────────────────────────────────────
        state = _State(
            layout=l1, instances=i1, components={"CONN_1x2": lib.get("CONN_1x2")},
            schematic=Schematic(nets={}),
        )
        stack: CommandStack = CommandStack()
        stack.execute(
            SetWireInsulatedCommand(wire_id=wire2_id, insulated=target_insulated),
            state,  # type: ignore[arg-type]
        )

        # In-memory: wire2 toggled, wire1 untouched.
        ins_by_id = {w.id: w.insulated for w in l1.wires}
        assert ins_by_id[wire1_id] is insulated
        assert ins_by_id[wire2_id] is target_insulated

        # ── Save → load (round-trip #2) ─────────────────────────────────
        save_project(
            directory=tmp_path,
            board=_board(),
            layout=l1,
            schematic=schematic,
            instances=i1,
        )
        _b2, l2, _s2, _i2, *_ = load_project(tmp_path)
    finally:
        set_migration_library(None)

    # ── Final state ─────────────────────────────────────────────────────
    final_sides = {w.id: w.side for w in l2.wires}
    final_insulated = {w.id: w.insulated for w in l2.wires}

    # Sides stayed on first_side (this test never moved a wire across).
    assert final_sides[wire1_id] == first_side, (
        f"wire1 ({first_side=}) drifted to {final_sides[wire1_id]!r} "
        "during insulation-toggle round-trip"
    )
    assert final_sides[wire2_id] == first_side
    # Insulation: wire1 unchanged, wire2 flipped.
    assert final_insulated[wire1_id] is insulated, (
        f"wire1 insulation drifted: was {insulated}, "
        f"got {final_insulated[wire1_id]}"
    )
    assert final_insulated[wire2_id] is target_insulated, (
        f"wire2 should be {target_insulated} after reload, "
        f"got {final_insulated[wire2_id]}"
    )
