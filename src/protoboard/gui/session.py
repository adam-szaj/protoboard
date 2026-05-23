# SPDX-License-Identifier: MIT
"""EditorSession — mutable state shared across all GUI panels.

Holds the current Board, Layout, Schematic and CommandStack. GUI
components observe it via Qt signals and mutate it only through Commands.
"""

from __future__ import annotations

import os
import time
import uuid

import structlog as _structlog
_click_log = _structlog.get_logger("protoboard.gui.session.click")
_CLICK_PROFILE = os.environ.get("PROTOBOARD_CLICK_PROFILE") not in (None, "", "0")
from collections import defaultdict
from dataclasses import dataclass, field
from pathlib import Path
from typing import TYPE_CHECKING

if TYPE_CHECKING:
    from protoboard.domain.board.pad_state import PadIndex
    from protoboard.domain.layout.wire import WireSegment

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
from protoboard.domain.axis_labels_settings import AxisLabelSettings
from protoboard.domain.routing_settings import RoutingSettings
from protoboard.gui.layer_state import LayerOverrides
from protoboard.gui.highlight_state import HighlightStack
from protoboard.services.recording.recorder import CommandRecorder


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
        self.axis_label_settings: AxisLabelSettings = AxisLabelSettings()
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
        # Cached pad-walk sets used by post-hoc cost analysis.  Both depend
        # only on board + placements; `_plated_through_pads_cache` is board-
        # only (never invalidates after construction), while
        # `_under_component_pads_cache` is invalidated alongside the graph.
        # Without these, every selection-changed event would re-walk every
        # board atom — visible as multi-hundred-ms freeze when the user
        # clicks a wire on a large board.
        self._plated_through_pads_cache: set[Point] | None = None
        self._under_component_pads_cache: set[Point] | None = None
        # wire.id → net.id mapping, cached because both BoardView and the
        # inspector / performance panels call ``_build_wire_net_map`` on
        # every selection click.  Invalidated alongside the graph since
        # it derives from ConnectivityGraph.
        self._wire_net_map_cache: dict[uuid.UUID, uuid.UUID] | None = None
        # wire.id → WireSegment lookup, populated lazily by
        # ``wire_by_id()``.  SelectTool's click handlers used to scan
        # ``layout.wires`` for every highlight redraw — O(N²) when the
        # active net has many wires.  Invalidated on every layout change.
        self._wire_by_id_cache: dict[uuid.UUID, "WireSegment"] | None = None
        self.perf: PerfMetrics = PerfMetrics()
        # Command recorder — wired in by MainWindow once a project path is
        # known.  Auto-records every command through CommandStack.
        self.recorder: CommandRecorder = CommandRecorder()
        self.stack.set_recorder(self.recorder)

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

    # ------------------------------------------------------------------
    # Recording lifecycle
    # ------------------------------------------------------------------

    DEFAULT_RECORDING_FILENAME = "session_commands.yaml"

    def start_recording(self, path: Path | None = None) -> None:
        """Begin auto-recording commands to *path*.

        If *path* is None, uses ``<project_path>/session_commands.yaml``;
        does nothing when neither *path* nor ``project_path`` is set.
        Restarting always begins a fresh log — previous content is replaced.
        """
        target = path
        if target is None:
            if self.project_path is None:
                return
            target = self.project_path / self.DEFAULT_RECORDING_FILENAME
        label = self.project_path.name if self.project_path else ""
        self.recorder.start(target, project_label=label)

    def stop_recording(self) -> None:
        """Stop the recorder if it is active.  Safe to call repeatedly."""
        self.recorder.stop()

    def reset_recording(self) -> None:
        """Clear the log and start a fresh recording at the same path.

        Called after a successful save+commit so that the log only ever
        contains commands executed *since* the last committed snapshot.
        No-op when no recording is active.
        """
        if not self.recorder.active or self.recorder.path is None:
            return
        target = self.recorder.path
        label = self.project_path.name if self.project_path else ""
        self.recorder.stop()
        self.recorder.start(target, project_label=label)

    def invalidate_graph(self) -> None:
        """Mark the cached ConnectivityGraph, PadIndex and spatial indexes as stale."""
        self._graph = None
        self._pad_index = None
        self._comp_spatial_index = None
        self._wire_spatial_index = None
        # Under-component pad set depends on placements; invalidate alongside
        # the graph.  Plated-through is board-only and survives.
        self._under_component_pads_cache = None
        # Wire→net map depends on the graph; recompute lazily next time.
        self._wire_net_map_cache = None
        # Wire-by-id index depends only on the wires list; rebuilt on
        # demand.  layout_changed invalidates the graph (via this method),
        # so this also stays in sync.
        self._wire_by_id_cache = None

    def cached_under_component_pads(self) -> set[Point]:
        """Return cached set of top-layer pads sitting under a component body.

        Used by the post-hoc cost analyzer.  Rebuilt on first access after
        ``invalidate_graph`` (which the layout_changed signal triggers).
        Keeping this on the session avoids re-walking every board atom for
        every panel refresh / selection click.
        """
        cache = self._under_component_pads_cache
        if cache is None:
            from protoboard.services.routing.cost_analyzer import (
                _under_component_pads,
            )
            cache = _under_component_pads(
                self.board, self.layout, self.components, self.instances,
            )
            self._under_component_pads_cache = cache
        return cache

    def wire_by_id(self, wire_id: uuid.UUID) -> "WireSegment | None":
        """O(1) wire lookup by id.

        Replaces the ``next((w for w in layout.wires if w.id == X), None)``
        idiom that select_tool was running per highlight redraw — at O(N)
        per call, that becomes O(N²) when the active net is large.
        """
        cache = self._wire_by_id_cache
        if cache is None:
            cache = {w.id: w for w in self.layout.wires}
            self._wire_by_id_cache = cache
        return cache.get(wire_id)

    def cached_wire_net_map(self) -> dict[uuid.UUID, uuid.UUID]:
        """Return cached wire.id → net.id mapping.

        Multiple panels build this map on every selection click — caching
        it on the session collapses the work to one build per layout
        change.  Recomputed lazily after ``invalidate_graph``.
        """
        cache = self._wire_net_map_cache
        if cache is None:
            _t0 = time.monotonic() if _CLICK_PROFILE else 0.0
            from protoboard.gui.board_view import _build_wire_net_map
            cache = _build_wire_net_map(self)
            self._wire_net_map_cache = cache
            if _CLICK_PROFILE:
                _click_log.info(
                    "click_profile.session.cached_wire_net_map.build",
                    ms=round((time.monotonic() - _t0) * 1000, 1),
                    n_wires=len(cache),
                )
        return cache

    def cached_plated_through_pads(self) -> set[Point]:
        """Return cached set of plated-through pad positions on this board.

        Board topology is fixed for the lifetime of the project, so this
        cache is built once and reused forever.
        """
        cache = self._plated_through_pads_cache
        if cache is None:
            from protoboard.services.routing.cost_analyzer import (
                _plated_through_pads,
            )
            cache = _plated_through_pads(self.board)
            self._plated_through_pads_cache = cache
        return cache

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
            dt = (time.monotonic() - t0) * 1000
            self.perf.graph_build_ms = dt
            if _CLICK_PROFILE:
                _click_log.info(
                    "click_profile.session.connectivity_graph.build",
                    ms=round(dt, 1),
                )
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
        """Run DRC synchronously on the calling thread.

        Preserved for tests and any caller that genuinely wants a
        blocking check (e.g. CLI tools, headless validation).  GUI code
        should use the ``DRCWorker`` path in MainWindow and feed the
        result back via :meth:`set_drc_issues` to keep the UI thread
        responsive.
        """
        t0 = time.monotonic()
        engine = DRCEngine()
        issues = engine.check_with_graph(
            schematic=self.schematic,
            layout=self.layout,
            components=self.components,
            instances=self.instances,
            graph=self.connectivity_graph(),
            axis_labels=self.axis_label_settings,
        )
        self.perf.drc_ms = (time.monotonic() - t0) * 1000
        self.set_drc_issues(issues)
        return issues

    def set_drc_issues(self, issues: list[DRCIssue]) -> None:
        """Apply a DRC result computed elsewhere (typically a worker).

        Owns the state-mutation + signal-emit half that ``run_drc``
        used to inline.  Always called on the main thread (Qt cross-
        thread signal routing handles ``DRCWorker.finished`` for us).
        """
        self._drc_issues = issues
        self.perf.drc_issue_count = len(issues)
        self.perf.wire_count = len(self.layout.wires)
        self.perf.component_count = len(self.instances)
        self.drc_changed.emit(issues)

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
