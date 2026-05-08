# SPDX-License-Identifier: MIT
"""EditorSession — mutable state shared across all GUI panels.

Holds the current Board, Layout, Schematic and CommandStack. GUI
components observe it via Qt signals and mutate it only through Commands.
"""

from __future__ import annotations

import uuid
from pathlib import Path

from PySide6.QtCore import QObject, Signal

from protoboard.domain.board.board import Board
from protoboard.domain.commands.base import CommandStack
from protoboard.domain.commands.component_cmds import ComponentState
from protoboard.domain.components.definition import ComponentDefinition
from protoboard.domain.components.instance import ComponentInstance
from protoboard.domain.connectivity.graph import ConnectivityGraph
from protoboard.domain.layout.layout import Layout
from protoboard.domain.schematic.schematic import Schematic
from protoboard.services.drc.engine import DRCEngine, DRCIssue
from protoboard.domain.components.body_type import BodyTypeSpec
from protoboard.services.library.body_type_loader import load_body_types
from protoboard.services.library.loader import ComponentLibrary
from protoboard.domain.routing_settings import RoutingSettings


class EditorSession(QObject):
    """Central state for one open project."""

    layout_changed = Signal()
    drag_moved = Signal()       # emitted on every drag step; does NOT trigger panels/DRC
    drc_changed = Signal(list)  # list[DRCIssue]
    dirty_changed = Signal(bool)
    # Carries (frozenset[UUID] comp_ids, frozenset[UUID] wire_ids, UUID|None net_id).
    # Emitted by SelectTool after any selection change so panels can sync.
    selection_changed = Signal(object, object, object)

    def __init__(
        self,
        board: Board,
        library: ComponentLibrary,
        parent: QObject | None = None,
    ) -> None:
        super().__init__(parent)
        self.board = board
        self.library = library
        self.schematic = Schematic(nets={})
        self.layout = Layout()
        self.layout.ensure_root_group()
        # Pre-populate components from library so renderers can look up definitions.
        self.components: dict[str, ComponentDefinition] = {
            name: library.get(name) for name in library.names()
        }
        self.instances: dict[uuid.UUID, ComponentInstance] = {}
        self.body_types: dict[str, BodyTypeSpec] = load_body_types()
        self.stack: CommandStack[ComponentState] = CommandStack()
        self.routing_settings: RoutingSettings = RoutingSettings()
        self.project_path: Path | None = None
        self._drc_issues: list[DRCIssue] = []
        self._dirty: bool = False
        self._graph: ConnectivityGraph | None = None  # rebuilt lazily; None = stale

    def notify_selection(
        self,
        comp_ids: "frozenset[uuid.UUID]",
        wire_ids: "frozenset[uuid.UUID]",
        net_id: "uuid.UUID | None",
    ) -> None:
        """Called by SelectTool to broadcast selection state to all panels."""
        self.selection_changed.emit(comp_ids, wire_ids, net_id)

    def invalidate_graph(self) -> None:
        """Mark the cached ConnectivityGraph as stale. Called on every layout_changed."""
        self._graph = None

    def connectivity_graph(self) -> ConnectivityGraph:
        """Return the cached graph, rebuilding it if stale."""
        if self._graph is None:
            self._graph = ConnectivityGraph(
                board=self.board,
                layout=self.layout,
                components=self.components,
                instances=self.instances,
            )
        return self._graph

    def run_drc(self) -> list[DRCIssue]:
        engine = DRCEngine()
        self._drc_issues = engine.check_with_graph(
            schematic=self.schematic,
            layout=self.layout,
            components=self.components,
            instances=self.instances,
            graph=self.connectivity_graph(),
        )
        self.drc_changed.emit(self._drc_issues)
        return self._drc_issues

    @property
    def drc_issues(self) -> list[DRCIssue]:
        return self._drc_issues

    @property
    def is_dirty(self) -> bool:
        return self._dirty

    def mark_dirty(self) -> None:
        if not self._dirty:
            self._dirty = True
            self.dirty_changed.emit(True)

    def mark_clean(self) -> None:
        if self._dirty:
            self._dirty = False
            self.dirty_changed.emit(False)
