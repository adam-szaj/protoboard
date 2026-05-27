# SPDX-License-Identifier: MIT
"""Tests for DRCEngine — verifies SHORT, MISSING_NET, OVERLAP checks."""

import uuid

import pytest

from protoboard.domain.board.board import Board
from protoboard.domain.board.boxes import BoardGrid, PadAtom, SizePolicy
from protoboard.domain.board.pads import PadLayers
from protoboard.domain.components.definition import ComponentDefinition
from protoboard.domain.components.footprints import SIL
from protoboard.domain.components.instance import ComponentInstance
from protoboard.domain.layout.layout import Layout
from protoboard.domain.layout.placement import ComponentPlacement
from protoboard.domain.layout.wire import Wire
from protoboard.domain.schematic.net import Net
from protoboard.domain.schematic.schematic import PinRef, Schematic
from protoboard.geometry import Point
from protoboard.services.drc.engine import DRCEngine, DRCIssue


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


class TestDRCClean:
    def test_empty_layout_no_issues(self) -> None:
        board = _perfboard(3, 3)
        sch = Schematic(nets={})
        lay = Layout(placements={}, wires=[])
        engine = DRCEngine()
        issues = engine.check(
            board=board,
            schematic=sch,
            layout=lay,
            components={},
            instances={},
        )
        assert issues == []

    def test_voltage_divider_clean(self) -> None:
        # Two resistors on a 1×5 perfboard, one VCC net and one GND net plus
        # a mid-point. All pins assigned → DRC clean.
        board = _perfboard(1, 5)
        defn = _r_defn()
        r1 = ComponentInstance.new(definition_ref="R", refdes="R1", value="10k")
        r2 = ComponentInstance.new(definition_ref="R", refdes="R2", value="10k")

        # Pad centers at x=0.5,1.5,2.5,3.5,4.5, y=0.5 (1-row board).
        # SIL(2) rotation=90: pin2 rel=(0,1) rotates to (1,0) → world at (px+1, py).
        # R1: pin1@(0.5,0.5), pin2@(1.5,0.5). R2: pin1@(2.5,0.5), pin2@(3.5,0.5).
        pl_r1 = ComponentPlacement(
            component_id=r1.id, position=Point(0.5, 0.5), rotation=90, side="top"
        )
        pl_r2 = ComponentPlacement(
            component_id=r2.id, position=Point(2.5, 0.5), rotation=90, side="top"
        )
        # Wire: pin2(R1)=(1.5,0.5) to pin1(R2)=(2.5,0.5)
        mid_wire = Wire(
            id=uuid.uuid4(),
            path=[Point(1.5, 0.5), Point(2.5, 0.5)],
            insulated=False,
            side="top",
        )

        vcc_net = Net(
            id=uuid.uuid4(),
            name="VCC",
            pins={PinRef(component_id=r1.id, pin_number=1)},
        )
        mid_net = Net(
            id=uuid.uuid4(),
            name="MID",
            pins={
                PinRef(component_id=r1.id, pin_number=2),
                PinRef(component_id=r2.id, pin_number=1),
            },
        )
        gnd_net = Net(
            id=uuid.uuid4(),
            name="GND",
            pins={PinRef(component_id=r2.id, pin_number=2)},
        )

        sch = Schematic(nets={n.id: n for n in (vcc_net, mid_net, gnd_net)})
        lay = Layout(
            placements={r1.id: pl_r1, r2.id: pl_r2},
            wires=[mid_wire],
        )
        engine = DRCEngine()
        issues = engine.check(
            board=board,
            schematic=sch,
            layout=lay,
            components={defn.name: defn},
            instances={r1.id: r1, r2.id: r2},
        )
        assert issues == []


