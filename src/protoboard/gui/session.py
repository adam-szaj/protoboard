# SPDX-License-Identifier: MIT
"""EditorSession — mutable state shared across all GUI panels.

Holds the current Board, Layout, Schematic and CommandStack. GUI
components observe it via Qt signals and mutate it only through Commands.
"""

from __future__ import annotations

import time
import uuid
from collections.abc import Iterable
from dataclasses import dataclass, field
from pathlib import Path
from typing import TYPE_CHECKING

from PySide6.QtCore import QObject, Signal

from protoboard.domain.board.board import Board
from protoboard.domain.routing_settings import RoutingSettings
from protoboard.gui.highlight_state import HighlightStack
from protoboard.gui.layer_state import LayerOverrides
from protoboard.services.drc.engine import DRCIssue
from protoboard.controller.editing.headless_session import HeadlessSession
from protoboard.controller.editing.item_handle import ItemId, ItemTag
from protoboard.services.library.loader import ComponentLibrary
from protoboard.services.recording.recorder import CommandRecorder

if TYPE_CHECKING:
    from protoboard.controller.selection.service import SelectionService


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
    # Per-layer paint timings (last paint pass).  ``bg_build_ms`` is the
    # time to rasterise the board pixmap; ``back_build_ms`` rasterises
    # the back-side scene items; ``blit_ms`` is the sum of all blit ops
    # executed during the paint (board base + back + board mid for the
    # full path, or anchor stretch-blit during a wheel/pan burst).
    # 0.0 if the corresponding pass didn't run that paint.
    bg_build_ms: float = 0.0
    back_build_ms: float = 0.0
    blit_ms: float = 0.0
    paint_ms: float = 0.0  # total paintEvent wall time
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

    def record_paint(
        self,
        *,
        bg_build_ms: float = 0.0,
        back_build_ms: float = 0.0,
        blit_ms: float = 0.0,
        paint_ms: float = 0.0,
    ) -> None:
        """Record one paint pass.  All ms params default 0 so callers
        can record partial passes (e.g. burst-blit has no build cost)."""
        self.bg_build_ms = bg_build_ms
        self.back_build_ms = back_build_ms
        self.blit_ms = blit_ms
        self.paint_ms = paint_ms


