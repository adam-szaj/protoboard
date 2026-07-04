# SPDX-License-Identifier: MIT
"""Tests for ConflictScanner — the cohesive view over pad/wire-conflict detection.

The scanner only gathers the shared ``placements / instances / components /
schematic / pad_index`` bundle once and delegates to the free functions. These
tests assert method-vs-function *equivalence* on the same fixtures the free
functions are already tested against, so the wrapper can never silently diverge.
"""

from __future__ import annotations

import uuid
from dataclasses import dataclass

from protoboard.domain.components.definition import ComponentDefinition
from protoboard.domain.components.footprints import SIL
from protoboard.domain.components.instance import ComponentInstance
from protoboard.domain.layout.group import ComponentGroup
from protoboard.domain.layout.pad_conflict import (
    ConflictScanner,
    MoveConflictSeverity,
    check_group_move_conflicts,
    check_multi_group_move_conflicts,
    find_conflicts,
    find_wire_conflicts,
    find_wires_crossing_pads,
)
from protoboard.domain.layout.placement import ComponentPlacement
from protoboard.domain.layout.wire import Wire
from protoboard.domain.schematic.net import Net
from protoboard.domain.schematic.schematic import PinRef, Schematic
from protoboard.geometry import Point

ROOT_GID = uuid.UUID("00000000-0000-0000-0000-000000000001")


# ---------------------------------------------------------------------------
# Helpers (mirroring test_pad_conflict / test_group_move_conflict)
# ---------------------------------------------------------------------------

def _defn(name: str, n: int = 2) -> ComponentDefinition:
    return ComponentDefinition.from_footprint(
        name=name, footprint=SIL(n), pin_names={i: str(i) for i in range(1, n + 1)}
    )


def _inst(defn: ComponentDefinition, refdes: str = "X1") -> ComponentInstance:
    return ComponentInstance.new(
        definition_ref=defn.name, refdes=refdes, value="",
    ).bind(defn, {})


def _pl(comp_id: uuid.UUID, x: float, y: float, rot: int = 90) -> ComponentPlacement:
    return ComponentPlacement(
        component_id=comp_id, position=Point(x, y), rotation=rot, side="top"
    )


def _wire(*pts: tuple[float, float]) -> Wire:
    return Wire(
        id=uuid.uuid4(),
        path=[Point(x, y) for x, y in pts],
        insulated=False,
        side="top",
    )


def _schematic(*nets: tuple[uuid.UUID, list[tuple[uuid.UUID, int]]]) -> Schematic:
    net_objs: dict[uuid.UUID, Net] = {}
    for net_id, pins in nets:
        net_objs[net_id] = Net(
            id=net_id,
            name=f"N{net_id.int}",
            pins={PinRef(component_id=c, pin_number=p) for c, p in pins},
        )
    return Schematic(nets=net_objs)


def _root_group(*comp_ids: uuid.UUID) -> dict[uuid.UUID, object]:
    return {
        ROOT_GID: ComponentGroup(
            id=ROOT_GID,
            name="root",
            component_ids=frozenset(comp_ids),
            child_group_ids=frozenset(),
            exported_pins=frozenset(),
        )
    }


@dataclass
class _FakeLayout:
    placements: dict[uuid.UUID, ComponentPlacement]


@dataclass
class _FakeSession:
    """Minimal EditorSession-like duck for ConflictScanner.from_session."""
    layout: _FakeLayout
    instances: dict[uuid.UUID, ComponentInstance]
    components: dict[str, ComponentDefinition]
    schematic: Schematic


# ---------------------------------------------------------------------------
# find_pad_conflicts equivalence
# ---------------------------------------------------------------------------

