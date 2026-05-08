# SPDX-License-Identifier: MIT
"""Commands for net management: create, delete, rename, assign/unassign pins."""

from __future__ import annotations

import uuid
from dataclasses import dataclass, field
from typing import Protocol

from protoboard.domain.commands.base import Command
from protoboard.domain.schematic.schematic import Schematic


class NetState(Protocol):
    """Minimal state needed by net-management commands."""

    schematic: Schematic


# ---------------------------------------------------------------------------
# RenameNetCommand
# ---------------------------------------------------------------------------


@dataclass
class RenameNetCommand(Command[NetState]):
    """Rename an existing net."""

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
    _saved_pins: object = field(default=None, init=False, repr=False)  # set[PinRef]|None

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
            pins=set(self._saved_pins),  # type: ignore[arg-type]
        )


# ---------------------------------------------------------------------------
# AssignPinToNetCommand
# ---------------------------------------------------------------------------


@dataclass
class AssignPinToNetCommand(Command[NetState]):
    """Move a pin to a target net (removes it from its current net first)."""

    component_id: uuid.UUID
    pin_number: int
    target_net_id: uuid.UUID
    _source_net_id: uuid.UUID | None = field(default=None, init=False, repr=False)
    _source_net_name: str = field(default="", init=False, repr=False)

    @property
    def description(self) -> str:  # type: ignore[override]
        return f"Assign pin {self.pin_number} to net"

    def apply(self, state: NetState) -> None:
        from protoboard.domain.schematic.net import Net, PinRef

        ref = PinRef(component_id=self.component_id, pin_number=self.pin_number)

        # Remove from current net (if any).
        for net in list(state.schematic.nets.values()):
            if ref in net.pins:
                self._source_net_id = net.id
                self._source_net_name = net.name
                net.pins.discard(ref)
                if not net.pins:
                    del state.schematic.nets[net.id]
                break

        # Add to target net.
        state.schematic.nets[self.target_net_id].pins.add(ref)

    def revert(self, state: NetState) -> None:
        from protoboard.domain.schematic.net import Net, PinRef

        ref = PinRef(component_id=self.component_id, pin_number=self.pin_number)

        # Remove from target net.
        target = state.schematic.nets.get(self.target_net_id)
        if target is not None:
            target.pins.discard(ref)

        # Restore to source net (if any).
        if self._source_net_id is not None:
            src = state.schematic.nets.get(self._source_net_id)
            if src is not None:
                src.pins.add(ref)
            else:
                # Net was deleted when the pin was removed — recreate it.
                state.schematic.nets[self._source_net_id] = Net(
                    id=self._source_net_id,
                    name=self._source_net_name,
                    pins={ref},
                )


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
    _saved_pins: object = field(default=None, init=False, repr=False)  # set[PinRef]|None

    @property
    def description(self) -> str:  # type: ignore[override]
        return f"Unassign pin {self.pin_number} from net"

    def apply(self, state: NetState) -> None:
        from protoboard.domain.schematic.net import PinRef

        ref = PinRef(component_id=self.component_id, pin_number=self.pin_number)
        net = state.schematic.net_for_pin(ref)
        if net is None:
            return
        self._saved_net_id = net.id
        self._saved_net_name = net.name
        self._saved_pins = net.pins.copy()
        net.pins.discard(ref)
        if not net.pins:
            del state.schematic.nets[net.id]

    def revert(self, state: NetState) -> None:
        from protoboard.domain.schematic.net import Net, PinRef

        if self._saved_net_id is None or self._saved_pins is None:
            return
        ref = PinRef(component_id=self.component_id, pin_number=self.pin_number)
        net = state.schematic.nets.get(self._saved_net_id)
        if net is not None:
            net.pins.add(ref)
        else:
            state.schematic.nets[self._saved_net_id] = Net(
                id=self._saved_net_id,
                name=self._saved_net_name,
                pins=set(self._saved_pins),  # type: ignore[arg-type]
            )
