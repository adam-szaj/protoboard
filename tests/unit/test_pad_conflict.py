# SPDX-License-Identifier: MIT
"""Tests for pad-conflict detection logic and DRC OVERLAP exemption."""

from __future__ import annotations

import uuid

from protoboard.domain.board.board import Board
from protoboard.domain.board.boxes import BoardGrid, PadAtom, SizePolicy
from protoboard.domain.board.pads import PadLayers
from protoboard.domain.components.definition import ComponentDefinition
from protoboard.domain.components.footprints import SIL
from protoboard.domain.components.instance import ComponentInstance
from protoboard.domain.layout.layout import Layout
from protoboard.domain.layout.pad_conflict import (
    ConflictKind,
    find_conflicts,
    find_wire_conflicts,
)
from protoboard.domain.layout.placement import ComponentPlacement
from protoboard.domain.layout.wire import Wire
from protoboard.domain.schematic.net import Net
from protoboard.domain.schematic.schematic import PinRef, Schematic
from protoboard.geometry import Point
from protoboard.services.drc.engine import DRCEngine

# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


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


def _r_defn() -> ComponentDefinition:
    return ComponentDefinition(name="R", footprint=SIL(2), pin_names={1: "A", 2: "B"})


def _make_inst(refdes: str = "R1") -> ComponentInstance:
    return ComponentInstance.new(definition_ref="R", refdes=refdes, value="10k")


def _placement(inst: ComponentInstance, x: float, y: float, rot: int = 90,
               shared_pins: frozenset[int] | None = None) -> ComponentPlacement:
    return ComponentPlacement(
        component_id=inst.id,
        position=Point(x, y),
        rotation=rot,
        side="top",
        shared_pins=shared_pins or frozenset(),
    )


# ---------------------------------------------------------------------------
# find_conflicts: no conflict
# ---------------------------------------------------------------------------


class TestFindConflictsNone:
    def test_no_existing_placements(self) -> None:
        defn = _r_defn()
        r1 = _make_inst()
        pl = _placement(r1, 0.5, 0.5)
        conflicts = find_conflicts(
            incoming_placement=pl,
            incoming_defn=defn,
            incoming_instance_id=r1.id,
            layout_placements={},
            instances={},
            components={},
            schematic=Schematic(nets={}),
        )
        assert conflicts == []

    def test_non_overlapping_positions(self) -> None:
        defn = _r_defn()
        r1 = _make_inst("R1")
        r2 = _make_inst("R2")
        # SIL(2) rotation=90: pins at (x, y) and (x+1, y)
        # R1 at (0.5, 0.5) → pins at (0.5,0.5) and (1.5,0.5)
        # R2 at (2.5, 0.5) → pins at (2.5,0.5) and (3.5,0.5)
        pl_r1 = _placement(r1, 0.5, 0.5)
        pl_r2 = _placement(r2, 2.5, 0.5)
        conflicts = find_conflicts(
            incoming_placement=pl_r2,
            incoming_defn=defn,
            incoming_instance_id=r2.id,
            layout_placements={r1.id: pl_r1},
            instances={r1.id: r1},
            components={"R": defn},
            schematic=Schematic(nets={}),
        )
        assert conflicts == []

    def test_self_excluded_from_conflict(self) -> None:
        """Moving a component should not conflict with itself."""
        defn = _r_defn()
        r1 = _make_inst()
        pl = _placement(r1, 0.5, 0.5)
        conflicts = find_conflicts(
            incoming_placement=pl,
            incoming_defn=defn,
            incoming_instance_id=r1.id,
            layout_placements={r1.id: pl},
            instances={r1.id: r1},
            components={"R": defn},
            schematic=Schematic(nets={}),
        )
        assert conflicts == []


# ---------------------------------------------------------------------------
# find_conflicts: BOTH_UNCONNECTED
# ---------------------------------------------------------------------------