class TestPadConflictEquivalence:
    def test_diff_net_pad_conflict_matches_free_function(self) -> None:
        net_a, net_b = uuid.uuid4(), uuid.uuid4()
        defn = _defn("R")
        r1 = _inst(defn, "R1")
        r2 = _inst(defn, "R2")
        # R1 at (3,0) pin1 → (3,0); R2 dropped at (3,0) → pin1 also (3,0).
        pl_r1 = _pl(r1.id, 3.0, 0.0)
        pl_r2 = _pl(r2.id, 3.0, 0.0)
        sch = _schematic((net_a, [(r1.id, 1)]), (net_b, [(r2.id, 1)]))
        placements = {r1.id: pl_r1, r2.id: pl_r2}
        instances = {r1.id: r1, r2.id: r2}
        components = {"R": defn}

        expected = find_conflicts(
            incoming_placement=pl_r2,
            incoming_defn=defn,
            incoming_instance_id=r2.id,
            layout_placements=placements,
            instances=instances,
            components=components,
            schematic=sch,
        )
        scanner = ConflictScanner(
            placements=placements,
            instances=instances,
            components=components,
            schematic=sch,
        )
        got = scanner.find_pad_conflicts(pl_r2, defn, r2.id)

        assert got == expected
        # Both pins of the two coincident SIL(2) placements overlap → the
        # scanner reports exactly what the free function does.
        assert len(got) == len(expected) >= 1

    def test_no_conflict_matches_free_function(self) -> None:
        defn = _defn("R")
        r1 = _inst(defn, "R1")
        r2 = _inst(defn, "R2")
        pl_r1 = _pl(r1.id, 0.5, 0.5)
        pl_r2 = _pl(r2.id, 5.5, 0.5)
        placements = {r1.id: pl_r1, r2.id: pl_r2}
        instances = {r1.id: r1, r2.id: r2}
        components = {"R": defn}
        sch = Schematic(nets={})

        scanner = ConflictScanner(
            placements=placements,
            instances=instances,
            components=components,
            schematic=sch,
        )
        assert scanner.find_pad_conflicts(pl_r2, defn, r2.id) == []


# ---------------------------------------------------------------------------
# find_wire_conflicts equivalence
# ---------------------------------------------------------------------------

class TestWireConflictEquivalence:
    def test_pin_on_wire_matches_free_function(self) -> None:
        defn = _defn("R")
        r1 = _inst(defn, "R1")
        pl = _pl(r1.id, 2.0, 2.0)  # pins at (2,2) and (3,2)
        wire = _wire((2.0, 2.0), (2.0, 5.0))  # passes through (2,2)
        sch = Schematic(nets={})
        placements = {r1.id: pl}
        instances = {r1.id: r1}
        components = {"R": defn}
        point_to_net: dict[tuple[float, float], tuple[uuid.UUID, str]] = {}

        expected = find_wire_conflicts(
            incoming_placement=pl,
            incoming_defn=defn,
            incoming_instance_id=r1.id,
            wires=[wire],
            schematic=sch,
            point_to_net=point_to_net,
            already_conflicted=set(),
            incoming_instance=r1,
        )
        scanner = ConflictScanner(
            placements=placements,
            instances=instances,
            components=components,
            schematic=sch,
        )
        got = scanner.find_wire_conflicts(
            pl, defn, r1.id, [wire], point_to_net, set()
        )
        assert got == expected


# ---------------------------------------------------------------------------
# find_wires_crossing_pads equivalence
# ---------------------------------------------------------------------------

class TestWiresCrossingEquivalence:
    def test_crossing_matches_free_function(self) -> None:
        net_a = uuid.uuid4()
        defn = _defn("R", 1)
        r1 = _inst(defn, "R1")
        pl = _pl(r1.id, 2.0, 2.0, rot=0)  # single pin at (2,2)
        sch = _schematic((net_a, [(r1.id, 1)]))
        # Bare wire (no net) crossing straight over the netted pad at (2,2).
        wire = _wire((2.0, 0.0), (2.0, 5.0))
        placements = {r1.id: pl}
        instances = {r1.id: r1}
        components = {"R": defn}

        expected = find_wires_crossing_pads(
            wires=[wire],
            moved_comp_ids=set(),
            layout_placements=placements,
            instances=instances,
            components=components,
            schematic=sch,
        )
        scanner = ConflictScanner(
            placements=placements,
            instances=instances,
            components=components,
            schematic=sch,
        )
        got = scanner.find_wires_crossing_pads([wire], set())
        assert got == expected
        assert wire.id in got  # bare wire on alien netted pad → conflicting


# ---------------------------------------------------------------------------
# check_group_move equivalence
# ---------------------------------------------------------------------------

