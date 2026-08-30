# SPDX-License-Identifier: MIT
"""Commands for net management: create, delete, rename, assign/unassign pins."""

from __future__ import annotations

import uuid
from dataclasses import dataclass, field
from typing import TYPE_CHECKING, Protocol

from protoboard.domain.commands.base import Command
from protoboard.domain.schematic.schematic import Schematic

if TYPE_CHECKING:
    from protoboard.domain.components.definition import ComponentDefinition
    from protoboard.domain.components.instance import ComponentInstance
    from protoboard.domain.schematic.net import PinRef
    from protoboard.domain.schematic.schematic import PinGroupUndo


class NetState(Protocol):
    """State needed by net-management commands.

    ``instances`` / ``components`` let assignment commands resolve a pin's
    same-name siblings (shared :class:`PinIdentity`) so a net assigned to one
    pin propagates to all of them. The runtime ``EditorSession`` satisfies this.
    """

    schematic: Schematic
    instances: dict[uuid.UUID, ComponentInstance]
    components: dict[str, ComponentDefinition]


# ---------------------------------------------------------------------------
# RenameNetCommand
# ---------------------------------------------------------------------------


@dataclass
class RenameNetCommand(Command[NetState]):
    """Rename an existing net."""

    allowed_during_autoroute = True  # name doesn't enter the routing snapshot

    net_id: uuid.UUID
    new_name: str
    _old_name: str = field(default="", init=False, repr=False)

    @property
    def description(self) -> str:  # type: ignore[override]
        return f"Rename net to '{self.new_name}'"

    def apply(self, state: NetState) -> None:
        net = state.schematic.nets[self.net_id]
        self._old_name = net.name
        net.name = self.new_name

    def revert(self, state: NetState) -> None:
        state.schematic.nets[self.net_id].name = self._old_name


# ---------------------------------------------------------------------------
# SetNetGlobalCommand
# ---------------------------------------------------------------------------


@dataclass
class SetNetGlobalCommand(Command[NetState]):
    """Mark or unmark a net as global (excluded from hug/spread neighbour graph)."""

    # Affects only *future* routing; the in-flight worker already has
    # its snapshot, so flipping this flag now is safe.
    allowed_during_autoroute = True

    net_id: uuid.UUID
    global_net: bool
    _old_global: bool = field(default=False, init=False, repr=False)

    @property
    def description(self) -> str:  # type: ignore[override]
        return "Mark net as global" if self.global_net else "Unmark net as global"

    def apply(self, state: NetState) -> None:
        net = state.schematic.nets.get(self.net_id)
        if net is None:
            return
        self._old_global = net.global_net
        net.global_net = self.global_net

    def revert(self, state: NetState) -> None:
        net = state.schematic.nets.get(self.net_id)
        if net is None:
            return
        net.global_net = self._old_global


# SetNetColorCommand
# ---------------------------------------------------------------------------


@dataclass
class SetNetColorCommand(Command[NetState]):
    """Set (or clear) the display colour of a net."""

    # Pure display attribute — never feeds the routing snapshot.
    allowed_during_autoroute = True

    net_id: uuid.UUID
    new_color: str | None  # hex string or None to revert to auto palette
    _old_color: str | None = field(default=None, init=False, repr=False)

    @property
    def description(self) -> str:  # type: ignore[override]
        return "Set net color" if self.new_color else "Clear net color"

    def apply(self, state: NetState) -> None:
        net = state.schematic.nets[self.net_id]
        self._old_color = net.color
        net.color = self.new_color

    def revert(self, state: NetState) -> None:
        state.schematic.nets[self.net_id].color = self._old_color


# ---------------------------------------------------------------------------
# CreateNetCommand
# ---------------------------------------------------------------------------