class TestFindConflictsBothUnconnected:
    def test_detects_overlap_no_nets(self) -> None:
        defn = _r_defn()
        r1 = _make_inst("R1")
        r2 = _make_inst("R2")
        # Both at same position → pins overlap
        pl_r1 = _placement(r1, 0.5, 0.5)
        pl_r2 = _placement(r2, 0.5, 0.5)
        conflicts = find_conflicts(
            incoming_placement=pl_r2,
            incoming_defn=defn,
            incoming_instance_id=r2.id,
            layout_placements={r1.id: pl_r1},
            instances={r1.id: r1},
            components={"R": defn},
            schematic=Schematic(nets={}),
        )
        assert len(conflicts) == 2
        assert all(c.kind == ConflictKind.BOTH_UNCONNECTED for c in conflicts)

    def test_shared_pin_skipped(self) -> None:
        # If incoming pin is already in shared_pins, it must not appear in conflicts.
        defn = _r_defn()
        r1 = _make_inst("R1")
        r2 = _make_inst("R2")
        pl_r1 = _placement(r1, 0.5, 0.5)
        # Both pins of r2 shared → no conflict should be returned
        pl_r2 = _placement(r2, 0.5, 0.5, shared_pins=frozenset({1, 2}))
        conflicts = find_conflicts(
            incoming_placement=pl_r2,
            incoming_defn=defn,
            incoming_instance_id=r2.id,
            layout_placements={r1.id: pl_r1},
            instances={r1.id: r1},
            components={"R": defn},
            schematic=Schematic(nets={}),
        )
        assert conflicts == []


# ---------------------------------------------------------------------------
# find_conflicts: SAME_NET
# ---------------------------------------------------------------------------


class TestFindConflictsSameNet:
    def test_same_net_both_pins(self) -> None:
        defn = _r_defn()
        r1 = _make_inst("R1")
        r2 = _make_inst("R2")
        pl_r1 = _placement(r1, 0.5, 0.5)
        pl_r2 = _placement(r2, 0.5, 0.5)
        # Both pin1 of r1 and pin1 of r2 on the same net
        net = Net(
            id=uuid.uuid4(),
            name="VCC",
            pins={
                PinRef(component_id=r1.id, pin_number=1),
                PinRef(component_id=r2.id, pin_number=1),
            },
        )
        net2 = Net(
            id=uuid.uuid4(),
            name="GND",
            pins={
                PinRef(component_id=r1.id, pin_number=2),
                PinRef(component_id=r2.id, pin_number=2),
            },
        )
        sch = Schematic(nets={net.id: net, net2.id: net2})
        conflicts = find_conflicts(
            incoming_placement=pl_r2,
            incoming_defn=defn,
            incoming_instance_id=r2.id,
            layout_placements={r1.id: pl_r1},
            instances={r1.id: r1},
            components={"R": defn},
            schematic=sch,
        )
        assert all(c.kind == ConflictKind.SAME_NET for c in conflicts)
        assert len(conflicts) == 2


# ---------------------------------------------------------------------------
# find_conflicts: DIFFERENT_NETS
# ---------------------------------------------------------------------------


class TestFindConflictsDifferentNets:
    def test_different_net_is_blocked(self) -> None:
        defn = _r_defn()
        r1 = _make_inst("R1")
        r2 = _make_inst("R2")
        pl_r1 = _placement(r1, 0.5, 0.5)
        pl_r2 = _placement(r2, 0.5, 0.5)
        net_a = Net(
            id=uuid.uuid4(),
            name="A",
            pins={PinRef(component_id=r1.id, pin_number=1)},
        )
        net_b = Net(
            id=uuid.uuid4(),
            name="B",
            pins={PinRef(component_id=r2.id, pin_number=1)},
        )
        sch = Schematic(nets={net_a.id: net_a, net_b.id: net_b})
        conflicts = find_conflicts(
            incoming_placement=pl_r2,
            incoming_defn=defn,
            incoming_instance_id=r2.id,
            layout_placements={r1.id: pl_r1},
            instances={r1.id: r1},
            components={"R": defn},
            schematic=sch,
        )
        diff = [c for c in conflicts if c.kind == ConflictKind.DIFFERENT_NETS]
        assert len(diff) >= 1
        assert diff[0].incoming_net_name == "B"
        assert diff[0].existing_net_name == "A"


# ---------------------------------------------------------------------------
# find_conflicts: ONE_UNCONNECTED
# ---------------------------------------------------------------------------