class TestDRCMissingNet:
    def test_missing_connection_detected(self) -> None:
        board = _perfboard(1, 4)
        defn = _r_defn()
        r1 = ComponentInstance.new(definition_ref="R", refdes="R1", value="10k")
        r2 = ComponentInstance.new(definition_ref="R", refdes="R2", value="10k")
        # Pad centers at 0.5, 1.5, 2.5, 3.5
        pl_r1 = ComponentPlacement(
            component_id=r1.id, position=Point(0.5, 0.5), rotation=90, side="top"
        )
        pl_r2 = ComponentPlacement(
            component_id=r2.id, position=Point(2.5, 0.5), rotation=90, side="top"
        )
        # Net declares R1.pin2 and R2.pin1 should be connected, but no wire.
        net = Net(
            id=uuid.uuid4(),
            name="MID",
            pins={
                PinRef(component_id=r1.id, pin_number=2),
                PinRef(component_id=r2.id, pin_number=1),
            },
        )
        sch = Schematic(nets={net.id: net})
        lay = Layout(
            placements={r1.id: pl_r1, r2.id: pl_r2},
            wires=[],
        )
        engine = DRCEngine()
        issues = engine.check(
            board=board,
            schematic=sch,
            layout=lay,
            components={defn.name: defn},
            instances={r1.id: r1, r2.id: r2},
        )
        missing = [i for i in issues if i.code == "MISSING_NET"]
        assert len(missing) >= 1
        assert all(i.severity == "error" for i in missing)


class TestDRCShort:
    def test_short_detected(self) -> None:
        # Two separate nets, but a wire accidentally connects both.
        board = _perfboard(1, 4)
        defn = _r_defn()
        r1 = ComponentInstance.new(definition_ref="R", refdes="R1", value="10k")
        r2 = ComponentInstance.new(definition_ref="R", refdes="R2", value="10k")
        # Pad centers at 0.5, 1.5, 2.5, 3.5
        pl_r1 = ComponentPlacement(
            component_id=r1.id, position=Point(0.5, 0.5), rotation=90, side="top"
        )
        pl_r2 = ComponentPlacement(
            component_id=r2.id, position=Point(2.5, 0.5), rotation=90, side="top"
        )
        # Wire spans all 4 pad centers (shorts both nets).
        bad_wire = Wire(
            id=uuid.uuid4(),
            path=[Point(0.5, 0.5), Point(3.5, 0.5)],
            insulated=False,
            side="top",
        )
        net_a = Net(
            id=uuid.uuid4(),
            name="A",
            pins={PinRef(component_id=r1.id, pin_number=1)},
        )
        net_b = Net(
            id=uuid.uuid4(),
            name="B",
            pins={PinRef(component_id=r2.id, pin_number=2)},
        )
        sch = Schematic(nets={n.id: n for n in (net_a, net_b)})
        lay = Layout(
            placements={r1.id: pl_r1, r2.id: pl_r2},
            wires=[bad_wire],
        )
        engine = DRCEngine()
        issues = engine.check(
            board=board,
            schematic=sch,
            layout=lay,
            components={defn.name: defn},
            instances={r1.id: r1, r2.id: r2},
        )
        shorts = [i for i in issues if i.code == "SHORT"]
        assert len(shorts) >= 1
        assert all(i.severity == "error" for i in shorts)


