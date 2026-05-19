# SPDX-License-Identifier: MIT
"""Headless state container used by the replay tooling and tests.

:class:`ReplayState` carries the minimal attributes that every recorded
command expects to find on its state object — ``board``, ``layout``,
``schematic``, ``instances``, ``components``.  It satisfies the
ComponentState, SchematicState and NetState protocols structurally without
importing Qt.
"""

from __future__ import annotations

import uuid
from dataclasses import dataclass, field
from pathlib import Path

from protoboard.domain.board.board import Board
from protoboard.domain.components.definition import ComponentDefinition
from protoboard.domain.components.instance import ComponentInstance
from protoboard.domain.layout.layout import Layout
from protoboard.domain.schematic.schematic import Schematic
from protoboard.services.library.loader import ComponentLibrary
from protoboard.services.persistence.project_yaml import load_project


@dataclass
class ReplayState:
    """Minimal mutable state container for headless command replay."""

    board: Board
    layout: Layout
    schematic: Schematic
    instances: dict[uuid.UUID, ComponentInstance] = field(default_factory=dict)
    components: dict[str, ComponentDefinition] = field(default_factory=dict)

    @classmethod
    def from_project(
        cls,
        project_dir: Path,
        library: ComponentLibrary,
    ) -> "ReplayState":
        """Load a project directory and bind it to a fresh ReplayState."""
        board, layout, schematic, instances, *_rest = load_project(project_dir)
        components = {name: library.get(name) for name in library.names()}
        return cls(
            board=board,
            layout=layout,
            schematic=schematic,
            instances=instances,
            components=components,
        )


__all__ = ["ReplayState"]
