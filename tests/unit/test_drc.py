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
from protoboard.domain.layout.wire import Wire, WireCollection
from protoboard.domain.schematic.net import Net
from protoboard.domain.schematic.schematic import PinRef, Schematic
from protoboard.geometry import Point, Vector
from protoboard.services.drc.engine import DRCEngine, DRCIssue
from tests.instance_utils import bind


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
    return ComponentDefinition.from_footprint(name="R", footprint=SIL(2), pin_names={1: "A", 2: "B"})


class TestDRCClean:
    def test_empty_layout_no_issues(self) -> None:
        board = _perfboard(3, 3)
        sch = Schematic(nets={})
        lay = Layout(placements={}, wires=WireCollection())
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
        r1 = bind(ComponentInstance.new(definition_ref="R", refdes="R1", value="10k"), defn, {})
        r2 = bind(ComponentInstance.new(definition_ref="R", refdes="R2", value="10k"), defn, {})

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
            wires=WireCollection([mid_wire]),
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
        r1 = bind(ComponentInstance.new(definition_ref="R", refdes="R1", value="10k"), defn, {})
        r2 = bind(ComponentInstance.new(definition_ref="R", refdes="R2", value="10k"), defn, {})
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
            wires=WireCollection(),
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
        r1 = bind(ComponentInstance.new(definition_ref="R", refdes="R1", value="10k"), defn, {})
        r2 = bind(ComponentInstance.new(definition_ref="R", refdes="R2", value="10k"), defn, {})
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
            wires=WireCollection([bad_wire]),
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
        r1 = bind(ComponentInstance.new(definition_ref="R", refdes="R1", value="10k"), defn, {})
        r2 = bind(ComponentInstance.new(definition_ref="R", refdes="R2", value="10k"), defn, {})
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
            wires=WireCollection(),
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
        bend_spec = PinBendSpec(default_offset=Vector(0.0, 0.0))
        defn = ComponentDefinition.from_footprint(
            name="R_bend",
            footprint=SIL(2),
            pin_names={1: "A", 2: "B"},
            bendable_pins={1: bend_spec, 2: bend_spec},
        )
        r1 = bind(ComponentInstance.new(definition_ref="R_bend", refdes="R1", value="10k"), defn, {})
        r2 = bind(ComponentInstance.new(definition_ref="R_bend", refdes="R2", value="10k"), defn, {})
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
            layout=Layout(placements={r1.id: pl_r1, r2.id: pl_r2}, wires=WireCollection()),
            components={defn.name: defn},
            instances={r1.id: r1, r2.id: r2},
        )
        assert [i for i in issues if i.code == "OVERLAP"] == []

    def test_overlap_skipped_when_package_pins_bendable(self) -> None:
        # DRC queries defn.package via is_pin_bendable. The component ORIGIN is
        # fixed at [0,0]; EVERY pin (incl. pin 1) may bend within the ring, so a
        # bendable package makes all pins bendable — fully-coincident placement
        # is intentional sharing → no OVERLAP (mirrors the legacy bendable test).
        from protoboard.domain.components.package import (
            BOARD_DEFAULT,
            Cylinder,
            Leads,
            is_pin_bendable,
        )

        leads = Leads(
            direction=((0, 0, -1), (0, 0, -1)),
            body_offset_mm=(Vector(2.5, 0.0),),
            board_offset=BOARD_DEFAULT,
        )
        pkg = Cylinder(diameter_mm=6.3, length_mm=11.0, leads=leads, bend_holes=4.0)
        # Origin is fixed but bending applies to every pin, pin 1 included.
        assert is_pin_bendable(pkg, 1) is True
        assert is_pin_bendable(pkg, 2) is True

        board = _perfboard(1, 3)
        defn = ComponentDefinition(
            name="C_pkg", pin_names={1: "+", 2: "-"}, package=pkg,
        )
        c1 = bind(ComponentInstance.new(definition_ref="C_pkg", refdes="C1", value="10u"), defn, {})
        c2 = bind(ComponentInstance.new(definition_ref="C_pkg", refdes="C2", value="10u"), defn, {})
        pl = lambda cid: ComponentPlacement(  # noqa: E731
            component_id=cid, position=Point(0.0, 0.0), rotation=0, side="top",
        )
        engine = DRCEngine()
        issues = engine.check(
            board=board,
            schematic=Schematic(nets={}),
            layout=Layout(placements={c1.id: pl(c1.id), c2.id: pl(c2.id)}, wires=WireCollection()),
            components={defn.name: defn},
            instances={c1.id: c1, c2.id: c2},
        )
        # All shared pins bendable via package → overlap is intentional, skipped.
        assert [i for i in issues if i.code == "OVERLAP"] == []

    def test_overlap_still_flags_when_partial_bendable(self) -> None:
        from protoboard.domain.components.definition import PinBendSpec

        board = _perfboard(1, 3)
        bend_spec = PinBendSpec(default_offset=Vector(0.0, 0.0))
        # Only one of the two definitions declares bendable pins.
        bendy = ComponentDefinition.from_footprint(
            name="R_bend",
            footprint=SIL(2),
            pin_names={1: "A", 2: "B"},
            bendable_pins={1: bend_spec, 2: bend_spec},
        )
        rigid = _r_defn()
        r1 = bind(ComponentInstance.new(definition_ref="R_bend", refdes="R1", value="10k"), bendy, {})
        r2 = bind(ComponentInstance.new(definition_ref="R", refdes="R2", value="10k"), rigid, {})
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
            layout=Layout(placements={r1.id: pl_r1, r2.id: pl_r2}, wires=WireCollection()),
            components={bendy.name: bendy, rigid.name: rigid},
            instances={r1.id: r1, r2.id: r2},
        )
        # Partial bendability = still a real overlap.
        assert [i for i in issues if i.code == "OVERLAP"] != []

    def test_overlap_populates_affected_components_and_pins(self) -> None:
        board = _perfboard(1, 3)
        defn = _r_defn()
        r1 = bind(ComponentInstance.new(definition_ref="R", refdes="R1", value="10k"), defn, {})
        r2 = bind(ComponentInstance.new(definition_ref="R", refdes="R2", value="10k"), defn, {})
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
            layout=Layout(placements={r1.id: pl_r1, r2.id: pl_r2}, wires=WireCollection()),
            components={defn.name: defn},
            instances={r1.id: r1, r2.id: r2},
        )
        overlap = next(i for i in issues if i.code == "OVERLAP")
        assert set(overlap.affected_components) == {r1.id, r2.id}
        assert len(overlap.affected_pins) >= 2
        # Refdes shows in message instead of UUIDs.
        assert "R1" in overlap.message
        assert "R2" in overlap.message