class TestDRCOverlap:
    def test_overlap_detected(self) -> None:
        # Two components placed at the same position → pin overlap.
        board = _perfboard(1, 3)
        defn = _r_defn()
        r1 = ComponentInstance.new(definition_ref="R", refdes="R1", value="10k")
        r2 = ComponentInstance.new(definition_ref="R", refdes="R2", value="10k")
        # Both at (0,0) → same pads.
        pl_r1 = ComponentPlacement(
            component_id=r1.id, position=Point(0.0, 0.0), rotation=0, side="top"
        )
        pl_r2 = ComponentPlacement(
            component_id=r2.id, position=Point(0.0, 0.0), rotation=0, side="top"
        )
        sch = Schematic(nets={})
        lay = Layout(
            placements={r1.id: pl_r1, r2.id: pl_r2},
            wires=[],
        )
        engine = DRCEngine()
        issues = engine.check(
            board=board,
            schematic=sch,
            layout=lay,
            components={defn.name: defn},
            instances={r1.id: r1, r2.id: r2},
        )
        overlaps = [i for i in issues if i.code == "OVERLAP"]
        assert len(overlaps) >= 1
        assert all(i.severity == "error" for i in overlaps)

    def test_overlap_skipped_when_all_pins_bendable(self) -> None:
        from protoboard.domain.components.definition import PinBendSpec

        board = _perfboard(1, 3)
        # Two component definitions where both pin 1 and pin 2 are bendable.
        bend_spec = PinBendSpec(default_offset=Point(0.0, 0.0))
        defn = ComponentDefinition(
            name="R_bend",
            footprint=SIL(2),
            pin_names={1: "A", 2: "B"},
            bendable_pins={1: bend_spec, 2: bend_spec},
        )
        r1 = ComponentInstance.new(definition_ref="R_bend", refdes="R1", value="10k")
        r2 = ComponentInstance.new(definition_ref="R_bend", refdes="R2", value="10k")
        pl_r1 = ComponentPlacement(
            component_id=r1.id, position=Point(0.0, 0.0), rotation=0, side="top",
        )
        pl_r2 = ComponentPlacement(
            component_id=r2.id, position=Point(0.0, 0.0), rotation=0, side="top",
        )
        engine = DRCEngine()
        issues = engine.check(
            board=board,
            schematic=Schematic(nets={}),
            layout=Layout(placements={r1.id: pl_r1, r2.id: pl_r2}, wires=[]),
            components={defn.name: defn},
            instances={r1.id: r1, r2.id: r2},
        )
        assert [i for i in issues if i.code == "OVERLAP"] == []

    def test_overlap_still_flags_when_partial_bendable(self) -> None:
        from protoboard.domain.components.definition import PinBendSpec

        board = _perfboard(1, 3)
        bend_spec = PinBendSpec(default_offset=Point(0.0, 0.0))
        # Only one of the two definitions declares bendable pins.
        bendy = ComponentDefinition(
            name="R_bend",
            footprint=SIL(2),
            pin_names={1: "A", 2: "B"},
            bendable_pins={1: bend_spec, 2: bend_spec},
        )
        rigid = _r_defn()
        r1 = ComponentInstance.new(definition_ref="R_bend", refdes="R1", value="10k")
        r2 = ComponentInstance.new(definition_ref="R", refdes="R2", value="10k")
        pl_r1 = ComponentPlacement(
            component_id=r1.id, position=Point(0.0, 0.0), rotation=0, side="top",
        )
        pl_r2 = ComponentPlacement(
            component_id=r2.id, position=Point(0.0, 0.0), rotation=0, side="top",
        )
        engine = DRCEngine()
        issues = engine.check(
            board=board,
            schematic=Schematic(nets={}),
            layout=Layout(placements={r1.id: pl_r1, r2.id: pl_r2}, wires=[]),
            components={bendy.name: bendy, rigid.name: rigid},
            instances={r1.id: r1, r2.id: r2},
        )
        # Partial bendability = still a real overlap.
        assert [i for i in issues if i.code == "OVERLAP"] != []

    def test_overlap_populates_affected_components_and_pins(self) -> None:
        board = _perfboard(1, 3)
        defn = _r_defn()
        r1 = ComponentInstance.new(definition_ref="R", refdes="R1", value="10k")
        r2 = ComponentInstance.new(definition_ref="R", refdes="R2", value="10k")
        pl_r1 = ComponentPlacement(
            component_id=r1.id, position=Point(0.0, 0.0), rotation=0, side="top",
        )
        pl_r2 = ComponentPlacement(
            component_id=r2.id, position=Point(0.0, 0.0), rotation=0, side="top",
        )
        engine = DRCEngine()
        issues = engine.check(
            board=board,
            schematic=Schematic(nets={}),
            layout=Layout(placements={r1.id: pl_r1, r2.id: pl_r2}, wires=[]),
            components={defn.name: defn},
            instances={r1.id: r1, r2.id: r2},
        )
        overlap = next(i for i in issues if i.code == "OVERLAP")
        assert set(overlap.affected_components) == {r1.id, r2.id}
        assert len(overlap.affected_pins) >= 2
        # Refdes shows in message instead of UUIDs.
        assert "R1" in overlap.message
        assert "R2" in overlap.message


