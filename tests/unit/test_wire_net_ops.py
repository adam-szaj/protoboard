# SPDX-License-Identifier: MIT
"""Tests for wire_net_ops pure functions (no Qt required)."""

from __future__ import annotations

import uuid

from protoboard.domain.components.definition import ComponentDefinition
from protoboard.domain.components.footprints import SIL
from protoboard.domain.components.instance import ComponentInstance
from protoboard.domain.layout.layout import Layout
from protoboard.domain.layout.placement import ComponentPlacement
from protoboard.domain.layout.wire import Wire, WireCollection
from protoboard.domain.schematic.net import Net, PinRef
from protoboard.domain.schematic.schematic import Schematic
from protoboard.domain.schematic.wire_net_ops import (
    WireNetKind,
    classify_wire_nets,
    find_pin_at,
)
from protoboard.geometry import Point

# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def _r_defn() -> ComponentDefinition:
    return ComponentDefinition(name="R", footprint=SIL(2), pin_names={1: "A", 2: "B"})


def _make_inst(refdes: str = "R1") -> ComponentInstance:
    return ComponentInstance.new(definition_ref="R", refdes=refdes, value="10k")


def _placement(
    inst: ComponentInstance, x: float, y: float, rot: int = 90
) -> ComponentPlacement:
    return ComponentPlacement(
        component_id=inst.id, position=Point(x, y), rotation=rot, side="top"
    )


class _FakeSession:
    """Minimal session stub — no Qt, no board needed."""

    def __init__(self) -> None:
        self.layout = Layout(placements={}, wires=WireCollection())
        self.instances: dict[uuid.UUID, ComponentInstance] = {}
        self.components: dict[str, ComponentDefinition] = {}
        self.schematic = Schematic(nets={})

    def place(self, inst: ComponentInstance, pl: ComponentPlacement) -> None:
        self.layout.placements[inst.id] = pl
        self.instances[inst.id] = inst
        self.components[inst.definition_ref] = _r_defn()


def _wire(a: Point, b: Point) -> Wire:
    return Wire(id=uuid.uuid4(), path=[a, b], insulated=True, side="top")


# SIL(2) rotation=90: pin1 at (x,y), pin2 at (x+1, y)
# So: R1 at (0.5, 0.5) → pin1=(0.5,0.5), pin2=(1.5,0.5)
#     R2 at (2.5, 0.5) → pin1=(2.5,0.5), pin2=(3.5,0.5)
_R1_PIN1 = Point(0.5, 0.5)
_R1_PIN2 = Point(1.5, 0.5)
_R2_PIN1 = Point(2.5, 0.5)
_R2_PIN2 = Point(3.5, 0.5)


def _two_component_session(
    net_r1: Net | None = None,
    net_r2: Net | None = None,
) -> _FakeSession:
    s = _FakeSession()
    r1 = _make_inst("R1")
    r2 = _make_inst("R2")
    s.place(r1, _placement(r1, 0.5, 0.5))
    s.place(r2, _placement(r2, 2.5, 0.5))
    if net_r1 is not None:
        s.schematic.nets[net_r1.id] = net_r1
    if net_r2 is not None:
        s.schematic.nets[net_r2.id] = net_r2
    return s


# ---------------------------------------------------------------------------
# find_pin_at
# ---------------------------------------------------------------------------


class TestFindPinAt:
    def test_exact_hit(self) -> None:
        s = _two_component_session()
        r1_id = list(s.layout.placements.keys())[0]
        result = find_pin_at(_R1_PIN1, s)
        assert result is not None
        comp_id, pin_num = result
        assert comp_id == r1_id
        assert pin_num == 1

    def test_no_hit_between_pads(self) -> None:
        s = _two_component_session()
        result = find_pin_at(Point(2.0, 0.5), s)
        assert result is None

    def test_tolerance_inside(self) -> None:
        s = _two_component_session()
        slightly_off = Point(_R1_PIN1.x + 0.00005, _R1_PIN1.y)
        assert find_pin_at(slightly_off, s) is not None

    def test_tolerance_outside(self) -> None:
        s = _two_component_session()
        too_far = Point(_R1_PIN1.x + 0.001, _R1_PIN1.y)
        assert find_pin_at(too_far, s) is None