@dataclass
class CreateNetCommand(Command[NetState]):
    """Create a new, empty net with a given name."""

    name: str
    net_id: uuid.UUID = field(default_factory=uuid.uuid4)

    @property
    def description(self) -> str:  # type: ignore[override]
        return f"Create net '{self.name}'"

    def apply(self, state: NetState) -> None:
        from protoboard.domain.schematic.net import Net

        state.schematic.nets[self.net_id] = Net(id=self.net_id, name=self.name)

    def revert(self, state: NetState) -> None:
        state.schematic.nets.pop(self.net_id, None)


# ---------------------------------------------------------------------------
# DeleteNetCommand
# ---------------------------------------------------------------------------


@dataclass
class DeleteNetCommand(Command[NetState]):
    """Delete a net (removes all its pins with it)."""

    net_id: uuid.UUID
    _saved_name: str = field(default="", init=False, repr=False)
    _saved_pins: set[PinRef] | None = field(default=None, init=False, repr=False)

    @property
    def description(self) -> str:  # type: ignore[override]
        return "Delete net"

    def apply(self, state: NetState) -> None:
        net = state.schematic.nets.pop(self.net_id, None)
        if net is not None:
            self._saved_name = net.name
            self._saved_pins = net.pins.copy()

    def revert(self, state: NetState) -> None:
        from protoboard.domain.schematic.net import Net

        if self._saved_pins is None:
            return
        state.schematic.nets[self.net_id] = Net(
            id=self.net_id,
            name=self._saved_name,
            pins=set(self._saved_pins),
        )


# ---------------------------------------------------------------------------
# AssignPinToNetCommand
# ---------------------------------------------------------------------------


@dataclass
class AssignPinToNetCommand(Command[NetState]):
    """Move a pin to a target net (removes it from its current net first).

    Acts on the whole same-name sibling group — assigning one ``GND`` assigns
    every ``GND`` of the component (they are one electrical node). Sibling
    expansion and revert both live in the shared schematic layer, not here.
    """

    component_id: uuid.UUID
    pin_number: int
    target_net_id: uuid.UUID
    _undo: PinGroupUndo | None = field(default=None, init=False, repr=False)

    @property
    def description(self) -> str:  # type: ignore[override]
        return f"Assign pin {self.pin_number} to net"

    def apply(self, state: NetState) -> None:
        from protoboard.domain.schematic.pin_siblings import same_name_refs

        refs = same_name_refs(
            self.component_id, self.pin_number, state.instances, state.components
        )
        self._undo = state.schematic.assign_pins(refs, self.target_net_id)

    def revert(self, state: NetState) -> None:
        if self._undo is not None:
            state.schematic.restore(self._undo)


# ---------------------------------------------------------------------------
# UnassignPinCommand
# ---------------------------------------------------------------------------


@dataclass
class UnassignPinCommand(Command[NetState]):
    """Remove a pin from its net."""

    component_id: uuid.UUID
    pin_number: int
    _saved_net_id: uuid.UUID | None = field(default=None, init=False, repr=False)
    _saved_net_name: str = field(default="", init=False, repr=False)
    _saved_pins: set[PinRef] | None = field(default=None, init=False, repr=False)

    @property
    def description(self) -> str:  # type: ignore[override]
        return f"Unassign pin {self.pin_number} from net"

    def apply(self, state: NetState) -> None:
        from protoboard.domain.schematic.net import PinRef

        ref = PinRef(component_id=self.component_id, pin_number=self.pin_number)
        removed = state.schematic.unassign_pin(ref)
        if removed is None:
            return
        self._saved_net_id, self._saved_net_name, self._saved_pins = removed

    def revert(self, state: NetState) -> None:
        from protoboard.domain.schematic.net import Net, PinRef

        if self._saved_net_id is None or self._saved_pins is None:
            return
        ref = PinRef(component_id=self.component_id, pin_number=self.pin_number)
        net = state.schematic.nets.get(self._saved_net_id)
        if net is not None:
            state.schematic.add_pin_to_net(ref, self._saved_net_id)
        else:
            state.schematic.nets[self._saved_net_id] = Net(
                id=self._saved_net_id,
                name=self._saved_net_name,
                pins=set(self._saved_pins),
            )
