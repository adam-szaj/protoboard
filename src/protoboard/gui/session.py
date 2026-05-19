# SPDX-License-Identifier: MIT
"""EditorSession — mutable state shared across all GUI panels.

Holds the current Board, Layout, Schematic and CommandStack. GUI
components observe it via Qt signals and mutate it only through Commands.
"""

from __future__ import annotations

import time
import uuid
from collections import defaultdict
from dataclasses import dataclass, field
from pathlib import Path
from typing import TYPE_CHECKING

if TYPE_CHECKING:
    from protoboard.domain.board.pad_state import PadIndex

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
from protoboard.geometry import Point
from protoboard.services.drc.engine import DRCEngine, DRCIssue
from protoboard.domain.components.body_type import BodyTypeSpec
from protoboard.services.library.body_type_loader import load_body_types
from protoboard.services.library.loader import ComponentLibrary
from protoboard.domain.routing_settings import RoutingSettings
from protoboard.gui.layer_state import LayerOverrides
from protoboard.gui.highlight_state import HighlightStack


@dataclass
class PerfMetrics:
    """Rolling performance metrics updated by instrumented code paths."""

    refresh_ms: float = 0.0
    drc_ms: float = 0.0
    route_ms: float = 0.0
    graph_build_ms: float = 0.0
    pad_index_build_ms: float = 0.0
    scene_items: int = 0
    wire_count: int = 0
    component_count: int = 0
    drc_issue_count: int = 0
    # History (last N samples) for sparkline display.
    refresh_history: list[float] = field(default_factory=list)
    route_history: list[float] = field(default_factory=list)
    _history_max: int = field(default=30, repr=False)

    def record_refresh(self, ms: float) -> None:
        self.refresh_ms = ms
        self.refresh_history.append(ms)
        if len(self.refresh_history) > self._history_max:
            self.refresh_history.pop(0)

    def record_route(self, ms: float) -> None:
        self.route_ms = ms
        self.route_history.append(ms)
        if len(self.route_history) > self._history_max:
            self.route_history.pop(0)


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
    # Emitted by RouteTool when it starts/ends routing a specific net.
    # Carries the net UUID or None.  BoardView additively highlights this net
    # on top of any active HighlightStack entry (union semantics).
    routing_net_changed = Signal(object)

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
        self.highlight_stack: HighlightStack = HighlightStack(self)
        self._routing_net_id: uuid.UUID | None = None
        self.project_path: Path | None = None
        self._drc_issues: list[DRCIssue] = []
        self._dirty: bool = False
        self._graph: ConnectivityGraph | None = None  # rebuilt lazily; None = stale
        self._pad_index: "PadIndex | None" = None
        self._comp_spatial_index: dict[tuple[int, int], list[uuid.UUID]] | None = None
        self._wire_spatial_index: dict[tuple[int, int], list[uuid.UUID]] | None = None
        self.perf: PerfMetrics = PerfMetrics()

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

    def set_routing_net(self, net_id: uuid.UUID | None) -> None:
        """Mark a net as being actively routed (RouteTool while drawing).

        BoardView listens to ``routing_net_changed`` and unions the net's
        comps/wires with whatever the HighlightStack is currently showing.
        Pass None to clear.
        """
        if net_id == self._routing_net_id:
            return
        self._routing_net_id = net_id
        self.routing_net_changed.emit(net_id)

    @property
    def routing_net_id(self) -> "uuid.UUID | None":
        return self._routing_net_id

    def invalidate_graph(self) -> None:
        """Mark the cached ConnectivityGraph, PadIndex and spatial indexes as stale."""
        self._graph = None
        self._pad_index = None
        self._comp_spatial_index = None
        self._wire_spatial_index = None

    def connectivity_graph(self) -> ConnectivityGraph:
        """Return the cached graph, rebuilding it if stale."""
        if self._graph is None:
            t0 = time.monotonic()
            self._graph = ConnectivityGraph(
                board=self.board,
                layout=self.layout,
                components=self.components,
                instances=self.instances,
            )
            self.perf.graph_build_ms = (time.monotonic() - t0) * 1000
        return self._graph

    def pad_index(self) -> "PadIndex":
        """Return the cached PadIndex, rebuilding it if stale.

        PadIndex is the single source of truth for per-pad occupancy
        (electrical net + physical blockers) used by simplify, autoroute,
        drag-conflict and DRC.  Lifetime parallels ``connectivity_graph()``.
        """
        if self._pad_index is None:
            from protoboard.domain.board.pad_state import PadIndex
            t0 = time.monotonic()
            self._pad_index = PadIndex(
                board=self.board,
                layout=self.layout,
                schematic=self.schematic,
                components=self.components,
                instances=self.instances,
            )
            self.perf.pad_index_build_ms = (time.monotonic() - t0) * 1000
        return self._pad_index

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
        t0 = time.monotonic()
        engine = DRCEngine()
        self._drc_issues = engine.check_with_graph(
            schematic=self.schematic,
            layout=self.layout,
            components=self.components,
            instances=self.instances,
            graph=self.connectivity_graph(),
        )
        self.perf.drc_ms = (time.monotonic() - t0) * 1000
        self.perf.drc_issue_count = len(self._drc_issues)
        self.perf.wire_count = len(self.layout.wires)
        self.perf.component_count = len(self.instances)
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

    # ------------------------------------------------------------------
    # Convenience helpers for component/pin lookups
    # ------------------------------------------------------------------

    def get_pin_position(
        self,
        comp_id: uuid.UUID,
        pin_number: int,
    ) -> Point | None:
        """Return the world position of *pin_number* on *comp_id*, or None.

        Returns None if the component or its definition is not found, or if
        *pin_number* is out of range for the footprint.
        """
        placement = self.layout.placements.get(comp_id)
        inst = self.instances.get(comp_id)
        if placement is None or inst is None:
            return None
        defn = self.components.get(inst.definition_ref)
        if defn is None:
            return None
        try:
            return pin_world_position(defn.footprint, placement, pin_number)
        except (IndexError, ValueError):
            return None

    def iter_pin_positions(
        self,
        comp_id: uuid.UUID,
    ) -> list[tuple[int, Point]]:
        """Return [(pin_number, world_pos), …] for every pin of *comp_id*.

        Returns an empty list if the component or its definition is not found.
        """
        placement = self.layout.placements.get(comp_id)
        inst = self.instances.get(comp_id)
        if placement is None or inst is None:
            return []
        defn = self.components.get(inst.definition_ref)
        if defn is None:
            return []
        result: list[tuple[int, Point]] = []
        for n in range(defn.footprint.pin_count()):
            pin_num = n + 1
            try:
                pos = pin_world_position(defn.footprint, placement, pin_num)
                result.append((pin_num, pos))
            except (IndexError, ValueError):
                continue
        return result
