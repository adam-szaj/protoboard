# SPDX-License-Identifier: MIT
"""Unit tests for net management commands (net_cmds.py)."""

from __future__ import annotations

import uuid
from dataclasses import dataclass, field

from protoboard.domain.commands.net_cmds import (
    AssignPinToNetCommand,
    CreateNetCommand,
    DeleteNetCommand,
    RenameNetCommand,
    UnassignPinCommand,
)
from protoboard.domain.schematic.net import Net, PinRef
from protoboard.domain.schematic.schematic import Schematic


# ---------------------------------------------------------------------------
# Minimal state stub
# ---------------------------------------------------------------------------


@dataclass
class _State:
    schematic: Schematic
    instances: dict = field(default_factory=dict)
    components: dict = field(default_factory=dict)


def _make_state(*nets: Net) -> _State:
    return _State(schematic=Schematic(nets={n.id: n for n in nets}))


def _net(name: str, *pins: PinRef) -> Net:
    return Net(id=uuid.uuid4(), name=name, pins=set(pins))


def _pin(pin_num: int = 1) -> tuple[uuid.UUID, PinRef]:
    comp_id = uuid.uuid4()
    return comp_id, PinRef(component_id=comp_id, pin_number=pin_num)


# ---------------------------------------------------------------------------
# RenameNetCommand
# ---------------------------------------------------------------------------


class TestRenameNetCommand:
    def test_renames_net(self) -> None:
        net = _net("VCC")
        state = _make_state(net)
        cmd = RenameNetCommand(net_id=net.id, new_name="V+")
        cmd.apply(state)
        assert state.schematic.nets[net.id].name == "V+"

    def test_undo_restores_name(self) -> None:
        net = _net("VCC")
        state = _make_state(net)
        cmd = RenameNetCommand(net_id=net.id, new_name="V+")
        cmd.apply(state)
        cmd.revert(state)
        assert state.schematic.nets[net.id].name == "VCC"


# ---------------------------------------------------------------------------
# CreateNetCommand
# ---------------------------------------------------------------------------


class TestCreateNetCommand:
    def test_creates_empty_net(self) -> None:
        state = _make_state()
        cmd = CreateNetCommand(name="GND")
        cmd.apply(state)
        assert any(n.name == "GND" for n in state.schematic.nets.values())

    def test_undo_removes_net(self) -> None:
        state = _make_state()
        cmd = CreateNetCommand(name="GND")
        cmd.apply(state)
        cmd.revert(state)
        assert not any(n.name == "GND" for n in state.schematic.nets.values())

    def test_net_id_is_stable(self) -> None:
        state = _make_state()
        cmd = CreateNetCommand(name="GND")
        cmd.apply(state)
        assert cmd.net_id in state.schematic.nets


# ---------------------------------------------------------------------------
# DeleteNetCommand
# ---------------------------------------------------------------------------


class TestDeleteNetCommand:
    def test_deletes_net(self) -> None:
        net = _net("GND")
        state = _make_state(net)
        cmd = DeleteNetCommand(net_id=net.id)
        cmd.apply(state)
        assert net.id not in state.schematic.nets

    def test_undo_restores_net(self) -> None:
        comp_id, ref = _pin()
        net = _net("GND", ref)
        state = _make_state(net)
        cmd = DeleteNetCommand(net_id=net.id)
        cmd.apply(state)
        cmd.revert(state)
        assert net.id in state.schematic.nets
        assert ref in state.schematic.nets[net.id].pins

    def test_delete_nonexistent_is_noop(self) -> None:
        state = _make_state()
        cmd = DeleteNetCommand(net_id=uuid.uuid4())
        cmd.apply(state)  # must not raise
        cmd.revert(state)  # nothing to restore


# ---------------------------------------------------------------------------
# AssignPinToNetCommand
# ---------------------------------------------------------------------------


class TestAssignPinToNetCommand:
    def test_adds_pin_to_net(self) -> None:
        comp_id, ref = _pin(1)
        net = _net("VCC")
        state = _make_state(net)
        cmd = AssignPinToNetCommand(
            component_id=comp_id, pin_number=1, target_net_id=net.id
        )
        cmd.apply(state)
        assert ref in state.schematic.nets[net.id].pins

    def test_moves_pin_from_old_net(self) -> None:
        comp_id, ref = _pin(1)
        comp_id2, ref2 = _pin(2)
        old = _net("OLD", ref, ref2)  # two pins — old net survives
        new = _net("NEW")
        state = _make_state(old, new)
        cmd = AssignPinToNetCommand(
            component_id=comp_id, pin_number=1, target_net_id=new.id
        )
        cmd.apply(state)
        assert ref not in state.schematic.nets[old.id].pins
        assert ref in state.schematic.nets[new.id].pins

    def test_undo_moves_pin_back(self) -> None:
        comp_id, ref = _pin(1)
        old = _net("OLD", ref)
        new = _net("NEW")
        state = _make_state(old, new)
        cmd = AssignPinToNetCommand(
            component_id=comp_id, pin_number=1, target_net_id=new.id
        )
        cmd.apply(state)
        cmd.revert(state)
        assert ref in state.schematic.nets[old.id].pins
        assert ref not in state.schematic.nets[new.id].pins

    def test_deletes_empty_source_net_on_apply(self) -> None:
        comp_id, ref = _pin(1)
        old = _net("OLD", ref)  # single pin → will become empty
        new = _net("NEW")
        state = _make_state(old, new)
        cmd = AssignPinToNetCommand(
            component_id=comp_id, pin_number=1, target_net_id=new.id
        )
        cmd.apply(state)
        assert old.id not in state.schematic.nets