class TestFindConflictsOneUnconnected:
    def test_existing_connected_incoming_not(self) -> None:
        defn = _r_defn()
        r1 = _make_inst("R1")
        r2 = _make_inst("R2")
        pl_r1 = _placement(r1, 0.5, 0.5)
        pl_r2 = _placement(r2, 0.5, 0.5)
        # Only pin1 of r1 is in a net; r2's pin1 unconnected
        net = Net(
            id=uuid.uuid4(),
            name="VCC",
            pins={PinRef(component_id=r1.id, pin_number=1)},
        )
        sch = Schematic(nets={net.id: net})
        conflicts = find_conflicts(
            incoming_placement=pl_r2,
            incoming_defn=defn,
            incoming_instance_id=r2.id,
            layout_placements={r1.id: pl_r1},
            instances={r1.id: r1},
            components={"R": defn},
            schematic=sch,
        )
        one_unc = [c for c in conflicts if c.kind == ConflictKind.ONE_UNCONNECTED]
        assert len(one_unc) >= 1
        # pin1 of r2 hits pin1 of r1 (which has a net); r2 has no net → one unconnected
        pin1_conflicts = [c for c in one_unc if c.incoming_pin == 1]
        assert len(pin1_conflicts) == 1
        assert pin1_conflicts[0].existing_net_id == net.id
        assert pin1_conflicts[0].incoming_net_id is None


# ---------------------------------------------------------------------------
# DRC OVERLAP exemption for shared_pins
# ---------------------------------------------------------------------------


class TestDRCOverlapSharedPins:
    def _run_drc(
        self,
        r1: ComponentInstance,
        r2: ComponentInstance,
        defn: ComponentDefinition,
        pl_r1: ComponentPlacement,
        pl_r2: ComponentPlacement,
    ) -> list[str]:
        sch = Schematic(nets={})
        lay = Layout(placements={r1.id: pl_r1, r2.id: pl_r2}, wires=[])
        engine = DRCEngine()
        board = _perfboard(1, 4)
        issues = engine.check(
            board=board,
            schematic=sch,
            layout=lay,
            components={defn.name: defn},
            instances={r1.id: r1, r2.id: r2},
        )
        return [i.code for i in issues]

    def test_overlap_without_shared_pins_is_error(self) -> None:
        defn = _r_defn()
        r1 = _make_inst("R1")
        r2 = _make_inst("R2")
        pl_r1 = _placement(r1, 0.5, 0.5)
        pl_r2 = _placement(r2, 0.5, 0.5)
        codes = self._run_drc(r1, r2, defn, pl_r1, pl_r2)
        assert "OVERLAP" in codes

    def test_overlap_with_both_shared_pins_no_error(self) -> None:
        """If all conflicting pins are in shared_pins on both sides, no OVERLAP."""
        defn = _r_defn()
        r1 = _make_inst("R1")
        r2 = _make_inst("R2")
        # Both pins shared on both components
        pl_r1 = _placement(r1, 0.5, 0.5, shared_pins=frozenset({1, 2}))
        pl_r2 = _placement(r2, 0.5, 0.5, shared_pins=frozenset({1, 2}))
        codes = self._run_drc(r1, r2, defn, pl_r1, pl_r2)
        assert "OVERLAP" not in codes

    def test_partial_shared_pins_still_errors(self) -> None:
        """Only pin 1 shared on r2 but not r1 → pin 1 still in unshared set."""
        defn = _r_defn()
        r1 = _make_inst("R1")
        r2 = _make_inst("R2")
        pl_r1 = _placement(r1, 0.5, 0.5)
        pl_r2 = _placement(r2, 0.5, 0.5, shared_pins=frozenset({1}))
        codes = self._run_drc(r1, r2, defn, pl_r1, pl_r2)
        assert "OVERLAP" in codes


# ---------------------------------------------------------------------------
# find_wire_conflicts
# ---------------------------------------------------------------------------

def _wire(path: list[Point]) -> Wire:
    return Wire(
        id=uuid.uuid4(), path=path, insulated=True, side="top"
    )