class TestDRCMessages:
    """Human-readable formatting of issue messages."""

    def test_axis_labels_used_when_enabled(self) -> None:
        from protoboard.domain.axis_labels_settings import (
            AxisLabelFormat, AxisLabelSettings,
        )

        board = _perfboard(3, 3)
        defn = _r_defn()
        r1 = ComponentInstance.new(definition_ref="R", refdes="R1", value="10k")
        r2 = ComponentInstance.new(definition_ref="R", refdes="R2", value="10k")
        pl_r1 = ComponentPlacement(
            component_id=r1.id, position=Point(0.0, 0.0), rotation=0, side="top",
        )
        pl_r2 = ComponentPlacement(
            component_id=r2.id, position=Point(0.0, 0.0), rotation=0, side="top",
        )
        engine = DRCEngine()
        labels = AxisLabelSettings(
            columns=AxisLabelFormat.NUMERIC_1,
            rows=AxisLabelFormat.ALPHABETIC,
        )
        issues = engine.check(
            board=board,
            schematic=Schematic(nets={}),
            layout=Layout(placements={r1.id: pl_r1, r2.id: pl_r2}, wires=[]),
            components={defn.name: defn},
            instances={r1.id: r1, r2.id: r2},
            axis_labels=labels,
        )
        overlap = next(i for i in issues if i.code == "OVERLAP")
        # Pin 1 of both components lands at (0.5, 0.5) → col index 0, row index 0
        # → "1" (NUMERIC_1) and "a" (ALPHABETIC) → compact "1a".
        assert "1a" in overlap.message

    def test_axis_labels_fallback_to_col_row(self) -> None:
        board = _perfboard(3, 3)
        defn = _r_defn()
        r1 = ComponentInstance.new(definition_ref="R", refdes="R1", value="10k")
        r2 = ComponentInstance.new(definition_ref="R", refdes="R2", value="10k")
        pl_r1 = ComponentPlacement(
            component_id=r1.id, position=Point(0.0, 0.0), rotation=0, side="top",
        )
        pl_r2 = ComponentPlacement(
            component_id=r2.id, position=Point(0.0, 0.0), rotation=0, side="top",
        )
        engine = DRCEngine()
        issues = engine.check(
            board=board,
            schematic=Schematic(nets={}),
            layout=Layout(placements={r1.id: pl_r1, r2.id: pl_r2}, wires=[]),
            components={defn.name: defn},
            instances={r1.id: r1, r2.id: r2},
            # axis_labels=None → fallback
        )
        overlap = next(i for i in issues if i.code == "OVERLAP")
        assert "col 0" in overlap.message
        assert "row 0" in overlap.message


class TestDRCIssue:
    def test_issue_has_required_fields(self) -> None:
        issue = DRCIssue(
            severity="error",
            code="SHORT",
            message="Short between VCC and GND",
            locations=[Point(0.0, 0.0)],
            affected_nets=[uuid.uuid4()],
        )
        assert issue.severity == "error"
        assert issue.code == "SHORT"
        assert len(issue.locations) == 1
        assert len(issue.affected_nets) == 1

    def test_invalid_severity_raises(self) -> None:
        with pytest.raises(ValueError):
            DRCIssue(
                severity="critical",  # type: ignore[arg-type]
                code="SHORT",
                message="msg",
                locations=[],
                affected_nets=[],
            )


class TestDRCFloatingPin:
    """FLOATING_PIN: a pin assigned to a net but not placed on the board."""

    def _setup(self) -> tuple[Board, ComponentDefinition, ComponentInstance]:
        board = _perfboard(1, 3)
        defn = _r_defn()
        inst = ComponentInstance.new(definition_ref="R", refdes="R1", value="10k")
        return board, defn, inst

    def test_floating_pin_when_component_not_placed(self) -> None:
        board, defn, inst = self._setup()
        net = Net(
            id=uuid.uuid4(),
            name="VCC",
            pins={PinRef(component_id=inst.id, pin_number=1)},
        )
        sch = Schematic(nets={net.id: net})
        # Component exists in instances but NOT in placements.
        lay = Layout(placements={}, wires=[])
        engine = DRCEngine()
        issues = engine.check(
            board=board,
            schematic=sch,
            layout=lay,
            components={defn.name: defn},
            instances={inst.id: inst},
        )
        floating = [i for i in issues if i.code == "FLOATING_PIN"]
        assert len(floating) >= 1
        assert all(i.severity == "warning" for i in floating)

    def test_no_floating_pin_when_placed(self) -> None:
        board, defn, inst = self._setup()
        net = Net(
            id=uuid.uuid4(),
            name="VCC",
            pins={PinRef(component_id=inst.id, pin_number=1)},
        )
        sch = Schematic(nets={net.id: net})
        pl = ComponentPlacement(
            component_id=inst.id, position=Point(0.5, 0.5), rotation=90, side="top"
        )
        lay = Layout(placements={inst.id: pl}, wires=[])
        engine = DRCEngine()
        issues = engine.check(
            board=board,
            schematic=sch,
            layout=lay,
            components={defn.name: defn},
            instances={inst.id: inst},
        )
        floating = [i for i in issues if i.code == "FLOATING_PIN"]
        assert floating == []