def _prism_defn() -> ComponentDefinition:
    """A 4-pin prism (2×1 box) whose body covers interior holes."""
    from protoboard.domain.components.package import PackageFootprint
    from protoboard.domain.components.package.loader import build_package

    pkg = build_package(
        {
            "shape": "prism",
            "dims": {"width": 5.0, "length": 5.0},
            "bend": 0,
            "leads": {
                "direction": [0, 0, -1],
                "body_offset": {"unit": 2.5, "value": [[0, 1], [2, 0], [0, -1]]},
                "board_offset": "default",
            },
        },
        "DIP4",
    )
    return ComponentDefinition(
        name="U",
        # type: ignore[arg-type]
        pin_names={},
        package=pkg,  # type: ignore[arg-type]
    )


class TestDRCBodyOverlap:
    def _placed(
        self,
        defn: ComponentDefinition,
        *placements: tuple[str, Point, str],
    ) -> tuple[Layout, dict, dict]:
        insts: dict = {}
        pls: dict = {}
        for refdes, pos, side in placements:
            inst = bind(
                ComponentInstance.new(
                    definition_ref=defn.name, refdes=refdes, value="",
                ),
                defn,
                {},
            )
            insts[inst.id] = inst
            pls[inst.id] = ComponentPlacement(
                component_id=inst.id, position=pos, rotation=0,
                side=side,  # type: ignore[arg-type]
            )
        return Layout(placements=pls, wires=WireCollection()), {defn.name: defn}, insts

    def test_overlapping_bodies_flag_body_overlap(self) -> None:
        board = _perfboard(4, 4)
        defn = _prism_defn()
        lay, comps, insts = self._placed(
            defn,
            ("U1", Point(0.5, 0.5), "top"),
            ("U2", Point(0.5, 0.5), "top"),
        )
        issues = DRCEngine().check(
            board=board, schematic=Schematic(nets={}),
            layout=lay, components=comps, instances=insts,
        )
        body = [i for i in issues if i.code == "BODY_OVERLAP"]
        assert len(body) == 1
        assert body[0].severity == "error"
        assert "bodies overlap" in body[0].message
        assert "U1" in body[0].message and "U2" in body[0].message

    def test_far_apart_bodies_clean(self) -> None:
        board = _perfboard(8, 8)
        defn = _prism_defn()
        lay, comps, insts = self._placed(
            defn,
            ("U1", Point(0.5, 0.5), "top"),
            ("U2", Point(5.5, 5.5), "top"),
        )
        issues = DRCEngine().check(
            board=board, schematic=Schematic(nets={}),
            layout=lay, components=comps, instances=insts,
        )
        assert [i for i in issues if i.code == "BODY_OVERLAP"] == []

    def test_opposite_side_bodies_do_not_collide(self) -> None:
        # Bodies on opposite sides don't share BODY bits; only a pin in the
        # same through-hole would (reported as a pin-collision message, not a
        # body overlap).  This layout has the bodies clear of each other's
        # pin holes, so it should be clean.
        board = _perfboard(4, 4)
        defn = _prism_defn()
        lay, comps, insts = self._placed(
            defn,
            ("U1", Point(0.5, 0.5), "top"),
            ("U2", Point(0.5, 2.5), "bottom"),
        )
        issues = DRCEngine().check(
            board=board, schematic=Schematic(nets={}),
            layout=lay, components=comps, instances=insts,
        )
        assert [i for i in issues if i.code == "BODY_OVERLAP"] == []

    def test_legacy_components_without_package_ignored(self) -> None:
        # Two non-package SIL resistors fully overlapping: the pin-on-pad
        # OVERLAP fires, but BODY_OVERLAP only considers packaged components.
        board = _perfboard(1, 3)
        defn = _r_defn()
        lay, comps, insts = self._placed(
            defn,
            ("R1", Point(0.0, 0.0), "top"),
            ("R2", Point(0.0, 0.0), "top"),
        )
        issues = DRCEngine().check(
            board=board, schematic=Schematic(nets={}),
            layout=lay, components=comps, instances=insts,
        )
        assert [i for i in issues if i.code == "BODY_OVERLAP"] == []
        assert [i for i in issues if i.code == "OVERLAP"] != []

    def test_single_component_no_overlap(self) -> None:
        board = _perfboard(4, 4)
        defn = _prism_defn()
        lay, comps, insts = self._placed(defn, ("U1", Point(0.5, 0.5), "top"))
        issues = DRCEngine().check(
            board=board, schematic=Schematic(nets={}),
            layout=lay, components=comps, instances=insts,
        )
        assert [i for i in issues if i.code == "BODY_OVERLAP"] == []