# ---------------------------------------------------------------------------
# classify_wire_nets — NO_PINS
# ---------------------------------------------------------------------------


class TestClassifyNoPins:
    def test_both_ends_not_on_pin(self) -> None:
        s = _two_component_session()
        wire = _wire(Point(0.0, 0.0), Point(1.0, 1.0))
        cl = classify_wire_nets(wire, s)
        assert cl.kind == WireNetKind.NO_PINS

    def test_one_end_on_pin_other_not(self) -> None:
        s = _two_component_session()
        wire = _wire(_R1_PIN1, Point(9.9, 9.9))
        cl = classify_wire_nets(wire, s)
        assert cl.kind == WireNetKind.NO_PINS


# ---------------------------------------------------------------------------
# classify_wire_nets — BOTH_UNCONNECTED
# ---------------------------------------------------------------------------


class TestClassifyBothUnconnected:
    def test_both_pins_no_nets(self) -> None:
        s = _two_component_session()
        wire = _wire(_R1_PIN2, _R2_PIN1)
        cl = classify_wire_nets(wire, s)
        assert cl.kind == WireNetKind.BOTH_UNCONNECTED
        assert cl.pin_a is not None
        assert cl.pin_b is not None
        assert cl.net_a is None
        assert cl.net_b is None


# ---------------------------------------------------------------------------
# classify_wire_nets — ONE_UNCONNECTED
# ---------------------------------------------------------------------------


class TestClassifyOneUnconnected:
    def test_pin_a_connected_pin_b_not(self) -> None:
        def _make() -> tuple[_FakeSession, uuid.UUID, uuid.UUID]:
            s = _FakeSession()
            r1 = _make_inst("R1")
            r2 = _make_inst("R2")
            s.place(r1, _placement(r1, 0.5, 0.5))
            s.place(r2, _placement(r2, 2.5, 0.5))
            net = Net(
                id=uuid.uuid4(),
                name="VCC",
                pins={PinRef(component_id=r1.id, pin_number=2)},
            )
            s.schematic.nets[net.id] = net
            return s, r1.id, r2.id

        s, r1_id, r2_id = _make()
        # R1-pin2 is in VCC, R2-pin1 is unconnected
        wire = _wire(_R1_PIN2, _R2_PIN1)
        cl = classify_wire_nets(wire, s)
        assert cl.kind == WireNetKind.ONE_UNCONNECTED
        assert cl.net_a is not None
        assert cl.net_b is None


# ---------------------------------------------------------------------------
# classify_wire_nets — SAME_NET
# ---------------------------------------------------------------------------


class TestClassifySameNet:
    def test_both_in_same_net(self) -> None:
        s = _FakeSession()
        r1 = _make_inst("R1")
        r2 = _make_inst("R2")
        s.place(r1, _placement(r1, 0.5, 0.5))
        s.place(r2, _placement(r2, 2.5, 0.5))
        net = Net(
            id=uuid.uuid4(),
            name="MID",
            pins={
                PinRef(component_id=r1.id, pin_number=2),
                PinRef(component_id=r2.id, pin_number=1),
            },
        )
        s.schematic.nets[net.id] = net
        wire = _wire(_R1_PIN2, _R2_PIN1)
        cl = classify_wire_nets(wire, s)
        assert cl.kind == WireNetKind.SAME_NET
        assert cl.net_a is not None
        assert cl.net_a.id == net.id


# ---------------------------------------------------------------------------
# classify_wire_nets — DIFFERENT_NETS
# ---------------------------------------------------------------------------