class TestDRCBareWireCross:
    """BARE_WIRE_CROSS: two bare (non-insulated) wires from different nets
    share a pad (physical crossing point)."""

    def test_crossing_bare_wires_detected(self) -> None:
        # 3×3 board. Wire A goes (0.5,1.5)→(2.5,1.5) horizontally (bare).
        # Wire B goes (1.5,0.5)→(1.5,2.5) vertically (bare).
        # They share pad (1.5,1.5) → BARE_WIRE_CROSS.
        board = _perfboard(3, 3)
        defn = _r_defn()
        r1 = ComponentInstance.new(definition_ref="R", refdes="R1", value="1k")
        r2 = ComponentInstance.new(definition_ref="R", refdes="R2", value="1k")
        pl_r1 = ComponentPlacement(
            component_id=r1.id, position=Point(0.5, 1.5), rotation=90, side="top"
        )
        pl_r2 = ComponentPlacement(
            component_id=r2.id, position=Point(1.5, 0.5), rotation=0, side="top"
        )
        net_a = Net(
            id=uuid.uuid4(),
            name="NET_A",
            pins={
                PinRef(component_id=r1.id, pin_number=1),
                PinRef(component_id=r1.id, pin_number=2),
            },
        )
        net_b = Net(
            id=uuid.uuid4(),
            name="NET_B",
            pins={
                PinRef(component_id=r2.id, pin_number=1),
                PinRef(component_id=r2.id, pin_number=2),
            },
        )
        wire_a = Wire(
            id=uuid.uuid4(),
            path=[Point(0.5, 1.5), Point(1.5, 1.5), Point(2.5, 1.5)],
            insulated=False,
            side="top",
        )
        wire_b = Wire(
            id=uuid.uuid4(),
            path=[Point(1.5, 0.5), Point(1.5, 1.5), Point(1.5, 2.5)],
            insulated=False,
            side="top",
        )
        sch = Schematic(nets={net_a.id: net_a, net_b.id: net_b})
        lay = Layout(
            placements={r1.id: pl_r1, r2.id: pl_r2},
            wires=[wire_a, wire_b],
        )
        engine = DRCEngine()
        issues = engine.check(
            board=board,
            schematic=sch,
            layout=lay,
            components={defn.name: defn},
            instances={r1.id: r1, r2.id: r2},
        )
        cross = [i for i in issues if i.code == "BARE_WIRE_CROSS"]
        assert len(cross) >= 1
        assert all(i.severity == "error" for i in cross)

    def test_insulated_crossing_no_issue(self) -> None:
        # Same geometry but both wires insulated → no BARE_WIRE_CROSS.
        board = _perfboard(3, 3)
        wire_a = Wire(
            id=uuid.uuid4(),
            path=[Point(0.5, 1.5), Point(1.5, 1.5), Point(2.5, 1.5)],
            insulated=True,
            side="top",
        )
        wire_b = Wire(
            id=uuid.uuid4(),
            path=[Point(1.5, 0.5), Point(1.5, 1.5), Point(1.5, 2.5)],
            insulated=True,
            side="top",
        )
        sch = Schematic(nets={})
        lay = Layout(placements={}, wires=[wire_a, wire_b])
        engine = DRCEngine()
        issues = engine.check(
            board=board, schematic=sch, layout=lay, components={}, instances={}
        )
        cross = [i for i in issues if i.code == "BARE_WIRE_CROSS"]
        assert cross == []