class TestDRCMessages:
    """Human-readable formatting of issue messages."""

    def test_axis_labels_used_when_enabled(self) -> None:
        from protoboard.domain.axis_labels_settings import (
            AxisLabelFormat,
            AxisLabelSettings,
        )

        board = _perfboard(3, 3)
        defn = _r_defn()
        r1 = bind(ComponentInstance.new(definition_ref="R", refdes="R1", value="10k"), defn, {})
        r2 = bind(ComponentInstance.new(definition_ref="R", refdes="R2", value="10k"), defn, {})
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
            layout=Layout(placements={r1.id: pl_r1, r2.id: pl_r2}, wires=WireCollection()),
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
        r1 = bind(ComponentInstance.new(definition_ref="R", refdes="R1", value="10k"), defn, {})
        r2 = bind(ComponentInstance.new(definition_ref="R", refdes="R2", value="10k"), defn, {})
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
            layout=Layout(placements={r1.id: pl_r1, r2.id: pl_r2}, wires=WireCollection()),
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
        inst = bind(ComponentInstance.new(definition_ref="R", refdes="R1", value="10k"), defn, {})
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
        lay = Layout(placements={}, wires=WireCollection())
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
        lay = Layout(placements={inst.id: pl}, wires=WireCollection())
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
        r1 = bind(ComponentInstance.new(definition_ref="R", refdes="R1", value="1k"), defn, {})
        r2 = bind(ComponentInstance.new(definition_ref="R", refdes="R2", value="1k"), defn, {})
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
            wires=WireCollection([wire_a, wire_b]),
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
        lay = Layout(placements={}, wires=WireCollection([wire_a, wire_b]))
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
        lay = Layout(placements={}, wires=WireCollection([wire_a, wire_b]))
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
        lay = Layout(placements={}, wires=WireCollection([wire_a, wire_b]))
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
        lay = Layout(placements={}, wires=WireCollection([wire_a, wire_b]))
        engine = DRCEngine()
        issues = engine.check(
            board=board, schematic=sch, layout=lay, components={}, instances={}
        )
        interior = [i for i in issues if i.code == "BARE_WIRE_INTERIOR_CROSS"]
        assert interior == []


class TestDRCGroupExports:
    """Group ``exported_pins`` must cover every external net and nothing more.

    External = the net touches a component outside the group, or is global
    (VCC/GND).  Missing coverage → GROUP_MISSING_EXPORT (error, blocks
    autoroute).  Exported pin on a purely-internal net → GROUP_BOGUS_EXPORT
    (warning).
    """

    @staticmethod
    def _three_resistor_setup() -> tuple[
        Board, ComponentDefinition,
        ComponentInstance, ComponentInstance, ComponentInstance,
        Schematic, dict, dict, dict[uuid.UUID, ComponentPlacement],
    ]:
        from protoboard.domain.schematic.net import PinRef as _PinRef
        board = _perfboard(1, 7)
        defn = _r_defn()
        r1 = bind(ComponentInstance.new(definition_ref="R", refdes="R1", value="10k"), defn, {})
        r2 = bind(ComponentInstance.new(definition_ref="R", refdes="R2", value="10k"), defn, {})
        r3 = bind(ComponentInstance.new(definition_ref="R", refdes="R3", value="10k"), defn, {})
        placements = {
            r1.id: ComponentPlacement(component_id=r1.id, position=Point(0.5, 0.5), rotation=90, side="top"),
            r2.id: ComponentPlacement(component_id=r2.id, position=Point(2.5, 0.5), rotation=90, side="top"),
            r3.id: ComponentPlacement(component_id=r3.id, position=Point(4.5, 0.5), rotation=90, side="top"),
        }
        in_net = Net(
            id=uuid.uuid4(), name="VCC",
            pins={_PinRef(component_id=r1.id, pin_number=1)},
            global_net=True,
        )
        ab_net = Net(
            id=uuid.uuid4(), name="AB",
            pins={
                _PinRef(component_id=r1.id, pin_number=2),
                _PinRef(component_id=r2.id, pin_number=1),
                _PinRef(component_id=r3.id, pin_number=1),
            },
        )
        out_net = Net(
            id=uuid.uuid4(), name="OUT",
            pins={_PinRef(component_id=r3.id, pin_number=2)},
        )
        sch = Schematic(nets={n.id: n for n in (in_net, ab_net, out_net)})
        return (
            board, defn, r1, r2, r3, sch,
            {defn.name: defn},
            {r1.id: r1, r2.id: r2, r3.id: r3},
            placements,
        )

    def test_missing_export_for_external_net(self) -> None:
        from protoboard.domain.layout.group import ComponentGroup
        from protoboard.domain.layout.layout import ROOT_GROUP_ID
        board, defn, r1, r2, r3, sch, comps, insts, placements = self._three_resistor_setup()
        gid = uuid.uuid4()
        grp = ComponentGroup(
            id=gid, name="G1",
            component_ids=frozenset({r1.id, r2.id}),
            exported_pins=frozenset(),
        )
        root_grp = ComponentGroup(
            id=ROOT_GROUP_ID, name="root",
            component_ids=frozenset(), child_group_ids=frozenset({gid}),
        )
        lay = Layout(
            placements=placements, wires=WireCollection(),
            groups={ROOT_GROUP_ID: root_grp, gid: grp},
        )
        issues = DRCEngine().check(
            board=board, schematic=sch, layout=lay,
            components=comps, instances=insts,
        )
        missing = [i for i in issues if i.code == "GROUP_MISSING_EXPORT"]
        assert len(missing) == 2
        assert all(i.severity == "error" for i in missing)
        net_names = sorted(sch.nets[i.affected_nets[0]].name for i in missing)
        assert net_names == ["AB", "VCC"]

    def test_complete_export_no_issues(self) -> None:
        from protoboard.domain.layout.group import ComponentGroup
        from protoboard.domain.layout.layout import ROOT_GROUP_ID
        from protoboard.domain.schematic.net import PinRef
        board, defn, r1, r2, r3, sch, comps, insts, placements = self._three_resistor_setup()
        gid = uuid.uuid4()
        grp = ComponentGroup(
            id=gid, name="G1",
            component_ids=frozenset({r1.id, r2.id}),
            exported_pins=frozenset({
                PinRef(component_id=r1.id, pin_number=1),  # VCC (global)
                PinRef(component_id=r1.id, pin_number=2),  # AB
            }),
        )
        root_grp = ComponentGroup(
            id=ROOT_GROUP_ID, name="root",
            component_ids=frozenset(), child_group_ids=frozenset({gid}),
        )
        lay = Layout(
            placements=placements, wires=WireCollection(),
            groups={ROOT_GROUP_ID: root_grp, gid: grp},
        )
        issues = DRCEngine().check(
            board=board, schematic=sch, layout=lay,
            components=comps, instances=insts,
        )
        assert [i for i in issues if i.code.startswith("GROUP_")] == []

    def test_bogus_export_on_internal_net(self) -> None:
        from protoboard.domain.layout.group import ComponentGroup
        from protoboard.domain.layout.layout import ROOT_GROUP_ID
        from protoboard.domain.schematic.net import PinRef
        from protoboard.domain.schematic.net import PinRef as _PinRef
        board = _perfboard(1, 5)
        defn = _r_defn()
        r1 = bind(ComponentInstance.new(definition_ref="R", refdes="R1", value="10k"), defn, {})
        r2 = bind(ComponentInstance.new(definition_ref="R", refdes="R2", value="10k"), defn, {})
        placements = {
            r1.id: ComponentPlacement(component_id=r1.id, position=Point(0.5, 0.5), rotation=90, side="top"),
            r2.id: ComponentPlacement(component_id=r2.id, position=Point(2.5, 0.5), rotation=90, side="top"),
        }
        vcc = Net(id=uuid.uuid4(), name="VCC", global_net=True,
                  pins={_PinRef(component_id=r1.id, pin_number=1)})
        gnd = Net(id=uuid.uuid4(), name="GND", global_net=True,
                  pins={_PinRef(component_id=r2.id, pin_number=2)})
        mid = Net(id=uuid.uuid4(), name="MID",
                  pins={_PinRef(component_id=r1.id, pin_number=2),
                        _PinRef(component_id=r2.id, pin_number=1)})
        sch = Schematic(nets={n.id: n for n in (vcc, gnd, mid)})
        gid = uuid.uuid4()
        grp = ComponentGroup(
            id=gid, name="G1",
            component_ids=frozenset({r1.id, r2.id}),
            exported_pins=frozenset({
                PinRef(component_id=r1.id, pin_number=1),  # VCC, ok
                PinRef(component_id=r2.id, pin_number=2),  # GND, ok
                PinRef(component_id=r1.id, pin_number=2),  # MID, BOGUS
            }),
        )
        root_grp = ComponentGroup(
            id=ROOT_GROUP_ID, name="root",
            component_ids=frozenset(), child_group_ids=frozenset({gid}),
        )
        lay = Layout(
            placements=placements, wires=WireCollection(),
            groups={ROOT_GROUP_ID: root_grp, gid: grp},
        )
        issues = DRCEngine().check(
            board=board, schematic=sch, layout=lay,
            components={defn.name: defn},
            instances={r1.id: r1, r2.id: r2},
        )
        bogus = [i for i in issues if i.code == "GROUP_BOGUS_EXPORT"]
        assert len(bogus) == 1
        assert bogus[0].severity == "warning"
        assert sch.nets[bogus[0].affected_nets[0]].name == "MID"
        assert [i for i in issues if i.code == "GROUP_MISSING_EXPORT"] == []

    def test_nested_group_uses_recursive_membership(self) -> None:
        """Sub-group's external-net set is computed against its OWN recursive
        members.  A net spanning sub-group and parent's siblings is external
        to the sub-group, internal to the parent."""
        from protoboard.domain.layout.group import ComponentGroup
        from protoboard.domain.layout.layout import ROOT_GROUP_ID
        from protoboard.domain.schematic.net import PinRef
        board, defn, r1, r2, r3, sch, comps, insts, placements = self._three_resistor_setup()
        inner_gid = uuid.uuid4()
        outer_gid = uuid.uuid4()
        inner = ComponentGroup(
            id=inner_gid, name="Inner",
            component_ids=frozenset({r1.id, r2.id}),
            exported_pins=frozenset(),  # missing — will fire
        )
        outer = ComponentGroup(
            id=outer_gid, name="Outer",
            component_ids=frozenset({r3.id}),
            child_group_ids=frozenset({inner_gid}),
            exported_pins=frozenset({
                PinRef(component_id=r1.id, pin_number=1),  # VCC (global)
                PinRef(component_id=r3.id, pin_number=2),  # OUT
            }),
        )
        root_grp = ComponentGroup(
            id=ROOT_GROUP_ID, name="root",
            component_ids=frozenset(), child_group_ids=frozenset({outer_gid}),
        )
        lay = Layout(
            placements=placements, wires=WireCollection(),
            groups={ROOT_GROUP_ID: root_grp, outer_gid: outer, inner_gid: inner},
        )
        issues = DRCEngine().check(
            board=board, schematic=sch, layout=lay,
            components=comps, instances=insts,
        )
        missing = [i for i in issues if i.code == "GROUP_MISSING_EXPORT"]
        # Inner sees VCC (global) and AB (R3 outside inner) as external.
        # Outer sees VCC (global) and OUT (single pin) — both covered.
        assert {sch.nets[i.affected_nets[0]].name for i in missing} == {"VCC", "AB"}