class TestClassifyDifferentNets:
    def test_both_in_different_nets(self) -> None:
        s = _FakeSession()
        r1 = _make_inst("R1")
        r2 = _make_inst("R2")
        s.place(r1, _placement(r1, 0.5, 0.5))
        s.place(r2, _placement(r2, 2.5, 0.5))
        net_a = Net(
            id=uuid.uuid4(),
            name="VCC",
            pins={PinRef(component_id=r1.id, pin_number=2)},
        )
        net_b = Net(
            id=uuid.uuid4(),
            name="GND",
            pins={PinRef(component_id=r2.id, pin_number=1)},
        )
        s.schematic.nets[net_a.id] = net_a
        s.schematic.nets[net_b.id] = net_b
        wire = _wire(_R1_PIN2, _R2_PIN1)
        cl = classify_wire_nets(wire, s)
        assert cl.kind == WireNetKind.DIFFERENT_NETS
        assert cl.net_a is not None
        assert cl.net_b is not None
        assert cl.net_a.id != cl.net_b.id


# ---------------------------------------------------------------------------
# Auto-net via wire-touch (W1's endpoint lands on an existing wire of
# some net — W1 inherits that net even though one of its endpoints is
# not on a pin)
# ---------------------------------------------------------------------------


class TestClassifyWireTouchAutoNet:
    def test_new_wire_endpoint_on_existing_wire_interior_inherits_net(
        self,
    ) -> None:
        # R1 and R2 are connected by a horizontal bus running through
        # (1.5, 0.5)..(3.5, 0.5).  The bus has net VCC.  R3 lives
        # below; a new wire goes from R3.pin1 up to the bus middle
        # (2.5, 0.5).  R3.pin1 has no net of its own, so the
        # classification should report ONE_UNCONNECTED with net_a=VCC,
        # letting _extend_net add R3.pin1 to VCC.
        s = _FakeSession()
        r1 = _make_inst("R1")
        r2 = _make_inst("R2")
        r3 = _make_inst("R3")
        s.place(r1, _placement(r1, 0.5, 0.5))
        s.place(r2, _placement(r2, 2.5, 0.5))
        s.place(r3, _placement(r3, 2.5, 2.5))
        # VCC connects R1.pin2 and R2.pin1.
        vcc = Net(
            id=uuid.uuid4(),
            name="VCC",
            pins={
                PinRef(component_id=r1.id, pin_number=2),
                PinRef(component_id=r2.id, pin_number=1),
            },
        )
        s.schematic.nets[vcc.id] = vcc
        # Existing bus wire from R1.pin2 → R2.pin1, passing through
        # (2.5, 0.5) as an interior vertex (well, here it lands on
        # R2.pin1; but classification is invariant to that).
        bus = Wire(
            id=uuid.uuid4(),
            path=[_R1_PIN2, Point(2.5, 0.5)],
            insulated=True, side="top",
        )
        s.layout.wires.append(bus)
        # New wire: R3.pin1 (2.5, 2.5) → bus interior (2.5, 0.5).
        new_wire = _wire(Point(2.5, 2.5), _R2_PIN1)
        cl = classify_wire_nets(new_wire, s)
        assert cl.kind == WireNetKind.ONE_UNCONNECTED
        # net_b derives from R2.pin1 (which IS on VCC) — and pin_b
        # too points at R2.pin1.  pin_a points at R3.pin1, which
        # was unconnected.
        assert cl.net_b is not None and cl.net_b.id == vcc.id
        assert cl.net_a is None
        # pin_a is R3.pin1.
        assert cl.pin_a == (r3.id, 1)

    def test_endpoint_on_wire_interior_no_pin_no_net_pure_wire(
        self,
    ) -> None:
        # Two stub wires meet at a pad without any pin involvement;
        # neither has a net.  Result: NO_PINS — nothing schematic
        # changes.
        s = _FakeSession()
        wire1 = Wire(
            id=uuid.uuid4(),
            path=[Point(0.5, 0.5), Point(2.5, 0.5)],
            insulated=True, side="top",
        )
        s.layout.wires.append(wire1)
        new_wire = _wire(Point(2.5, 0.5), Point(4.5, 0.5))
        cl = classify_wire_nets(new_wire, s)
        assert cl.kind == WireNetKind.NO_PINS
