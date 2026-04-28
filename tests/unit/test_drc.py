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
from protoboard.domain.layout.wire import WireSegment
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
        lay = Layout(placements={}, wires=[], cuts=[])
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
        mid_wire = WireSegment(
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
            cuts=[],
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
            cuts=[],
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
        bad_wire = WireSegment(
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
            cuts=[],
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
            cuts=[],
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