class TestFindWireConflicts:
    """Tests for find_wire_conflicts — pin of incoming component lands on wire."""

    def test_no_wire_no_conflict(self) -> None:
        defn = _r_defn()
        r1 = _make_inst("R1")
        pl = _placement(r1, 0.5, 0.5)
        sch = Schematic(nets={})
        conflicts = find_wire_conflicts(
            incoming_placement=pl,
            incoming_defn=defn,
            incoming_instance_id=r1.id,
            wires=[],
            schematic=sch,
            point_to_net={},
            already_conflicted=set(),
        )
        assert conflicts == []

    def test_pin_on_wire_unconnected(self) -> None:
        """Wire has no net, pin has no net → PIN_ON_WIRE_UNCONNECTED."""
        defn = _r_defn()
        r1 = _make_inst("R1")
        # SIL(2) rot=0: pin1 at (0.5,0.5), pin2 at (1.5,0.5)
        pl = _placement(r1, 0.5, 0.5, rot=0)
        # Wire passes through pin 1 position (0.5, 0.5)
        wire = _wire([Point(0.5, 0.5), Point(0.5, 2.5)])
        sch = Schematic(nets={})
        conflicts = find_wire_conflicts(
            incoming_placement=pl,
            incoming_defn=defn,
            incoming_instance_id=r1.id,
            wires=[wire],
            schematic=sch,
            point_to_net={},
            already_conflicted=set(),
        )
        assert len(conflicts) == 1
        assert conflicts[0].kind == ConflictKind.PIN_ON_WIRE_UNCONNECTED
        assert conflicts[0].wire_id == wire.id
        assert conflicts[0].incoming_pin == 1

    def test_pin_on_wire_with_net(self) -> None:
        """Wire point has a net, incoming pin has no net → PIN_ON_WIRE_WITH_NET."""
        defn = _r_defn()
        r1 = _make_inst("R1")
        r2 = _make_inst("R2")
        pl_r1 = _placement(r1, 0.5, 0.5, rot=0)  # r1 pin1 at (0.5, 0.5)
        net_a = Net(
            id=uuid.uuid4(),
            name="NET_A",
            pins={PinRef(component_id=r2.id, pin_number=1)},
        )
        sch = Schematic(nets={net_a.id: net_a})
        # Wire starts at (0.5, 2.5) — which has net_a in point_to_net —
        # and passes through r1's pin1 position (0.5, 0.5).
        wire = _wire([Point(0.5, 2.5), Point(0.5, 0.5)])
        point_to_net: dict[tuple[float, float], tuple[uuid.UUID, str]] = {
            (round(0.5, 4), round(2.5, 4)): (net_a.id, net_a.name),
        }
        conflicts = find_wire_conflicts(
            incoming_placement=pl_r1,
            incoming_defn=defn,
            incoming_instance_id=r1.id,
            wires=[wire],
            schematic=sch,
            point_to_net=point_to_net,
            already_conflicted=set(),
        )
        assert len(conflicts) == 1
        assert conflicts[0].kind == ConflictKind.PIN_ON_WIRE_WITH_NET
        assert conflicts[0].existing_net_name == "NET_A"
        assert conflicts[0].incoming_net_id is None

    def test_pin_on_wire_same_net(self) -> None:
        """Wire point and incoming pin are in the same net → auto-ok."""
        defn = _r_defn()
        r1 = _make_inst("R1")
        r2 = _make_inst("R2")
        pl_r1 = _placement(r1, 0.5, 0.5, rot=0)
        net_a = Net(
            id=uuid.uuid4(),
            name="NET_A",
            pins={
                PinRef(component_id=r1.id, pin_number=1),
                PinRef(component_id=r2.id, pin_number=1),
            },
        )
        sch = Schematic(nets={net_a.id: net_a})
        wire = _wire([Point(0.5, 2.5), Point(0.5, 0.5)])
        point_to_net: dict[tuple[float, float], tuple[uuid.UUID, str]] = {
            (round(0.5, 4), round(2.5, 4)): (net_a.id, net_a.name),
        }
        conflicts = find_wire_conflicts(
            incoming_placement=pl_r1,
            incoming_defn=defn,
            incoming_instance_id=r1.id,
            wires=[wire],
            schematic=sch,
            point_to_net=point_to_net,
            already_conflicted=set(),
        )
        assert len(conflicts) == 1
        assert conflicts[0].kind == ConflictKind.PIN_ON_WIRE_SAME_NET

    def test_pin_on_wire_different_net(self) -> None:
        """Wire has net_a, incoming pin has net_b → PIN_ON_WIRE_DIFFERENT_NET."""
        defn = _r_defn()
        r1 = _make_inst("R1")
        r2 = _make_inst("R2")
        pl_r1 = _placement(r1, 0.5, 0.5, rot=0)
        net_a = Net(
            id=uuid.uuid4(),
            name="NET_A",
            pins={PinRef(component_id=r2.id, pin_number=1)},
        )
        net_b = Net(
            id=uuid.uuid4(),
            name="NET_B",
            pins={PinRef(component_id=r1.id, pin_number=1)},
        )
        sch = Schematic(nets={net_a.id: net_a, net_b.id: net_b})
        wire = _wire([Point(0.5, 2.5), Point(0.5, 0.5)])
        point_to_net: dict[tuple[float, float], tuple[uuid.UUID, str]] = {
            (round(0.5, 4), round(2.5, 4)): (net_a.id, net_a.name),
        }
        conflicts = find_wire_conflicts(
            incoming_placement=pl_r1,
            incoming_defn=defn,
            incoming_instance_id=r1.id,
            wires=[wire],
            schematic=sch,
            point_to_net=point_to_net,
            already_conflicted=set(),
        )
        assert len(conflicts) == 1
        assert conflicts[0].kind == ConflictKind.PIN_ON_WIRE_DIFFERENT_NET

    def test_already_conflicted_position_skipped(self) -> None:
        """Position in already_conflicted is not reported again."""
        defn = _r_defn()
        r1 = _make_inst("R1")
        pl = _placement(r1, 0.5, 0.5, rot=0)
        wire = _wire([Point(0.5, 0.5), Point(0.5, 2.5)])
        sch = Schematic(nets={})
        conflicts = find_wire_conflicts(
            incoming_placement=pl,
            incoming_defn=defn,
            incoming_instance_id=r1.id,
            wires=[wire],
            schematic=sch,
            point_to_net={},
            already_conflicted={(round(0.5, 4), round(0.5, 4))},
        )
        assert conflicts == []