class TestDRCGroupInvalidExport:
    """`G.exported_pins` must satisfy the encapsulated re-export chain
    (see docs/archive/group-export-encapsulation.md):

      ep.component_id ∈ G.component_ids
      OR ep ∈ ⋃ child.exported_pins  (for direct children)

    Grandchild flat exports — where a pin lives in C but neither C's parent
    B nor any intermediate group re-exports it — must produce
    GROUP_INVALID_EXPORT (error).
    """

    def test_grandchild_flat_export_flagged_invalid(self) -> None:
        from protoboard.domain.layout.group import ComponentGroup
        from protoboard.domain.layout.layout import ROOT_GROUP_ID
        from protoboard.domain.schematic.net import PinRef
        from protoboard.domain.schematic.net import PinRef as _PinRef
        board = _perfboard(1, 5)
        defn = _r_defn()
        r_inner = bind(ComponentInstance.new(definition_ref="R", refdes="R1", value="10k"), defn, {})
        r_outside = bind(ComponentInstance.new(definition_ref="R", refdes="R2", value="10k"), defn, {})
        placements = {
            r_inner.id: ComponentPlacement(component_id=r_inner.id, position=Point(0.5, 0.5), rotation=90, side="top"),
            r_outside.id: ComponentPlacement(component_id=r_outside.id, position=Point(2.5, 0.5), rotation=90, side="top"),
        }
        ext_net = Net(
            id=uuid.uuid4(), name="EXT",
            pins={
                _PinRef(component_id=r_inner.id, pin_number=1),
                _PinRef(component_id=r_outside.id, pin_number=1),
            },
        )
        sch = Schematic(nets={ext_net.id: ext_net})

        # C contains r_inner and exports its pin 1.
        # B contains C but does NOT re-export.
        # A flat-exports r_inner pin 1 → INVALID.
        ep = PinRef(component_id=r_inner.id, pin_number=1)
        c_gid = uuid.uuid4()
        b_gid = uuid.uuid4()
        a_gid = uuid.uuid4()
        c = ComponentGroup(
            id=c_gid, name="C",
            component_ids=frozenset({r_inner.id}),
            exported_pins=frozenset({ep}),
        )
        b = ComponentGroup(
            id=b_gid, name="B",
            child_group_ids=frozenset({c_gid}),
            # Note: B does NOT re-export ep.
        )
        a = ComponentGroup(
            id=a_gid, name="A",
            child_group_ids=frozenset({b_gid}),
            exported_pins=frozenset({ep}),
        )
        root_grp = ComponentGroup(
            id=ROOT_GROUP_ID, name="root",
            component_ids=frozenset(), child_group_ids=frozenset({a_gid}),
        )
        lay = Layout(
            placements=placements, wires=WireCollection(),
            groups={
                ROOT_GROUP_ID: root_grp,
                a_gid: a, b_gid: b, c_gid: c,
            },
        )
        issues = DRCEngine().check(
            board=board, schematic=sch, layout=lay,
            components={defn.name: defn},
            instances={r_inner.id: r_inner, r_outside.id: r_outside},
        )
        invalid = [i for i in issues if i.code == "GROUP_INVALID_EXPORT"]
        # Both A and B violate: A exports ep, B doesn't re-export it.
        # C is fine because the pin is a direct member.
        invalid_groups = {tuple(i.affected_pins)[0] for i in invalid}
        # PinRef equality — should be the ep's pin on both A and B's lists.
        from protoboard.domain.schematic.net import PinRef as _PR
        assert _PR(component_id=r_inner.id, pin_number=1) in invalid_groups
        assert any("A" in i.message for i in invalid)
        # B does not export ep at all — only A is invalid.
        assert len(invalid) == 1
        assert invalid[0].severity == "error"

    def test_chain_makes_export_valid_no_issue(self) -> None:
        # Same tree but B re-exports ep → A's entry is chain-legal, no
        # INVALID issue.
        from protoboard.domain.layout.group import ComponentGroup
        from protoboard.domain.layout.layout import ROOT_GROUP_ID
        from protoboard.domain.schematic.net import PinRef
        from protoboard.domain.schematic.net import PinRef as _PinRef
        board = _perfboard(1, 5)
        defn = _r_defn()
        r_inner = bind(ComponentInstance.new(definition_ref="R", refdes="R1", value="10k"), defn, {})
        r_outside = bind(ComponentInstance.new(definition_ref="R", refdes="R2", value="10k"), defn, {})
        placements = {
            r_inner.id: ComponentPlacement(component_id=r_inner.id, position=Point(0.5, 0.5), rotation=90, side="top"),
            r_outside.id: ComponentPlacement(component_id=r_outside.id, position=Point(2.5, 0.5), rotation=90, side="top"),
        }
        ext_net = Net(
            id=uuid.uuid4(), name="EXT",
            pins={
                _PinRef(component_id=r_inner.id, pin_number=1),
                _PinRef(component_id=r_outside.id, pin_number=1),
            },
        )
        sch = Schematic(nets={ext_net.id: ext_net})

        ep = PinRef(component_id=r_inner.id, pin_number=1)
        c_gid = uuid.uuid4()
        b_gid = uuid.uuid4()
        a_gid = uuid.uuid4()
        c = ComponentGroup(
            id=c_gid, name="C",
            component_ids=frozenset({r_inner.id}),
            exported_pins=frozenset({ep}),
        )
        b = ComponentGroup(
            id=b_gid, name="B",
            child_group_ids=frozenset({c_gid}),
            exported_pins=frozenset({ep}),  # re-export
        )
        a = ComponentGroup(
            id=a_gid, name="A",
            child_group_ids=frozenset({b_gid}),
            exported_pins=frozenset({ep}),
        )
        root_grp = ComponentGroup(
            id=ROOT_GROUP_ID, name="root",
            component_ids=frozenset(), child_group_ids=frozenset({a_gid}),
        )
        lay = Layout(
            placements=placements, wires=WireCollection(),
            groups={
                ROOT_GROUP_ID: root_grp,
                a_gid: a, b_gid: b, c_gid: c,
            },
        )
        issues = DRCEngine().check(
            board=board, schematic=sch, layout=lay,
            components={defn.name: defn},
            instances={r_inner.id: r_inner, r_outside.id: r_outside},
        )
        assert [i for i in issues if i.code == "GROUP_INVALID_EXPORT"] == []

    def test_invalid_not_double_flagged_as_bogus(self) -> None:
        """An INVALID export must not also generate a BOGUS warning —
        the user gets one error per offending entry."""
        from protoboard.domain.layout.group import ComponentGroup
        from protoboard.domain.layout.layout import ROOT_GROUP_ID
        from protoboard.domain.schematic.net import PinRef
        from protoboard.domain.schematic.net import PinRef as _PinRef
        board = _perfboard(1, 5)
        defn = _r_defn()
        r_inner = bind(ComponentInstance.new(definition_ref="R", refdes="R1", value="10k"), defn, {})
        r_outside = bind(ComponentInstance.new(definition_ref="R", refdes="R2", value="10k"), defn, {})
        placements = {
            r_inner.id: ComponentPlacement(component_id=r_inner.id, position=Point(0.5, 0.5), rotation=90, side="top"),
            r_outside.id: ComponentPlacement(component_id=r_outside.id, position=Point(2.5, 0.5), rotation=90, side="top"),
        }
        # Net stays fully inside C (no outside pin, not global).  An INVALID
        # flat export on A would normally also count as BOGUS for A — assert
        # only INVALID fires.
        internal_net = Net(
            id=uuid.uuid4(), name="INT",
            pins={
                _PinRef(component_id=r_inner.id, pin_number=1),
                _PinRef(component_id=r_outside.id, pin_number=1),
            },
        )
        sch = Schematic(nets={internal_net.id: internal_net})

        ep = PinRef(component_id=r_inner.id, pin_number=1)
        c_gid = uuid.uuid4()
        a_gid = uuid.uuid4()
        # A contains C (and r_outside as a direct member).  Net is fully
        # inside A's recursive set → BOGUS would normally apply to ep on A.
        # But ep is also chain-invalid (C exports it; A's child C does so
        # directly, so actually... we need C wrapped in B).
        b_gid = uuid.uuid4()
        c = ComponentGroup(
            id=c_gid, name="C",
            component_ids=frozenset({r_inner.id}),
            exported_pins=frozenset({ep}),
        )
        b = ComponentGroup(
            id=b_gid, name="B",
            child_group_ids=frozenset({c_gid}),
            # B does not re-export.
        )
        a = ComponentGroup(
            id=a_gid, name="A",
            component_ids=frozenset({r_outside.id}),
            child_group_ids=frozenset({b_gid}),
            exported_pins=frozenset({ep}),
        )
        root_grp = ComponentGroup(
            id=ROOT_GROUP_ID, name="root",
            component_ids=frozenset(), child_group_ids=frozenset({a_gid}),
        )
        lay = Layout(
            placements=placements, wires=WireCollection(),
            groups={
                ROOT_GROUP_ID: root_grp,
                a_gid: a, b_gid: b, c_gid: c,
            },
        )
        issues = DRCEngine().check(
            board=board, schematic=sch, layout=lay,
            components={defn.name: defn},
            instances={r_inner.id: r_inner, r_outside.id: r_outside},
        )
        a_issues_invalid = [
            i for i in issues if i.code == "GROUP_INVALID_EXPORT"
            and "A" in i.message
        ]
        a_issues_bogus = [
            i for i in issues if i.code == "GROUP_BOGUS_EXPORT"
            and "A" in i.message
        ]
        assert len(a_issues_invalid) == 1
        assert a_issues_bogus == []