class TestDRCBareWireInteriorCross:
    """BARE_WIRE_INTERIOR_CROSS: bare wires of different electric sets on the
    same side crossing at an off-pad point (centre of a mesh cell)."""

    def test_diagonal_bare_wires_cross_between_pads(self) -> None:
        # 4x4 board. Wire A: (0.5,0.5)->(2.5,2.5) diagonal bare.
        # Wire B: (0.5,2.5)->(2.5,0.5) diagonal bare. They cross at (1.5,1.5)
        # but neither path includes that point as a vertex — the crossing is
        # purely interior (well, the centre coincides with a pad here; use
        # endpoints that miss any pad to make the interior nature explicit).
        # Use (0.5,1.5)->(2.5,1.5) and (1.5,0.5)->(1.5,2.5) which share pad
        # (1.5,1.5) — that case is the existing BARE_WIRE_CROSS.  For interior
        # we need a crossing AT a non-pad point.  Use diagonal segments that
        # cross at integer (between-pads) coordinates.
        board = _perfboard(4, 4)
        # Diagonal A: (0.5,0.5)→(2.5,2.5).  Diagonal B: (2.5,0.5)→(0.5,2.5).
        # They cross at (1.5, 1.5) which IS a pad — also a vertex of neither
        # path.  Switch to (0.5,0.5)→(1.5,2.5) and (0.5,2.5)→(1.5,0.5): they
        # cross at (1.0, 1.5) — between pads on the y=1.5 row.
        wire_a = Wire(
            id=uuid.uuid4(),
            path=[Point(0.5, 0.5), Point(1.5, 2.5)],
            insulated=False,
            side="top",
        )
        wire_b = Wire(
            id=uuid.uuid4(),
            path=[Point(0.5, 2.5), Point(1.5, 0.5)],
            insulated=False,
            side="top",
        )
        sch = Schematic(nets={})
        lay = Layout(placements={}, wires=[wire_a, wire_b])
        engine = DRCEngine()
        issues = engine.check(
            board=board, schematic=sch, layout=lay, components={}, instances={}
        )
        interior = [i for i in issues if i.code == "BARE_WIRE_INTERIOR_CROSS"]
        assert len(interior) == 1
        assert interior[0].severity == "error"
        # Crossing point ~ (1.0, 1.5).
        assert abs(interior[0].locations[0].x - 1.0) < 1e-6
        assert abs(interior[0].locations[0].y - 1.5) < 1e-6

    def test_different_sides_no_interior_cross(self) -> None:
        # Same diagonal geometry but wires on top + bottom → no short.
        board = _perfboard(4, 4)
        wire_a = Wire(
            id=uuid.uuid4(),
            path=[Point(0.5, 0.5), Point(1.5, 2.5)],
            insulated=False,
            side="top",
        )
        wire_b = Wire(
            id=uuid.uuid4(),
            path=[Point(0.5, 2.5), Point(1.5, 0.5)],
            insulated=False,
            side="bottom",
        )
        sch = Schematic(nets={})
        lay = Layout(placements={}, wires=[wire_a, wire_b])
        engine = DRCEngine()
        issues = engine.check(
            board=board, schematic=sch, layout=lay, components={}, instances={}
        )
        interior = [i for i in issues if i.code == "BARE_WIRE_INTERIOR_CROSS"]
        assert interior == []

    def test_insulated_interior_cross_no_issue(self) -> None:
        # Same geometry with at least one insulated wire — no short.
        board = _perfboard(4, 4)
        wire_a = Wire(
            id=uuid.uuid4(),
            path=[Point(0.5, 0.5), Point(1.5, 2.5)],
            insulated=True,
            side="top",
        )
        wire_b = Wire(
            id=uuid.uuid4(),
            path=[Point(0.5, 2.5), Point(1.5, 0.5)],
            insulated=False,
            side="top",
        )
        sch = Schematic(nets={})
        lay = Layout(placements={}, wires=[wire_a, wire_b])
        engine = DRCEngine()
        issues = engine.check(
            board=board, schematic=sch, layout=lay, components={}, instances={}
        )
        interior = [i for i in issues if i.code == "BARE_WIRE_INTERIOR_CROSS"]
        assert interior == []
