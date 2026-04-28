# SPDX-License-Identifier: MIT
"""Tests for Schematic — logical netlist (pure graph, no geometry)."""

import dataclasses
import uuid

import pytest

from protoboard.domain.schematic.net import Net
from protoboard.domain.schematic.schematic import PinRef, Schematic


class TestPinRef:
    def test_construct(self) -> None:
        comp_id = uuid.uuid4()
        ref = PinRef(component_id=comp_id, pin_number=3)
        assert ref.component_id == comp_id
        assert ref.pin_number == 3

    def test_frozen(self) -> None:
        ref = PinRef(component_id=uuid.uuid4(), pin_number=1)
        with pytest.raises(dataclasses.FrozenInstanceError):
            ref.pin_number = 2  # type: ignore[misc]

    def test_hashable(self) -> None:
        comp_id = uuid.uuid4()
        a = PinRef(component_id=comp_id, pin_number=1)
        b = PinRef(component_id=comp_id, pin_number=1)
        assert a == b
        assert hash(a) == hash(b)
        assert len({a, b}) == 1

    def test_different_pins_unequal(self) -> None:
        comp_id = uuid.uuid4()
        a = PinRef(component_id=comp_id, pin_number=1)
        b = PinRef(component_id=comp_id, pin_number=2)
        assert a != b

    def test_zero_pin_raises(self) -> None:
        with pytest.raises(ValueError):
            PinRef(component_id=uuid.uuid4(), pin_number=0)

    def test_negative_pin_raises(self) -> None:
        with pytest.raises(ValueError):
            PinRef(component_id=uuid.uuid4(), pin_number=-1)


class TestNet:
    def test_construct(self) -> None:
        net_id = uuid.uuid4()
        net = Net(id=net_id, name="VCC", pins=set())
        assert net.id == net_id
        assert net.name == "VCC"
        assert net.pins == set()

    def test_with_pins(self) -> None:
        comp_id = uuid.uuid4()
        pins = {
            PinRef(component_id=comp_id, pin_number=1),
            PinRef(component_id=comp_id, pin_number=2),
        }
        net = Net(id=uuid.uuid4(), name="GND", pins=pins)
        assert len(net.pins) == 2

    def test_empty_name_raises(self) -> None:
        with pytest.raises(ValueError):
            Net(id=uuid.uuid4(), name="", pins=set())


class TestSchematic:
    def _make_schematic(self) -> tuple[Schematic, uuid.UUID, uuid.UUID]:
        comp1 = uuid.uuid4()
        comp2 = uuid.uuid4()
        vcc_net = Net(
            id=uuid.uuid4(),
            name="VCC",
            pins={PinRef(component_id=comp1, pin_number=8)},
        )
        gnd_net = Net(
            id=uuid.uuid4(),
            name="GND",
            pins={
                PinRef(component_id=comp1, pin_number=4),
                PinRef(component_id=comp2, pin_number=1),
            },
        )
        sig_net = Net(
            id=uuid.uuid4(),
            name="SIG",
            pins={
                PinRef(component_id=comp1, pin_number=1),
                PinRef(component_id=comp2, pin_number=2),
            },
        )
        sch = Schematic(
            nets={n.id: n for n in (vcc_net, gnd_net, sig_net)},
        )
        return sch, comp1, comp2

    def test_construct(self) -> None:
        sch, _, _ = self._make_schematic()
        assert len(sch.nets) == 3

    def test_net_names(self) -> None:
        sch, _, _ = self._make_schematic()
        names = {n.name for n in sch.nets.values()}
        assert names == {"VCC", "GND", "SIG"}

    def test_pins_for_component(self) -> None:
        sch, comp1, _ = self._make_schematic()
        pins = sch.pins_for_component(comp1)
        # comp1 has pin 8 (VCC), pin 4 (GND), pin 1 (SIG).
        assert len(pins) == 3
        pin_numbers = {ref.pin_number for ref in pins}
        assert pin_numbers == {1, 4, 8}

    def test_net_for_pin(self) -> None:
        sch, comp1, _ = self._make_schematic()
        ref = PinRef(component_id=comp1, pin_number=8)
        net = sch.net_for_pin(ref)
        assert net is not None
        assert net.name == "VCC"

    def test_net_for_unassigned_pin_is_none(self) -> None:
        sch, comp1, _ = self._make_schematic()
        ref = PinRef(component_id=comp1, pin_number=99)
        assert sch.net_for_pin(ref) is None

    def test_duplicate_net_names_allowed(self) -> None:
        # Two nets with the same display name is unusual but not forbidden
        # at the schematic level (different UUIDs).
        n1 = Net(id=uuid.uuid4(), name="N$1", pins=set())
        n2 = Net(id=uuid.uuid4(), name="N$1", pins=set())
        sch = Schematic(nets={n1.id: n1, n2.id: n2})
        assert len(sch.nets) == 2