class TestAutoroutePreconditionBlocksMissingExport:
    def test_missing_export_code_in_blocker_set(self) -> None:
        from protoboard.services.routing.autorouter import (
            _AUTOROUTE_BLOCKING_DRC_CODES,
        )
        assert "GROUP_MISSING_EXPORT" in _AUTOROUTE_BLOCKING_DRC_CODES
        assert "SHORT" in _AUTOROUTE_BLOCKING_DRC_CODES
        assert "OFF_BOARD_PIN" in _AUTOROUTE_BLOCKING_DRC_CODES


class TestDRCOffBoardPin:
    """Every placed component pin must land on a board pad on its side.

    Detects two failure modes: component dragged off the outline, and
    component spanning a gap between pad clusters (cuts).
    """

    def test_component_fully_on_board_no_issue(self) -> None:
        board = _perfboard(1, 4)
        defn = _r_defn()
        r1 = bind(ComponentInstance.new(definition_ref="R", refdes="R1", value="10k"), defn, {})
        # SIL(2) rot=90: pin1@(0.5,0.5), pin2@(1.5,0.5) — both on board.
        placements = {r1.id: ComponentPlacement(
            component_id=r1.id, position=Point(0.5, 0.5), rotation=90, side="top",
        )}
        lay = Layout(placements=placements, wires=WireCollection())
        sch = Schematic(nets={})
        issues = DRCEngine().check(
            board=board, schematic=sch, layout=lay,
            components={defn.name: defn},
            instances={r1.id: r1},
        )
        assert [i for i in issues if i.code == "OFF_BOARD_PIN"] == []

    def test_component_overhanging_board_flags_pin(self) -> None:
        board = _perfboard(1, 2)  # pads at x=0.5, 1.5
        defn = _r_defn()
        r1 = bind(ComponentInstance.new(definition_ref="R", refdes="R1", value="10k"), defn, {})
        # pin1@(1.5, 0.5)=on board last pad, pin2@(2.5, 0.5)=off board.
        placements = {r1.id: ComponentPlacement(
            component_id=r1.id, position=Point(1.5, 0.5), rotation=90, side="top",
        )}
        lay = Layout(placements=placements, wires=WireCollection())
        sch = Schematic(nets={})
        issues = DRCEngine().check(
            board=board, schematic=sch, layout=lay,
            components={defn.name: defn},
            instances={r1.id: r1},
        )
        off = [i for i in issues if i.code == "OFF_BOARD_PIN"]
        assert len(off) == 1
        assert off[0].severity == "error"
        assert off[0].affected_pins[0].pin_number == 2
        assert off[0].affected_components[0] == r1.id

    def test_top_only_pad_rejects_bottom_side_pin(self) -> None:
        """Pad with top=True, bottom=False, plated_through=False cannot host
        a component placed on the bottom side."""
        grid = BoardGrid(
            width_policy=SizePolicy.HUG,
            height_policy=SizePolicy.HUG,
            cell=PadAtom(
                width_policy=SizePolicy.HUG,
                height_policy=SizePolicy.HUG,
                layers=PadLayers(top=True, bottom=False, plated_through=False),
            ),
            rows=1, cols=2, row_gap=0.0, col_gap=0.0,
        )
        board = Board(root=grid)
        defn = _r_defn()
        r1 = bind(ComponentInstance.new(definition_ref="R", refdes="R1", value="10k"), defn, {})
        placements = {r1.id: ComponentPlacement(
            component_id=r1.id, position=Point(0.5, 0.5), rotation=90, side="bottom",
        )}
        lay = Layout(placements=placements, wires=WireCollection())
        sch = Schematic(nets={})
        issues = DRCEngine().check(
            board=board, schematic=sch, layout=lay,
            components={defn.name: defn},
            instances={r1.id: r1},
        )
        off = [i for i in issues if i.code == "OFF_BOARD_PIN"]
        # Both pins miss — both bottom side, neither pad reaches bottom.
        assert len(off) == 2