# ---------------------------------------------------------------------------
# pad_index= path: verify the PadIndex-backed branch returns equivalent
# results to the legacy from-layout branch for the core scenarios.
# ---------------------------------------------------------------------------


class TestPadIndexPath:
    """Smoke tests for the optional pad_index= parameter — confirm the new
    PadIndex-backed branch matches legacy semantics."""

    def _build_index(
        self,
        board: Board,
        placements: dict,
        instances: dict,
        components: dict,
        schematic: Schematic,
        wires: list | None = None,
    ):
        from protoboard.domain.board.pad_state import PadIndex
        from protoboard.domain.layout.layout import Layout

        lay = Layout()
        lay.ensure_root_group()
        lay.placements.update(placements)
        if wires:
            lay.wires.extend(wires)
        return PadIndex(board, lay, schematic, components, instances)

    def test_find_conflicts_diff_nets_via_pad_index(self) -> None:
        """find_conflicts with pad_index detects pin-on-pin different-net."""
        board = _perfboard(3, 3)
        defn = _r_defn()
        r1 = _make_inst("R1")
        r2 = _make_inst("R2")
        pl_r1 = _placement(r1, 0.5, 0.5)
        pl_r2 = _placement(r2, 0.5, 0.5)
        net_a = Net(id=uuid.uuid4(), name="A",
                    pins={PinRef(component_id=r1.id, pin_number=1)})
        net_b = Net(id=uuid.uuid4(), name="B",
                    pins={PinRef(component_id=r2.id, pin_number=1)})
        sch = Schematic(nets={net_a.id: net_a, net_b.id: net_b})

        idx = self._build_index(
            board, {r1.id: pl_r1}, {r1.id: r1}, {"R": defn}, sch,
        )
        legacy = find_conflicts(
            incoming_placement=pl_r2, incoming_defn=defn,
            incoming_instance_id=r2.id,
            layout_placements={r1.id: pl_r1},
            instances={r1.id: r1}, components={"R": defn}, schematic=sch,
        )
        via_index = find_conflicts(
            incoming_placement=pl_r2, incoming_defn=defn,
            incoming_instance_id=r2.id,
            layout_placements={r1.id: pl_r1},
            instances={r1.id: r1}, components={"R": defn}, schematic=sch,
            pad_index=idx,
        )
        # Both pins of r2 overlap r1's pins; pin1 has DIFFERENT_NETS,
        # pin2 has ONE_UNCONNECTED (only one of the two pins has a net).
        def _summary(cs):
            return sorted((c.incoming_pin, c.kind) for c in cs)
        assert _summary(legacy) == _summary(via_index)
        assert (1, ConflictKind.DIFFERENT_NETS) in _summary(via_index)

    def test_find_conflicts_no_existing_via_pad_index(self) -> None:
        """No existing components → pad_index path returns empty."""
        board = _perfboard(3, 3)
        defn = _r_defn()
        r1 = _make_inst("R1")
        pl = _placement(r1, 0.5, 0.5)
        sch = Schematic(nets={})
        idx = self._build_index(board, {}, {}, {}, sch)
        out = find_conflicts(
            incoming_placement=pl, incoming_defn=defn,
            incoming_instance_id=r1.id,
            layout_placements={}, instances={}, components={}, schematic=sch,
            pad_index=idx,
        )
        assert out == []

    def test_find_wire_conflicts_via_pad_index(self) -> None:
        """find_wire_conflicts with pad_index detects pin landing on wire.

        Two pins of R1 (SIL(2) rot=0 at (0.5, 0.5)) sit on the wire:
          * pin 1 (0.5, 0.5) — wire endpoint (BareWireItem)
          * pin 2 (0.5, 1.5) — wire mid-segment (BareWireCrossing)
        Both must surface as conflicts; the historic pre-2026-05-31
        implementation missed the crossing case (only vertex items
        were inspected), letting the user silently drop a multi-pin
        connector across an existing bare wire.
        """
        board = _perfboard(3, 3)
        defn = _r_defn()
        r1 = _make_inst("R1")
        pl = _placement(r1, 0.5, 0.5, rot=0)
        # Bare wire so PadIndex records it as a bare wire item (top).
        wire = Wire(id=uuid.uuid4(),
                           path=[Point(0.5, 0.5), Point(0.5, 2.5)],
                           insulated=False, side="top")
        sch = Schematic(nets={})
        idx = self._build_index(
            board, {}, {}, {}, sch, wires=[wire],
        )
        out = find_wire_conflicts(
            incoming_placement=pl, incoming_defn=defn,
            incoming_instance_id=r1.id,
            wires=[wire], schematic=sch,
            point_to_net={}, already_conflicted=set(),
            pad_index=idx,
        )
        assert len(out) == 2
        assert {c.incoming_pin for c in out} == {1, 2}
        assert all(c.wire_id == wire.id for c in out)
        assert all(c.kind == ConflictKind.PIN_ON_WIRE_UNCONNECTED for c in out)

    def test_find_wires_crossing_pads_via_pad_index(self) -> None:
        """find_wires_crossing_pads with pad_index detects bare wire crossing
        an alien-net pin."""
        board = _perfboard(5, 3)
        defn = _r_defn()
        r1 = _make_inst("R1")
        r2 = _make_inst("R2")
        # R1 at pin1=(0.5,0.5), pin2=(1.5,0.5); R2 at (2.5,0.5),(3.5,0.5).
        # Both pins on different nets.
        pl_r1 = _placement(r1, 0.5, 0.5, rot=0)
        pl_r2 = _placement(r2, 2.5, 0.5, rot=0)
        net_a = Net(id=uuid.uuid4(), name="A",
                    pins={PinRef(component_id=r1.id, pin_number=1)})
        net_b = Net(id=uuid.uuid4(), name="B",
                    pins={PinRef(component_id=r2.id, pin_number=1)})
        sch = Schematic(nets={net_a.id: net_a, net_b.id: net_b})
        # Bare wire goes from net_a pin (0.5,0.5) straight through (2.5,0.5)
        # which is on net_b — crossing alien pad → conflict.
        from protoboard.domain.layout.pad_conflict import find_wires_crossing_pads
        wire = Wire(id=uuid.uuid4(),
                           path=[Point(0.5, 0.5), Point(2.5, 0.5)],
                           insulated=False, side="top")
        idx = self._build_index(
            board,
            {r1.id: pl_r1, r2.id: pl_r2},
            {r1.id: r1, r2.id: r2},
            {"R": defn},
            sch,
            wires=[wire],
        )
        out = find_wires_crossing_pads(
            wires=[wire], moved_comp_ids=set(),
            layout_placements={r1.id: pl_r1, r2.id: pl_r2},
            instances={r1.id: r1, r2.id: r2},
            components={"R": defn},
            schematic=sch,
            pad_index=idx,
        )
        # Legacy path returns the same wire id.
        legacy = find_wires_crossing_pads(
            wires=[wire], moved_comp_ids=set(),
            layout_placements={r1.id: pl_r1, r2.id: pl_r2},
            instances={r1.id: r1, r2.id: r2},
            components={"R": defn},
            schematic=sch,
        )
        assert out == legacy
        assert wire.id in out

    def test_multi_pin_connector_dropped_on_foreign_net_wire(self) -> None:
        """User regression 2026-05-31: a 3-pin connector dragged onto a
        foreign-net bare wire must report a conflict on every pin,
        even when the connector's pins land on *mid-segment* pads of
        the wire (no path vertex coincides with the pin).

        Before the fix, ``_wire_hit_via_pad_index`` filtered out
        BareWireCrossing items, so a connector pin landing on the
        interior of an existing wire silently passed validation and
        the wire was left in place after the drag commit — a hard
        short with the connector's net.

        Hard rule: NO foreign wire may sit on a pin's pad
        (semantics rule 6).
        """
        from protoboard.domain.components.footprints import SIL

        board = _perfboard(7, 3)
        # 3-pin SIL connector at (1.5..3.5, 0.5) on top.
        conn_defn = ComponentDefinition(
            name="J", footprint=SIL(3), pin_names={1: "A", 2: "B", 3: "C"},
        )
        conn = ComponentInstance.new(definition_ref="J", refdes="J1", value="")
        pl_conn = _placement(conn, 1.5, 0.5, rot=0)  # pins (1.5,0.5),(1.5,1.5),(1.5,2.5)
        # Foreign-net bare wire spanning (1.5, 0.5)→(1.5, 2.5) — every
        # connector pin lands on it (endpoint, middle vertex,
        # endpoint).  Wire belongs to NET_FOREIGN via an anchor pin
        # off to the side.
        anchor = ComponentInstance.new(definition_ref="J", refdes="ANCHOR", value="")
        pl_anchor = _placement(anchor, 5.5, 0.5, rot=0)
        net_foreign = Net(
            id=uuid.uuid4(), name="FOREIGN",
            pins={PinRef(component_id=anchor.id, pin_number=1)},
        )
        sch = Schematic(nets={net_foreign.id: net_foreign})
        wire = Wire(id=uuid.uuid4(),
                    path=[Point(1.5, 0.5), Point(1.5, 2.5)],
                    insulated=False, side="top")
        idx = self._build_index(
            board, {anchor.id: pl_anchor}, {anchor.id: anchor},
            {"J": conn_defn}, sch, wires=[wire],
        )
        # point_to_net for anchor's pin so the wire resolves to FOREIGN.
        # Coords are pre-rounded (matching find_wire_conflicts internals).
        point_to_net: dict[tuple[float, float], tuple[uuid.UUID, str]] = {
            (5.5, 0.5): (net_foreign.id, "FOREIGN"),
        }
        # New: also include the wire's start endpoint as carrying the
        # net so find_wire_conflicts's legacy net-resolution path
        # (used in the schematic-less leg) sees it.  PadIndex path
        # already knows from electrical_net_on().
        out = find_wire_conflicts(
            incoming_placement=pl_conn, incoming_defn=conn_defn,
            incoming_instance_id=conn.id,
            wires=[wire], schematic=sch,
            point_to_net=point_to_net, already_conflicted=set(),
            pad_index=idx,
        )
        # All three connector pins lie on the wire — every one must
        # be reported as a conflict.
        assert len(out) == 3, (
            f"expected 3 conflicts, got {len(out)}: "
            f"{[(c.incoming_pin, c.kind) for c in out]}"
        )
        assert {c.incoming_pin for c in out} == {1, 2, 3}
