# SPDX-License-Identifier: MIT
"""Commands for placing, moving and removing components.

All commands operate on a ComponentState protocol so that both
Layout.placements and session.instances stay in sync through undo/redo,
without creating a hard dependency on the GUI layer.
"""

from __future__ import annotations

import uuid
from dataclasses import dataclass
from typing import Protocol

from protoboard.domain.board.board import Board
from protoboard.domain.commands.base import Command
from protoboard.domain.components.instance import ComponentInstance
from protoboard.domain.layout.layout import Layout
from protoboard.domain.layout.placement import ComponentPlacement
from protoboard.domain.layout.wire import WireSegment


class ComponentState(Protocol):
    """Minimal state needed by all board/component commands."""

    board: Board
    layout: Layout
    instances: dict[uuid.UUID, ComponentInstance]


@dataclass
class PlaceComponentCommand(Command[ComponentState]):
    component_id: uuid.UUID
    placement: ComponentPlacement
    instance: ComponentInstance

    @property
    def description(self) -> str:  # type: ignore[override]
        return f"Place {self.instance.refdes}"

    def apply(self, state: ComponentState) -> None:
        state.layout.placements[self.component_id] = self.placement
        state.instances[self.component_id] = self.instance

    def revert(self, state: ComponentState) -> None:
        del state.layout.placements[self.component_id]
        state.instances.pop(self.component_id, None)


@dataclass
class MoveComponentCommand(Command[ComponentState]):
    component_id: uuid.UUID
    new_placement: ComponentPlacement
    old_placement: ComponentPlacement

    @property
    def description(self) -> str:  # type: ignore[override]
        return f"Move component {self.component_id}"

    def apply(self, state: ComponentState) -> None:
        state.layout.placements[self.component_id] = self.new_placement

    def revert(self, state: ComponentState) -> None:
        state.layout.placements[self.component_id] = self.old_placement


@dataclass
class RemoveComponentCommand(Command[ComponentState]):
    component_id: uuid.UUID
    _saved_placement: ComponentPlacement | None = None
    _saved_instance: ComponentInstance | None = None

    @property
    def description(self) -> str:  # type: ignore[override]
        return f"Remove component {self.component_id}"

    def apply(self, state: ComponentState) -> None:
        saved = state.layout.placements.pop(self.component_id)
        object.__setattr__(self, "_saved_placement", saved)
        inst = state.instances.pop(self.component_id, None)
        object.__setattr__(self, "_saved_instance", inst)

    def revert(self, state: ComponentState) -> None:
        if self._saved_placement is not None:
            state.layout.placements[self.component_id] = self._saved_placement
        if self._saved_instance is not None:
            state.instances[self.component_id] = self._saved_instance


@dataclass
class AddWireCommand(Command[ComponentState]):
    wire: WireSegment

    @property
    def description(self) -> str:  # type: ignore[override]
        return "Add wire"

    def apply(self, state: ComponentState) -> None:
        state.layout.wires.append(self.wire)

    def revert(self, state: ComponentState) -> None:
        state.layout.wires.remove(self.wire)