# ---------------------------------------------------------------------------
# HALF_ORPHAN_WIRE + WIRE_PIN_OFF_PLATED_THROUGH
# (docs/archive/wire-pin-implementation.md §3.5; rule 9 in pad-pin-wire-junction-semantics)
# ---------------------------------------------------------------------------


def _bare_perfboard(rows: int, cols: int, plated: bool) -> Board:
    """Variant of _perfboard that lets a test control plated-through."""
    return Board(root=BoardGrid(
        width_policy=SizePolicy.HUG,
        height_policy=SizePolicy.HUG,
        cell=PadAtom(
            width_policy=SizePolicy.HUG,
            height_policy=SizePolicy.HUG,
            layers=PadLayers(top=True, bottom=True, plated_through=plated),
        ),
        rows=rows, cols=cols, row_gap=0.0, col_gap=0.0,
    ))


class TestDRCHalfOrphanWire:
    """A wire endpoint with a sole-owner JointKnot is half-orphan.  DRC
    fires a warning so the user decides: delete the wire, extend it, or
    convert the endpoint to a wire-pin.
    """

    def _make_half_orphan_layout(self) -> Layout:
        from protoboard.domain.layout.knot import JointKnot
        from protoboard.domain.layout.wire import KnotRef

        lay = Layout(placements={}, wires=WireCollection())
        ka = JointKnot(id=uuid.uuid4(), position=Point(0.5, 0.5))
        kb = JointKnot(id=uuid.uuid4(), position=Point(1.5, 0.5))
        lay.knots[ka.id] = ka
        lay.knots[kb.id] = kb
        wire = Wire(
            id=uuid.uuid4(), path=[Point(0.5, 0.5), Point(1.5, 0.5)],
            side="top", a=KnotRef(id=ka.id), b=KnotRef(id=kb.id),
        )
        lay.add_wire(wire)
        return lay

    def test_lone_joint_endpoint_fires_warning(self) -> None:
        board = _bare_perfboard(1, 3, plated=True)
        lay = self._make_half_orphan_layout()
        issues = DRCEngine().check(
            board=board, schematic=Schematic(nets={}), layout=lay,
            components={}, instances={},
        )
        half = [i for i in issues if i.code == "HALF_ORPHAN_WIRE"]
        # Both endpoints are lone joints → 2 warnings.
        assert len(half) == 2
        assert all(i.severity == "warning" for i in half)

    def test_t_junction_endpoint_not_orphan(self) -> None:
        # JointKnot shared with another wire is a t-junction, not an
        # orphan — must not fire HALF_ORPHAN_WIRE.
        from protoboard.domain.layout.knot import JointKnot
        from protoboard.domain.layout.wire import KnotRef

        board = _bare_perfboard(1, 3, plated=True)
        lay = Layout(placements={}, wires=WireCollection())
        ka = JointKnot(id=uuid.uuid4(), position=Point(0.5, 0.5))
        kb = JointKnot(id=uuid.uuid4(), position=Point(1.5, 0.5))
        kc = JointKnot(id=uuid.uuid4(), position=Point(2.5, 0.5))
        lay.knots[ka.id] = ka
        lay.knots[kb.id] = kb
        lay.knots[kc.id] = kc
        w1 = Wire(
            id=uuid.uuid4(), path=[Point(0.5, 0.5), Point(1.5, 0.5)],
            side="top", a=KnotRef(id=ka.id), b=KnotRef(id=kb.id),
        )
        w2 = Wire(
            id=uuid.uuid4(), path=[Point(1.5, 0.5), Point(2.5, 0.5)],
            side="top", a=KnotRef(id=kb.id), b=KnotRef(id=kc.id),
        )
        lay.add_wire(w1)
        lay.add_wire(w2)
        # kb is shared by both wires (t-junction), ka and kc are still
        # half-orphan.
        issues = DRCEngine().check(
            board=board, schematic=Schematic(nets={}), layout=lay,
            components={}, instances={},
        )
        half_ids = {
            (i.locations[0].x, i.locations[0].y)
            for i in issues if i.code == "HALF_ORPHAN_WIRE"
        }
        assert (0.5, 0.5) in half_ids
        assert (2.5, 0.5) in half_ids
        assert (1.5, 0.5) not in half_ids  # t-junction, not orphan

    def test_wire_pin_endpoint_not_orphan(self) -> None:
        # Promoting an endpoint to a wire-pin suppresses the warning —
        # rule 9: "convert to wire-pin" is one of the three options
        # offered to the user when the orphan fires.
        from protoboard.domain.commands.component_cmds import (
            ConvertEndpointToWirePinCommand,
        )

        board = _bare_perfboard(1, 3, plated=True)
        lay = self._make_half_orphan_layout()
        # Look up wire id (only one in lay).
        wire = next(iter(lay.wires))
        # ConvertEndpointToWirePinCommand needs a state proxy.

        class _State:
            def __init__(self, lay: Layout, board: Board) -> None:
                self.board = board
                self.layout = lay
                self.instances: dict[uuid.UUID, ComponentInstance] = {}
                self.components: dict[str, ComponentDefinition] = {}

        state = _State(lay, board)
        ConvertEndpointToWirePinCommand(
            wire_id=wire.id, endpoint="a", net_id=None,
        ).apply(state)  # type: ignore[arg-type]
        ConvertEndpointToWirePinCommand(
            wire_id=wire.id, endpoint="b", net_id=None,
        ).apply(state)  # type: ignore[arg-type]
        issues = DRCEngine().check(
            board=board, schematic=Schematic(nets={}), layout=lay,
            components={}, instances={},
        )
        assert [i for i in issues if i.code == "HALF_ORPHAN_WIRE"] == []