class TestAssignPinToNetPropagation:
    """Assigning one same-name pin (GND) assigns all its siblings, because they
    share one PinIdentity / electrical node."""

    def _state_with_ic(self) -> tuple[_State, uuid.UUID, Net]:
        from protoboard.domain.components.definition import (
            ComponentClass,
            ComponentDefinition,
        )
        from protoboard.domain.components.footprints import DualRow
        from protoboard.domain.components.instance import ComponentInstance

        defn = ComponentDefinition(
            name="IC", footprint=DualRow(n_per_row=4),
            pin_names={1: "A", 2: "B", 3: "C", 4: "GND",
                       5: "E", 6: "F", 7: "G", 8: "GND"},
            component_class=ComponentClass.IC,
        )
        inst = ComponentInstance.new("IC", "U1", "")
        inst.build_pin_identities(defn)
        net = _net("GND")
        state = _State(
            schematic=Schematic(nets={net.id: net}),
            instances={inst.id: inst},
            components={"IC": defn},
        )
        return state, inst.id, net

    def test_propagation_builds_identities_on_demand(self) -> None:
        # Regression: an instance placed before identities were wired (empty
        # pin_identities) must still propagate — the command builds them.
        from protoboard.domain.components.definition import (
            ComponentClass,
            ComponentDefinition,
        )
        from protoboard.domain.components.footprints import DualRow
        from protoboard.domain.components.instance import ComponentInstance

        defn = ComponentDefinition(
            name="IC", footprint=DualRow(n_per_row=4),
            pin_names={1: "A", 2: "B", 3: "C", 4: "GND",
                       5: "E", 6: "F", 7: "G", 8: "GND"},
            component_class=ComponentClass.IC,
        )
        inst = ComponentInstance.new("IC", "U1", "")  # NO build_pin_identities
        assert inst.pin_identities == {}
        net = _net("GND")
        state = _State(
            schematic=Schematic(nets={net.id: net}),
            instances={inst.id: inst},
            components={"IC": defn},
        )
        AssignPinToNetCommand(
            component_id=inst.id, pin_number=4, target_net_id=net.id
        ).apply(state)
        pin_numbers = {r.pin_number for r in state.schematic.nets[net.id].pins}
        assert pin_numbers == {4, 8}

    def test_assigning_one_gnd_adds_all_gnd_siblings(self) -> None:
        state, comp_id, net = self._state_with_ic()
        AssignPinToNetCommand(
            component_id=comp_id, pin_number=4, target_net_id=net.id
        ).apply(state)
        pin_numbers = {r.pin_number for r in state.schematic.nets[net.id].pins}
        assert pin_numbers == {4, 8}, "both GND pins must join the net"

    def test_net_pins_and_identity_consistent(self) -> None:
        state, comp_id, net = self._state_with_ic()
        AssignPinToNetCommand(
            component_id=comp_id, pin_number=4, target_net_id=net.id
        ).apply(state)
        inst = state.instances[comp_id]
        assert inst.pin_ref(4).identity.net_id == net.id  # type: ignore[union-attr]
        assert inst.pin_ref(8).identity.net_id == net.id  # type: ignore[union-attr]

    def test_undo_removes_all_siblings(self) -> None:
        state, comp_id, net = self._state_with_ic()
        cmd = AssignPinToNetCommand(
            component_id=comp_id, pin_number=4, target_net_id=net.id
        )
        cmd.apply(state)
        assert {r.pin_number for r in state.schematic.nets[net.id].pins} == {4, 8}
        cmd.revert(state)
        # Net pre-existed empty; after undo both GND siblings are gone again.
        assert state.schematic.nets[net.id].pins == set()
        # Identity net cleared for both siblings too.
        inst = state.instances[comp_id]
        assert inst.pin_ref(4).identity.net_id is None  # type: ignore[union-attr]
        assert inst.pin_ref(8).identity.net_id is None  # type: ignore[union-attr]

    def test_non_grouped_pin_assigns_only_itself(self) -> None:
        state, comp_id, net = self._state_with_ic()
        AssignPinToNetCommand(
            component_id=comp_id, pin_number=2, target_net_id=net.id  # "B", unique
        ).apply(state)
        pin_numbers = {r.pin_number for r in state.schematic.nets[net.id].pins}
        assert pin_numbers == {2}


# ---------------------------------------------------------------------------
# UnassignPinCommand
# ---------------------------------------------------------------------------


class TestUnassignPinCommand:
    def test_removes_pin_from_net(self) -> None:
        comp_id, ref = _pin(2)
        comp_id2, ref2 = _pin(3)
        net = _net("VCC", ref, ref2)
        state = _make_state(net)
        cmd = UnassignPinCommand(component_id=comp_id, pin_number=2)
        cmd.apply(state)
        assert ref not in state.schematic.nets[net.id].pins
        assert ref2 in state.schematic.nets[net.id].pins

    def test_deletes_net_when_last_pin_removed(self) -> None:
        comp_id, ref = _pin(1)
        net = _net("VCC", ref)
        state = _make_state(net)
        cmd = UnassignPinCommand(component_id=comp_id, pin_number=1)
        cmd.apply(state)
        assert net.id not in state.schematic.nets

    def test_undo_restores_pin(self) -> None:
        comp_id, ref = _pin(1)
        net = _net("VCC", ref)
        state = _make_state(net)
        cmd = UnassignPinCommand(component_id=comp_id, pin_number=1)
        cmd.apply(state)
        cmd.revert(state)
        assert net.id in state.schematic.nets
        assert ref in state.schematic.nets[net.id].pins

    def test_noop_when_pin_has_no_net(self) -> None:
        comp_id, _ = _pin(1)
        state = _make_state()
        cmd = UnassignPinCommand(component_id=comp_id, pin_number=1)
        cmd.apply(state)  # must not raise
        cmd.revert(state)  # nothing to restore