class EditorSession(HeadlessSession, QObject):
    """Central state for one open project.

    Extends the Qt-free :class:`HeadlessSession` state substrate (board /
    layout / schematic / instances / components + CommandStack + read caches)
    with a ``QObject`` and the signals GUI panels observe. The pure-Python
    state and lookups live on the base so a headless CLI / test front-end can
    drive the same :class:`~protoboard.controller.editing.controller.EditController`
    verbs without a display server.
    """

    layout_changed = Signal()
    drag_moved = Signal()       # emitted on every drag step; does NOT trigger panels/DRC
    drc_changed = Signal(list)  # list[DRCIssue]
    dirty_changed = Signal(bool)
    # Carries (frozenset[UUID] comp_ids, frozenset[UUID] wire_ids, UUID|None net_id).
    # Emitted by SelectTool after any selection change so panels can sync
    # (board -> panel half of the selection hub).
    selection_changed = Signal(object, object, object)
    # Carries the same (comp_ids, wire_ids, net_id) triple, but in the opposite
    # direction: a panel row-click asks the board to select these objects
    # (panel -> board half).  BoardView subscribes once and drives SelectTool;
    # SelectTool then re-broadcasts the result through ``selection_changed`` so
    # every panel syncs.  Kept distinct from ``selection_changed`` so the two
    # directions never short-circuit into each other inside the session.
    selection_requested = Signal(object, object, object)
    # Emitted by SelectTool when the interaction mode changes (carries mode label str).
    highlight_mode_changed = Signal(str)
    # Emitted by RouteTool when it starts/ends routing a specific net.
    # Carries the net UUID or None.  BoardView additively highlights this net
    # on top of any active HighlightStack entry (union semantics).
    routing_net_changed = Signal(object)
    # True when a background AutorouteWorker is running.  Listeners (MainWindow,
    # BoardView, NetsEditorPanel) gate hard-mutate UI actions on this — the
    # in-flight worker built its RouteRequest from an immutable snapshot, but
    # applying its result alongside live edits would corrupt the undo stack
    # and silently nullify user changes made mid-route.
    autoroute_active_changed = Signal(bool)
    # Emitted by EditController.set_active_tool whenever the active tool
    # changes. ToolHost subscribes to rebuild its KeyMap layer for the new
    # tool (tool.make_key_map(self._base_keymap)).
    active_tool_changed = Signal()

    def __init__(
        self,
        board: Board,
        library: ComponentLibrary,
        parent: QObject | None = None,
    ) -> None:
        # QObject first (explicit — the diamond has non-cooperative __init__s),
        # then the Qt-free state substrate.
        QObject.__init__(self, parent)
        HeadlessSession.__init__(self, board, library)
        self.routing_settings: RoutingSettings = RoutingSettings()
        # axis_label_settings is initialised by HeadlessSession.__init__.
        self.layer_overrides: LayerOverrides = LayerOverrides()
        # The GUI session holds the QObject subclass (it carries the ``changed``
        # signal BoardView/MainWindow connect to); the base declares the field
        # as the Qt-free ``HighlightStackBase``. Narrowing a mutable attribute
        # trips pyright's invariance rule, but ``HighlightStack`` IS-A
        # ``HighlightStackBase`` and nothing writes a bare base back — the
        # override is sound.
        self.highlight_stack: HighlightStack = HighlightStack(self)  # type: ignore[assignment]
        self._routing_net_id: uuid.UUID | None = None
        self.project_path: Path | None = None
        self._drc_issues: list[DRCIssue] = []
        self._dirty: bool = False
        self._autoroute_active: bool = False
        self.perf: PerfMetrics = PerfMetrics()
        # Command recorder — wired in by MainWindow once a project path is
        # known.  Auto-records every command through CommandStack.
        self.recorder: CommandRecorder = CommandRecorder()
        self.stack.set_recorder(self.recorder)
        # The GUI selection lives in a per-session SelectionService, parked here
        # by SelectTool.activate (reused across tool switches). Declared here so
        # the EditState selection() / select() bridge and the tool share one
        # attribute; None until a SelectTool has activated at least once.
        self.selection_service: SelectionService | None = None

    def _record_build_time(self, kind: str, ms: float) -> None:
        """Feed lazy-cache (re)build times into the GUI ``PerfMetrics``.

        Overrides the headless base's no-op so the performance panel keeps
        reporting connectivity-graph / pad-index rebuild cost.
        """
        if kind == "graph":
            self.perf.graph_build_ms = ms
        elif kind == "pad_index":
            self.perf.pad_index_build_ms = ms

    def notify_changed(self) -> None:
        """Change hook for :class:`EditController` — emit ``layout_changed``.

        Satisfies the ``EditState`` protocol so a controller verb refreshes
        panels/DRC after every command. Headless drivers leave this a no-op
        (they don't define it); the GUI session emits the same signal the
        tools' old ``_execute_command`` fired directly.
        """
        self.layout_changed.emit()

    def notify_active_tool_changed(self) -> None:
        """Hook for :meth:`EditController.set_active_tool` — emit the signal.

        ``ToolHost`` subscribes to swap its KeyMap layer for the new tool.
        """
        self.active_tool_changed.emit()

    def notify_selection(
        self,
        comp_ids: "frozenset[uuid.UUID]",
        wire_ids: "frozenset[uuid.UUID]",
        net_id: "uuid.UUID | None",
    ) -> None:
        """Called by SelectTool to broadcast selection state to all panels."""
        self.selection_changed.emit(comp_ids, wire_ids, net_id)

    # ------------------------------------------------------------------
    # Selection (EditState bridge over the tool-owned SelectionService)
    # ------------------------------------------------------------------

    def selection(self) -> frozenset[ItemId]:
        """The current selection as tagged :class:`ItemId`s (EditState API).

        Projects the per-session ``SelectionService`` (``Hit``-based) onto the
        controller's ``ItemId`` shape — ``Hit.kind`` is exactly the ``ItemId``
        tag, so the map is 1:1. Empty before any SelectTool has activated.
        """
        svc = self.selection_service
        if svc is None:
            return frozenset()
        out: set[ItemId] = set()
        for sel in svc.selected():
            iid = sel.to_hit().item_id()
            if iid is not None:
                out.add(iid)
        return frozenset(out)

    def select(self, ids: Iterable[ItemId]) -> None:
        """Replace the selection with *ids* and broadcast to panels (EditState API).

        Inflates each :class:`ItemId` into the ``Selectable`` its tag needs
        (looking the entity up in the live layout) and hands the batch to the
        ``SelectionService``. Panels refresh via ``selection_changed``; the
        board highlight rebuilds on the next refresh, mirroring how
        :meth:`notify_changed` defers its repaint. A no-op before any SelectTool
        has activated (no service to write to).
        """
        svc = self.selection_service
        if svc is None:
            return
        from protoboard.controller.selection.adapters import (
            ComponentSelectable,
            GroupSelectable,
            WireSelectable,
        )
        from protoboard.controller.selection.selectable import Selectable

        id_set = frozenset(ids)
        targets: list[Selectable] = []
        comp_ids: set[uuid.UUID] = set()
        wire_ids: set[uuid.UUID] = set()
        for iid in id_set:
            if iid.tag is ItemTag.component:
                pl = self.layout.placements.get(iid.uuid)
                if pl is not None:
                    targets.append(
                        ComponentSelectable(component_id=iid.uuid, placement=pl)
                    )
                    comp_ids.add(iid.uuid)
            elif iid.tag is ItemTag.wire:
                wire = self.wire_by_id(iid.uuid)
                if wire is not None:
                    targets.append(WireSelectable(wire=wire))
                    wire_ids.add(iid.uuid)
            elif iid.tag is ItemTag.group:
                grp = self.layout.groups.get(iid.uuid)
                if grp is not None:
                    targets.append(GroupSelectable(group=grp))
        svc.set_many(targets)
        self.notify_selection(frozenset(comp_ids), frozenset(wire_ids), None)

    def request_select_component(self, comp_id: uuid.UUID) -> None:
        """Ask the board to make *comp_id* the sole selection (panel -> board).

        Emits ``selection_requested`` with a single component id and no
        wires/net; BoardView translates it into ``SelectTool._select_primary``.
        """
        self.selection_requested.emit(frozenset({comp_id}), frozenset(), None)

    def request_select_net(self, net_id: uuid.UUID) -> None:
        """Ask the board to select the whole net — all comps + wires (panel -> board).

        Emits ``selection_requested`` with ``net_id`` set and empty id sets;
        BoardView expands it via the canonical ``SelectTool._menu_select_net``.
        """
        self.selection_requested.emit(frozenset(), frozenset(), net_id)

    def request_select_pin(self, comp_id: uuid.UUID, pin_number: int) -> None:
        """Ask the board to select the pin's component AND light its net.

        The request triple carries no pin field, so the pin's net is resolved
        here (Qt-free, via the schematic) and component + net are passed so BOTH
        component- and net-panels react.  Falls back to a bare component request
        when the pin is netless.
        """
        from protoboard.domain.schematic.net import PinRef

        net = self.schematic.net_for_pin(
            PinRef(component_id=comp_id, pin_number=pin_number)
        )
        self.selection_requested.emit(
            frozenset({comp_id}),
            frozenset(),
            net.id if net is not None else None,
        )

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

    def run_drc(self) -> list[DRCIssue]:
        """Run DRC synchronously, record the result and emit ``drc_changed``.

        Wraps the Qt-free :meth:`HeadlessSession.run_drc` compute with the GUI
        side effects (perf timing + :meth:`set_drc_issues`). Preserved for tests
        and any caller wanting a blocking check; GUI code should use the
        ``DRCWorker`` path in MainWindow and feed the result back via
        :meth:`set_drc_issues` to keep the UI thread responsive.
        """
        t0 = time.monotonic()
        issues = super().run_drc()
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
    # Autoroute UI gate
    # ------------------------------------------------------------------

    @property
    def autoroute_active(self) -> bool:
        return self._autoroute_active

    def set_autoroute_active(self, active: bool) -> None:
        """Toggle the autoroute-running gate; emits when state changes."""
        active = bool(active)
        if active == self._autoroute_active:
            return
        self._autoroute_active = active
        self.autoroute_active_changed.emit(active)