class TestDRCWirePinOffPlatedThrough:
    """Wire-pin must sit on a plated-through pad — rule 5 (through-hole
    propagation).  Off-plated wire-pin is an error.
    """

    def _make_wire_pin_layout(self, position: Point) -> Layout:
        from protoboard.domain.layout.knot import JointKnot, WirePinKnot
        from protoboard.domain.layout.wire import KnotRef

        lay = Layout(placements={}, wires=WireCollection())
        ka = JointKnot(id=uuid.uuid4(), position=position)
        kb = JointKnot(
            id=uuid.uuid4(),
            position=Point(position.x + 1.0, position.y),
        )
        lay.knots[ka.id] = ka
        lay.knots[kb.id] = kb
        wire = Wire(
            id=uuid.uuid4(),
            path=[position, position + Vector(1.0, 0.0)],
            side="top", a=KnotRef(id=ka.id), b=KnotRef(id=kb.id),
        )
        lay.add_wire(wire)
        # Promote ka to wire-pin at the given position.
        lay.knots[ka.id] = WirePinKnot(
            id=ka.id, position=position, net_id=None,
            wire_ids=frozenset({wire.id}),
        )
        return lay

    def test_wire_pin_on_plated_through_no_issue(self) -> None:
        board = _bare_perfboard(1, 3, plated=True)
        lay = self._make_wire_pin_layout(Point(0.5, 0.5))
        issues = DRCEngine().check(
            board=board, schematic=Schematic(nets={}), layout=lay,
            components={}, instances={},
        )
        assert [
            i for i in issues if i.code == "WIRE_PIN_OFF_PLATED_THROUGH"
        ] == []

    def test_wire_pin_on_isolated_pad_fires_error(self) -> None:
        # Pad without plated-through → error.
        board = _bare_perfboard(1, 3, plated=False)
        lay = self._make_wire_pin_layout(Point(0.5, 0.5))
        issues = DRCEngine().check(
            board=board, schematic=Schematic(nets={}), layout=lay,
            components={}, instances={},
        )
        bad = [
            i for i in issues if i.code == "WIRE_PIN_OFF_PLATED_THROUGH"
        ]
        assert len(bad) == 1
        assert bad[0].severity == "error"

    def test_wire_pin_off_pad_fires_error(self) -> None:
        # Position between pads → error.
        board = _bare_perfboard(1, 3, plated=True)
        lay = self._make_wire_pin_layout(Point(0.7, 0.7))
        issues = DRCEngine().check(
            board=board, schematic=Schematic(nets={}), layout=lay,
            components={}, instances={},
        )
        bad = [
            i for i in issues if i.code == "WIRE_PIN_OFF_PLATED_THROUGH"
        ]
        assert len(bad) == 1


