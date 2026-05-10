# SPDX-License-Identifier: MIT
"""EditorSession — mutable state shared across all GUI panels.

Holds the current Board, Layout, Schematic and CommandStack. GUI
components observe it via Qt signals and mutate it only through Commands.
"""

from __future__ import annotations

import uuid
from collections import defaultdict
from pathlib import Path

from PySide6.QtCore import QObject, Signal

from protoboard.domain.board.board import Board
from protoboard.domain.commands.base import CommandStack
from protoboard.domain.commands.component_cmds import ComponentState
from protoboard.domain.components.definition import ComponentDefinition
from protoboard.domain.components.instance import ComponentInstance
from protoboard.domain.connectivity.graph import ConnectivityGraph
from protoboard.domain.layout.layout import Layout
from protoboard.domain.layout.placement import pin_world_position
from protoboard.domain.schematic.schematic import Schematic
from protoboard.services.drc.engine import DRCEngine, DRCIssue
from protoboard.domain.components.body_type import BodyTypeSpec
from protoboard.services.library.body_type_loader import load_body_types
from protoboard.services.library.loader import ComponentLibrary
from protoboard.domain.routing_settings import RoutingSettings
from protoboard.gui.layer_state import LayerOverrides


def _build_comp_spatial_index(
    layout: Layout,
    instances: dict[uuid.UUID, ComponentInstance],
    components: dict[str, ComponentDefinition],
) -> dict[tuple[int, int], list[uuid.UUID]]:
    """Build grid-cell → comp_id index from current placements."""
    idx: dict[tuple[int, int], list[uuid.UUID]] = defaultdict(list)
    for comp_id, placement in layout.placements.items():
        inst = instances.get(comp_id)
        if inst is None:
            continue
        defn = components.get(inst.definition_ref)
        if defn is None:
            continue
        seen_cells: set[tuple[int, int]] = set()
        for n in range(defn.footprint.pin_count()):
            p = pin_world_position(defn.footprint, placement, n + 1)
            cell = (round(p.x), round(p.y))
            if cell not in seen_cells:
                seen_cells.add(cell)
                idx[cell].append(comp_id)
    return dict(idx)


def _build_wire_spatial_index(layout: Layout) -> dict[tuple[int, int], list[uuid.UUID]]:
    """Build grid-cell → wire_id index from current wires."""
    idx: dict[tuple[int, int], list[uuid.UUID]] = defaultdict(list)
    for wire in layout.wires:
        seen_cells: set[tuple[int, int]] = set()
        for pt in wire.path:
            cell = (round(pt.x), round(pt.y))
            if cell not in seen_cells:
                seen_cells.add(cell)
                idx[cell].append(wire.id)
    return dict(idx)


class EditorSession(QObject):
    """Central state for one open project."""

    layout_changed = Signal()
    drag_moved = Signal()       # emitted on every drag step; does NOT trigger panels/DRC
    drc_changed = Signal(list)  # list[DRCIssue]
    dirty_changed = Signal(bool)
    # Carries (frozenset[UUID] comp_ids, frozenset[UUID] wire_ids, UUID|None net_id).
    # Emitted by SelectTool after any selection change so panels can sync.
    selection_changed = Signal(object, object, object)
    # Emitted by SelectTool when the interaction mode changes (carries mode label str).
    highlight_mode_changed = Signal(str)

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
        self.layer_overrides: LayerOverrides = LayerOverrides()
        self.project_path: Path | None = None
        self._drc_issues: list[DRCIssue] = []
        self._dirty: bool = False
        self._graph: ConnectivityGraph | None = None  # rebuilt lazily; None = stale
        self._comp_spatial_index: dict[tuple[int, int], list[uuid.UUID]] | None = None
        self._wire_spatial_index: dict[tuple[int, int], list[uuid.UUID]] | None = None

    def notify_selection(
        self,
        comp_ids: "frozenset[uuid.UUID]",
        wire_ids: "frozenset[uuid.UUID]",
        net_id: "uuid.UUID | None",
    ) -> None:
        """Called by SelectTool to broadcast selection state to all panels."""
        self.selection_changed.emit(comp_ids, wire_ids, net_id)

    def notify_highlight_mode(self, label: str) -> None:
        """Called by SelectTool when the interaction mode changes."""
        self.highlight_mode_changed.emit(label)

    def invalidate_graph(self) -> None:
        """Mark the cached ConnectivityGraph and spatial indexes as stale."""
        self._graph = None
        self._comp_spatial_index = None
        self._wire_spatial_index = None

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

    def comp_spatial_index(self) -> dict[tuple[int, int], list[uuid.UUID]]:
        """Return grid-cell → comp_id index, rebuilding lazily after layout_changed."""
        if self._comp_spatial_index is None:
            self._comp_spatial_index = _build_comp_spatial_index(
                self.layout, self.instances, self.components
            )
        return self._comp_spatial_index

    def wire_spatial_index(self) -> dict[tuple[int, int], list[uuid.UUID]]:
        """Return grid-cell → wire_id index, rebuilding lazily after layout_changed."""
        if self._wire_spatial_index is None:
            self._wire_spatial_index = _build_wire_spatial_index(self.layout)
        return self._wire_spatial_index

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
