# SPDX-License-Identifier: MIT
"""Commands for placing and removing components."""

from __future__ import annotations

import uuid
from dataclasses import dataclass

from protoboard.domain.commands.base import Command
from protoboard.domain.layout.layout import Layout
from protoboard.domain.layout.placement import ComponentPlacement


@dataclass
class PlaceComponentCommand(Command[Layout]):
    component_id: uuid.UUID
    placement: ComponentPlacement

    @property
    def description(self) -> str:  # type: ignore[override]
        return f"Place component {self.component_id}"

    def apply(self, state: Layout) -> None:
        state.placements[self.component_id] = self.placement

    def revert(self, state: Layout) -> None:
        del state.placements[self.component_id]


@dataclass
class RemoveComponentCommand(Command[Layout]):
    component_id: uuid.UUID
    _saved: ComponentPlacement | None = None

    @property
    def description(self) -> str:  # type: ignore[override]
        return f"Remove component {self.component_id}"

    def apply(self, state: Layout) -> None:
        # Raises KeyError if not present — intentional.
        saved = state.placements.pop(self.component_id)
        object.__setattr__(self, "_saved", saved)

    def revert(self, state: Layout) -> None:
        if self._saved is not None:
            state.placements[self.component_id] = self._saved