class TestDRCBodyOverlapResolvedVariant:
    """The body-overlap mask must come from the INSTANCE-resolved package —
    never ``defn.package`` (the default footprint option's body). Regression
    for a placed SMD resistor colliding via its phantom axial default body
    with a TO-220 five rasters away."""

    @staticmethod
    def _build(spec_leads: list, dims: dict, name: str):
        from protoboard.domain.components.package.loader import build_package
        return build_package(
            {
                "shape": "prism",
                "dims": dims,
                "bend": 0,
                "leads": {
                    "direction": [0, 0, -1],
                    "body_offset": {"unit": 2.5, "value": spec_leads},
                    "board_offset": "default",
                },
            },
            name,
        )

    def _smd_resistor_defn_and_families(self):
        """A part whose DEFAULT body is a long axial, offering an SMD variant."""
        from protoboard.domain.components.package import (
            PackageFamily,
            PackageFootprint,
            PackageVariant,
        )
        axial = self._build([[4, 0]], {"width": 2.5, "length": 7.5}, "AXIAL")
        smd = self._build([[1, 0]], {"width": 1.25, "length": 2.0}, "SMD")
        families = {
            "smd-0805": PackageFamily(
                family_id="smd-0805", label="SMD 0805",
                variants=(PackageVariant(variant_id="typ", label="typ",
                                         package=smd),),
            )
        }
        defn = ComponentDefinition(
            name="R",
            # type: ignore[arg-type]
            pin_names={},
            package=axial,  # type: ignore[arg-type]
        )
        return defn, families

    def _check(self, layout, comps, insts):
        return DRCEngine().check(
            board=_perfboard(16, 16), schematic=Schematic(nets={}),
            layout=layout, components=comps, instances=insts,
        )

    def test_smd_variant_does_not_collide_via_phantom_default_body(
        self,
    ) -> None:
        defn_r, families = self._smd_resistor_defn_and_families()
        defn_u = _prism_defn()
        r = bind(
            ComponentInstance.new(definition_ref="R", refdes="R1", value="",
                                  package_variant="smd-0805/typ"),
            defn_r, families,
        )
        u = bind(ComponentInstance.new(definition_ref="U", refdes="U1",
                                       value=""), defn_u, {})
        insts = {r.id: r, u.id: u}
        pls = {
            # SMD chip on pads (1,1)-(2,1); the phantom axial default would
            # reach 3 holes further right.
            r.id: ComponentPlacement(component_id=r.id, position=Point(1, 1),
                                     rotation=0, side="top"),
            u.id: ComponentPlacement(component_id=u.id, position=Point(4, 1),
                                     rotation=0, side="top"),
        }
        layout = Layout(placements=pls, wires=WireCollection())
        issues = self._check(layout, {"R": defn_r, "U": defn_u}, insts)
        assert [i for i in issues if i.code == "BODY_OVERLAP"] == []

    def test_radial_prism_mask_lies_along_the_pin_row(self) -> None:
        """A TO-220-style body (10 mm ALONG its 3-pin row, 4.5 mm across) must
        not claim holes two rows away perpendicular to the row — and must
        claim them along the row."""
        from protoboard.domain.components.package import PackageFootprint
        to220ish = self._build([[1, 0], [1, 0]],
                               {"width": 4.5, "length": 10.0}, "TO220ISH")
        defn_t = ComponentDefinition(
            name="Q",
            # type: ignore[arg-type]
            pin_names={},
            package=to220ish,  # type: ignore[arg-type]
        )
        defn_u = _prism_defn()

        def place(upos: Point):
            t = bind(ComponentInstance.new(definition_ref="Q", refdes="Q1",
                                           value=""), defn_t, {})
            u = bind(ComponentInstance.new(definition_ref="U", refdes="U1",
                                           value=""), defn_u, {})
            pls = {
                t.id: ComponentPlacement(component_id=t.id,
                                         position=Point(4, 4),
                                         rotation=0, side="top"),
                u.id: ComponentPlacement(component_id=u.id, position=upos,
                                         rotation=0, side="top"),
            }
            layout = Layout(placements=pls, wires=WireCollection())
            return self._check(layout, {"Q": defn_t, "U": defn_u},
                               {t.id: t, u.id: u})

        # Two rows BELOW the pin row (outside the 4.5 mm body depth): clean.
        below = [i for i in place(Point(4, 7))
                 if i.code == "BODY_OVERLAP"]
        assert below == []
        # ALONG the row just past the last pin (inside the 10 mm face): flags.
        along = [i for i in place(Point(7, 4)) if i.code == "BODY_OVERLAP"]
        assert len(along) == 1