class TestGroupMoveEquivalence:
    def test_blocked_matches_free_function(self) -> None:
        net_a, net_b = uuid.uuid4(), uuid.uuid4()
        defn_a = _defn("A", 1)
        defn_b = _defn("B", 1)
        inst_a = _inst(defn_a, "A1")
        inst_b = _inst(defn_b, "B1")
        pl_a = _pl(inst_a.id, 3.0, 0.0, rot=0)
        pl_b = _pl(inst_b.id, 3.0, 0.0, rot=0)
        groups = _root_group(inst_a.id)
        sch = _schematic((net_a, [(inst_a.id, 1)]), (net_b, [(inst_b.id, 1)]))
        placements = {inst_a.id: pl_a, inst_b.id: pl_b}
        instances = {inst_a.id: inst_a, inst_b.id: inst_b}
        components = {"A": defn_a, "B": defn_b}

        expected = check_group_move_conflicts(
            moved_ids={inst_a.id},
            active_group_id=ROOT_GID,
            layout_placements=placements,
            layout_wires=[],
            layout_groups=groups,
            instances=instances,
            components=components,
            schematic=sch,
        )
        scanner = ConflictScanner(
            placements=placements,
            instances=instances,
            components=components,
            schematic=sch,
        )
        got = scanner.check_group_move(
            moved_ids={inst_a.id},
            active_group_id=ROOT_GID,
            layout_wires=[],
            layout_groups=groups,
        )
        assert got.severity == expected.severity == MoveConflictSeverity.BLOCKED
        assert len(got.blocking_pad_positions) == len(expected.blocking_pad_positions)

    def test_clear_matches_free_function(self) -> None:
        defn = _defn("A", 1)
        inst = _inst(defn, "A1")
        pl = _pl(inst.id, 0.0, 0.0, rot=0)
        groups = _root_group(inst.id)
        placements = {inst.id: pl}
        instances = {inst.id: inst}
        components = {"A": defn}
        sch = _schematic()

        scanner = ConflictScanner(
            placements=placements,
            instances=instances,
            components=components,
            schematic=sch,
        )
        got = scanner.check_group_move(
            moved_ids={inst.id},
            active_group_id=ROOT_GID,
            layout_wires=[],
            layout_groups=groups,
        )
        assert got.severity == MoveConflictSeverity.CLEAR


# ---------------------------------------------------------------------------
# check_multi_group_move equivalence (via a minimal DragSet-like duck)
# ---------------------------------------------------------------------------

@dataclass
class _FakeDragSet:
    """Minimal DragSet duck: only the attributes the scanner forwards."""
    _comp_ids: frozenset[uuid.UUID]
    groups: frozenset[uuid.UUID]

    def all_component_ids(
        self, groups_map: dict[uuid.UUID, object]
    ) -> frozenset[uuid.UUID]:
        return self._comp_ids


class TestMultiGroupMoveEquivalence:
    def test_matches_free_function(self) -> None:
        defn = _defn("A", 1)
        inst = _inst(defn, "A1")
        pl = _pl(inst.id, 0.0, 0.0, rot=0)
        groups = _root_group(inst.id)
        placements = {inst.id: pl}
        instances = {inst.id: inst}
        components = {"A": defn}
        sch = _schematic()
        drag_set = _FakeDragSet(
            _comp_ids=frozenset({inst.id}), groups=frozenset({ROOT_GID})
        )

        expected = check_multi_group_move_conflicts(
            drag_set=drag_set,
            active_group_id=ROOT_GID,
            layout_placements=placements,
            layout_wires=[],
            layout_groups=groups,
            instances=instances,
            components=components,
            schematic=sch,
        )
        scanner = ConflictScanner(
            placements=placements,
            instances=instances,
            components=components,
            schematic=sch,
        )
        got = scanner.check_multi_group_move(
            drag_set=drag_set,
            active_group_id=ROOT_GID,
            layout_wires=[],
            layout_groups=groups,
        )
        assert got.severity == expected.severity


# ---------------------------------------------------------------------------
# from_session
# ---------------------------------------------------------------------------

class TestFromSession:
    def test_from_session_reads_context(self) -> None:
        defn = _defn("R")
        r1 = _inst(defn, "R1")
        pl = _pl(r1.id, 0.5, 0.5)
        sch = Schematic(nets={})
        session = _FakeSession(
            layout=_FakeLayout(placements={r1.id: pl}),
            instances={r1.id: r1},
            components={"R": defn},
            schematic=sch,
        )
        scanner = ConflictScanner.from_session(session)

        assert scanner.placements == {r1.id: pl}
        assert scanner.instances == {r1.id: r1}
        assert scanner.components == {"R": defn}
        assert scanner.schematic is sch
        assert scanner.pad_index is None

    def test_from_session_forwards_pad_index(self) -> None:
        defn = _defn("R")
        r1 = _inst(defn, "R1")
        pl = _pl(r1.id, 0.5, 0.5)
        session = _FakeSession(
            layout=_FakeLayout(placements={r1.id: pl}),
            instances={r1.id: r1},
            components={"R": defn},
            schematic=Schematic(nets={}),
        )
        sentinel = object()
        scanner = ConflictScanner.from_session(session, pad_index=sentinel)  # type: ignore[arg-type]
        assert scanner.pad_index is sentinel
