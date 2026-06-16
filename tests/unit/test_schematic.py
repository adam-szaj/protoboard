# SPDX-License-Identifier: MIT
"""Tests for Schematic — logical netlist (pure graph, no geometry)."""

import dataclasses
import uuid

import pytest

from protoboard.domain.components.definition import (
    ComponentClass,
    ComponentDefinition,
)
from protoboard.domain.components.footprints import DualRow
from protoboard.domain.components.instance import ComponentInstance
from protoboard.domain.schematic.net import Net, PinIdentity
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


class TestPinRefIdentity:
    """PinRef.identity is excluded from eq/hash — a bare lookup ref equals a
    fully-resolved one carrying a shared PinIdentity."""

    def test_identity_excluded_from_eq_and_hash(self) -> None:
        comp = uuid.uuid4()
        ident = PinIdentity(key=(comp, 4))
        bare = PinRef(comp, 4)
        full = PinRef(comp, 4, identity=ident)
        assert bare == full
        assert hash(bare) == hash(full)
        assert len({bare, full}) == 1

    def test_full_ref_dedupes_against_bare_in_set(self) -> None:
        comp = uuid.uuid4()
        ident = PinIdentity(key=(comp, 4))
        s = {PinRef(comp, 4)}
        s.add(PinRef(comp, 4, identity=ident))
        assert len(s) == 1

    def test_shared_identity_propagates_net(self) -> None:
        comp = uuid.uuid4()
        ident = PinIdentity(key=(comp, 4))
        r4 = PinRef(comp, 4, identity=ident)
        r8 = PinRef(comp, 8, identity=ident)
        net_id = uuid.uuid4()
        assert r4.identity is not None
        r4.identity.net_id = net_id
        assert r8.identity is not None and r8.identity.net_id == net_id


def _ic_with_two_gnd() -> ComponentDefinition:
    """8-pin IC where pins 4 and 8 are both 'GND' → one shared node."""
    return ComponentDefinition(
        name="T",
        footprint=DualRow(n_per_row=4),
        pin_names={1: "A", 2: "B", 3: "C", 4: "GND",
                   5: "E", 6: "F", 7: "G", 8: "GND"},
        component_class=ComponentClass.IC,
    )


def _net_id_of(ref: PinRef) -> uuid.UUID | None:
    assert ref.identity is not None
    return ref.identity.net_id


class TestPinIdentityFactory:
    """ComponentInstance is the factory for same-name pin identities."""

    def test_same_name_pins_share_identity(self) -> None:
        inst = ComponentInstance.new("T", "U1", "")
        inst.build_pin_identities(_ic_with_two_gnd())
        r4, r8 = inst.pin_ref(4), inst.pin_ref(8)
        assert r4.identity is not None
        assert r4.identity is r8.identity

    def test_ungrouped_pin_has_no_identity(self) -> None:
        inst = ComponentInstance.new("T", "U1", "")
        inst.build_pin_identities(_ic_with_two_gnd())
        assert inst.pin_ref(1).identity is None

    def test_instance_stays_frozen(self) -> None:
        inst = ComponentInstance.new("T", "U1", "")
        with pytest.raises(dataclasses.FrozenInstanceError):
            inst.refdes = "X"  # type: ignore[misc]

    def test_net_id_mutable_inside_frozen_instance(self) -> None:
        inst = ComponentInstance.new("T", "U1", "")
        inst.build_pin_identities(_ic_with_two_gnd())
        net_id = uuid.uuid4()
        r4 = inst.pin_ref(4)
        assert r4.identity is not None
        r4.identity.net_id = net_id
        # Sibling pin 8 sees it via the shared identity.
        assert _net_id_of(inst.pin_ref(8)) == net_id

    def test_eq_ignores_pin_identities(self) -> None:
        a = ComponentInstance.new("T", "U1", "")
        a.build_pin_identities(_ic_with_two_gnd())
        b = ComponentInstance(id=a.id, definition_ref="T", refdes="U1", value="")
        assert a == b

    def test_rebuild_clears_stale(self) -> None:
        inst = ComponentInstance.new("T", "U1", "")
        inst.build_pin_identities(_ic_with_two_gnd())
        assert 4 in inst.pin_identities
        # Rebuilding with a definition that has no same-name groups clears it.
        defn2 = ComponentDefinition(
            name="T", footprint=DualRow(n_per_row=4),
            pin_names={i: f"S{i}" for i in range(1, 9)},
            component_class=ComponentClass.IC,
        )
        inst.build_pin_identities(defn2)
        assert inst.pin_identities == {}


class TestSchematicMutators:
    """Encapsulated Net.pins mutators also mirror net_id onto PinIdentity."""

    def _setup(self) -> tuple[Schematic, ComponentInstance, uuid.UUID]:
        inst = ComponentInstance.new("T", "U1", "")
        inst.build_pin_identities(_ic_with_two_gnd())
        net_id = uuid.uuid4()
        sch = Schematic(nets={net_id: Net(id=net_id, name="GND", pins=set())})
        return sch, inst, net_id

    def test_assign_one_gnd_pin_propagates_net_id(self) -> None:
        sch, inst, net_id = self._setup()
        sch.assign_pin(inst.pin_ref(4), net_id)
        # Both GND pins now see the net via the shared identity.
        assert _net_id_of(inst.pin_ref(4)) == net_id
        assert _net_id_of(inst.pin_ref(8)) == net_id

    def test_unassign_clears_identity_net(self) -> None:
        sch, inst, net_id = self._setup()
        sch.assign_pin(inst.pin_ref(4), net_id)
        sch.unassign_pin(inst.pin_ref(4))
        assert _net_id_of(inst.pin_ref(8)) is None

    def test_bare_ref_assign_is_noop_on_identity(self) -> None:
        # Assigning via a bare ref (no identity) must not error and leaves
        # Net.pins correct; identity simply isn't touched.
        sch, inst, net_id = self._setup()
        sch.add_pin_to_net(PinRef(inst.id, 4), net_id)
        assert PinRef(inst.id, 4) in sch.nets[net_id].pins
