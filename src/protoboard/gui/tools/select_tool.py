# SPDX-License-Identifier: MIT
"""``SelectTool`` — the selection tool (rewritten as a service).

A thin ``_Tool`` that wires the headless-tested pieces together:

* :class:`~protoboard.gui.selection.hit_test.HitTester` — point → Selectable,
* :class:`~protoboard.gui.selection.service.SelectionService` — selection state
  and the active-pick routing,
* :class:`~protoboard.gui.highlight.controller.HighlightController` — dimming,
* :class:`~protoboard.gui.selection.drag_controller.DragController` and
  :class:`~protoboard.gui.selection.rubber_band_controller.RubberBandController`
  — concrete interactions behind one
  :class:`~protoboard.gui.selection.mouse_input.MouseInputController` seam.

Covers: hit-test → selection (SET/ADD/REMOVE via the modifier matrix),
rubber-band area-select, drag / floating of the selection, rotate / flip /
rotate-around-point, ESC-pop, group enter/exit, and routing clicks into an
active pick.  Wire-edit and net-relation selection arrive in later phases.

The tool keeps almost no logic of its own — on press it arms a mouse-interaction
controller or mutates the selection; pointer events and translated intents
(CONFIRM / CANCEL / UNDO) forward to the active controller; the behaviour lives
in tested, Qt-free controllers.
"""

from __future__ import annotations

import uuid
from typing import TYPE_CHECKING

import structlog
from PySide6.QtCore import QPointF, Qt
from PySide6.QtGui import QColor, QPen
from PySide6.QtWidgets import QGraphicsRectItem

from protoboard.domain.layout.layout import ROOT_GROUP_ID
from protoboard.geometry import Point
from protoboard.gui.board_view import _Tool
from protoboard.gui.highlight.controller import HighlightController
from protoboard.gui.selection.drag_controller import DragController, FloatingPhase
from protoboard.gui.selection.hit_test import HitTester, SelectionFilters
from protoboard.gui.selection.mouse_input import Intent, Mods
from protoboard.gui.selection.rubber_band_controller import RubberBandController
from protoboard.gui.selection.service import SelectionService
from protoboard.gui.selection_fsm import BasisScope, SetOp
from protoboard.services.debug_trace import traced

_log = structlog.get_logger(__name__)

if TYPE_CHECKING:
    from collections.abc import Callable

    from PySide6.QtGui import QKeyEvent, QMouseEvent, QWheelEvent
    from PySide6.QtWidgets import QGraphicsItem

    from protoboard.domain.layout.group_tree import GroupTree
    from protoboard.domain.layout.wire import Wire
    from protoboard.gui.board_view import BoardView
    from protoboard.gui.drag_set import DragSet
    from protoboard.gui.selection.adapters import WireVertexSelectable
    from protoboard.gui.selection.armed import ArmedSubTool
    from protoboard.gui.selection.mouse_input import MouseInputController
    from protoboard.gui.selection.selectable import Selectable
    from protoboard.gui.selection.wire_edit import WireEditState
    from protoboard.gui.session import EditorSession
    from protoboard.gui.tools.drag_commit_decision import DragCommitDecision
    from protoboard.gui.tools.drag_corner_text import DragCornerText

# Below this travel (raster units) a press+release is a click, not a drag —
# used to synthesise the on_click event for click-driven controllers (picks).
_CLICK_THRESHOLD = 0.3

# Namespaced key actions this tool declares.  None → inherit the generic
# ``global.action.<name>`` default (so Delete/Edit/Cancel follow whatever the
# user bound globally); a literal overrides for this tool.  The shortcut veto
# and on_key dispatch resolve keys against these *by id*, never by hard-coded
# Qt.Key literals — so a user rebinding any of them is honoured automatically.
_ACT_EDIT = "select-tool.action.edit"
_ACT_REROUTE = "select-tool.action.reroute"
_ACT_AUTOROUTE = "select-tool.action.autoroute"
_ACT_DELETE = "select-tool.action.delete"
_ACT_CANCEL = "select-tool.action.cancel"
# Wire-edit op-table extra: [F] flips the selected fragment to the other layer.
_ACT_FLIP_FRAGMENT = "select-tool.action.flip_fragment"
# Armed transform modes: [R] arms float-rotate, [G] arms keyboard float-move.
# Both inherit a generic global default and dispatch by id, never raw key.
_ACT_ROTATE = "select-tool.action.rotate"
_ACT_MOVE = "select-tool.action.move"
_ACT_FLIP = "select-tool.action.flip"
# Directional step actions used INSIDE the armed modes — each carries two
# default binds (arrow ``|`` WASD) so both work; rebindable like any action.
# rotate steps CW/CCW; move steps the anchor one cell.  Dispatched + vetoed
# through the KeyMap by id (never raw Qt.Key), per [[every-action-has-keybinding]].
_ACT_STEP_LEFT = "select-tool.action.step_left"
_ACT_STEP_RIGHT = "select-tool.action.step_right"
_ACT_STEP_UP = "select-tool.action.step_up"
_ACT_STEP_DOWN = "select-tool.action.step_down"
# Every action the armed sub-tools dispatch / veto against (the superset).
_ARMED_ACTION_IDS: tuple[str, ...] = (
    _ACT_STEP_LEFT, _ACT_STEP_RIGHT, _ACT_STEP_UP, _ACT_STEP_DOWN,
    _ACT_ROTATE, _ACT_FLIP, _ACT_CANCEL,
)

# Rubber-band visuals (match the legacy tool's cyan band).
_BAND_PEN_LR = QPen(QColor(0, 200, 255, 180), 0.06)
_BAND_PEN_RL = QPen(QColor(0, 200, 255, 180), 0.06)
_BAND_PEN_RL.setStyle(Qt.PenStyle.DashLine)
_BAND_FILL_LR = QColor(0, 200, 255, 20)
_BAND_FILL_RL = QColor(0, 200, 255, 10)

# Wire-edit visuals (copied verbatim from the legacy SelectTool).  The cyan
# segment colour signals "edit mode"; the start/end vertices are marked red, and
# hand-drawn reroute waypoints preview as a dashed orange polyline.
_WIRE_EDIT_PEN = QPen(QColor(0, 204, 255, 220), 0.42)
_WIRE_EDIT_PEN.setCapStyle(Qt.PenCapStyle.RoundCap)
_WIRE_EDIT_PEN.setJoinStyle(Qt.PenJoinStyle.RoundJoin)
_NODE_EDIT_PEN = QPen(QColor(0, 204, 255, 255), 0.05)
_NODE_EDIT_BRUSH = QColor(0, 204, 255, 220)
_NODE_SELECTED_PEN = QPen(Qt.GlobalColor.red, 0.06)
_NODE_SELECTED_BRUSH = QColor(255, 80, 0, 230)
_EDIT_PREVIEW_PEN = QPen(QColor(255, 120, 0, 200), 0.12)
_EDIT_PREVIEW_PEN.setStyle(Qt.PenStyle.DashLine)
_NODE_RADIUS = 0.13


class SelectTool(_Tool):
    """Selection tool built on the SelectionService / HitTester / Highlight stack."""

    def __init__(self) -> None:
        super().__init__()  # sets _session / _view to None up-front
        self._selection: SelectionService = None  # type: ignore[assignment]
        self._highlight: HighlightController = None  # type: ignore[assignment]
        self._group_stack: list[uuid.UUID] = [ROOT_GROUP_ID]
        # The in-flight mouse interaction (drag, rubber-band, pick, …) or None.
        # All pointer + intent events route here uniformly; the controller owns
        # its own start threshold and outcome.
        self._mouse: MouseInputController | None = None
        self._press_pt: QPointF | None = None
        self._band_item: QGraphicsRectItem | None = None
        # A plain press that landed on an *already-selected* entity defers its
        # selection change to release: if the gesture turns out to be a click
        # (no drag), the release collapses the selection onto this entity — or
        # deselects it when it was the only thing selected.  None when the press
        # was on something not selected (selection already happened on press).
        self._deferred_click_sel: Selectable | None = None
        # Live-drag conflict label (corner text).  Lazily built on first
        # conflicting drag step; cleared the moment the drag returns to CLEAR
        # so the frame never lingers after the cursor leaves the conflict zone
        # (the legacy "sticky yellow frame" complaint, 2026-06-19).
        self._drag_corner: DragCornerText | None = None
        # Rotate-around-point pivot capture (legacy menu path; seeds the armed
        # rotate's shared pivot).
        self._pivot_capture_pending: bool = False
        self._rotate_pivot: object | None = None  # geometry.Point | None
        # The active armed transform sub-tool (float-rotate [R] / keyboard
        # float-move [G]) or None.  One field, one dispatch — no branching on
        # *which* mode is live (see gui/selection/armed.py).  A keyboard-move
        # sub-tool also parks its live DragController in self._mouse so the
        # preview/glide path is shared with a mouse drag.
        self._armed: ArmedSubTool | None = None
        # Re-drag (bendable lead reposition) — a request_pick client; the
        # overlay items live here, the pick state lives in the service.
        self._redrag_comp_id: uuid.UUID | None = None
        self._redrag_pin: int | None = None
        self._redrag_items: list[QGraphicsItem] = []
        # Armed-rotate pivot markers: re-drawn after each preview refresh (which
        # rebuilds the scene), so a rotating selection visibly shows *what it
        # spins around* — one marker per object (per-object pivot) or one on the
        # shared pad.  Cleared on disarm + scene-clear.
        self._pivot_marker_items: list[QGraphicsItem] = []
        # Wire-edit: the selected fragment (tool-local, like _rotate_pivot) plus
        # the cluster-cycle state threaded through the wire-vertex pick.  The
        # operations on the fragment (reroute / autoroute / delete) are actions
        # dispatched by `_wire_edit_on_key` once the fragment is selected.
        from protoboard.gui.selection.hit_test import WireEditCycle

        self._wire_edit: WireEditState | None = None
        self._wire_edit_cycle: WireEditCycle = WireEditCycle()
        self._wire_edit_wire_id: uuid.UUID | None = None  # wire [E] armed on
        self._wire_edit_start: WireVertexSelectable | None = None  # first vertex
        self._wire_edit_items: list[QGraphicsItem] = []  # cyan overlay + dots
        self._reroute_items: list[QGraphicsItem] = []  # orange waypoint preview
        self._reroute_live: QGraphicsItem | None = None  # live cursor segment
        # Net/connectivity relation axis: which set is correlated to a clicked
        # object — drives Alt (RELATION-basis) selection and the auto-highlight
        # that follows any plain SET click.  Default = same logical net.
        from protoboard.gui.selection_axes import RelationClass, SelectionFilter

        self._relation: RelationClass = RelationClass.SAME_LOGICAL_NET
        # Selection filter (which object kinds may be selected).  Shim for
        # Phase 6a: state is held + mirrored to the status-bar widget, but
        # `_filters()` does NOT yet enforce it (all kinds stay hittable).
        # Enforcement in the hit-test is a deferred follow-up.
        self._filter: SelectionFilter = SelectionFilter.ALL

    # -- _Tool lifecycle ----------------------------------------------------

    @traced("SelectTool.activate")
    def activate(self, session: EditorSession, view: BoardView) -> None:
        self._session = session
        self._view = view
        # SelectionService / HighlightController are per-session — reuse one
        # parked on the session if present, else create and park it.
        svc = getattr(session, "selection_service", None)
        if not isinstance(svc, SelectionService):
            svc = SelectionService()
            session.selection_service = svc  # type: ignore[attr-defined]
        self._selection = svc
        self._highlight = HighlightController(
            session.highlight_stack, source_id="select_tool"
        )

    @traced("SelectTool.deactivate")
    def deactivate(self) -> None:
        # Cancel an armed transform first so leaving the tool reverts a
        # half-done rotate/move (and drops its live preview / controller).
        self._disarm(cancel=True)
        self._cancel_gesture()
        self._clear_band()
        self._clear_drag_corner()
        self._deferred_click_sel = None
        self._redrag_clear()
        self._wire_edit_clear()

    def on_scene_cleared(self) -> None:
        self._band_item = None
        # The scene was rebuilt — our overlay QGraphicsItems are gone; drop the
        # stale references so a later clear doesn't touch freed items.
        self._wire_edit_items = []
        self._reroute_items = []
        self._reroute_live = None
        self._pivot_marker_items = []
        self._drag_corner = None

    # -- helpers ------------------------------------------------------------

    def _hit_tester(self) -> HitTester:
        from typing import cast

        from protoboard.types import Side

        return HitTester(
            self._session, camera_side=cast(Side, self._view.camera_side)
        )

    def _filters(self) -> SelectionFilters:
        # Phase 2: all kinds hittable; the status-bar filter wires in later.
        return SelectionFilters()

    @traced("SelectTool._pick_at")
    def _pick_at(
        self, what: object, scene_pt: QPointF
    ) -> Selectable | None:
        """Resolve a click into the Selectable the active pick wants.

        A ``pad`` / ``point`` pick resolves to the snapped board coordinate (a
        :class:`PointSelectable`); a ``wire-vertex`` pick runs the net-restricted
        cluster-cycling vertex hit-test (threading ``self._wire_edit_cycle`` so
        repeated clicks at a shared knot rotate through siblings); every other
        kind hit-tests for a board entity.
        """
        if what in ("pad", "point"):
            from protoboard.gui.selection.adapters import PointSelectable

            return PointSelectable(point=self._snap(
                Point(scene_pt.x(), scene_pt.y()), frozenset()
            ))
        if what == "wire-vertex":
            from protoboard.gui.board_view import _build_wire_net_map

            sel, cycle = self._hit_tester().hit_wire_vertex(
                scene_pt.x(),
                scene_pt.y(),
                start_wire_id=self._wire_edit_active_start_wire(),
                wire_net_map=_build_wire_net_map(self._session),
                cycle=self._wire_edit_cycle,
            )
            self._wire_edit_cycle = cycle
            return sel
        return self._hit_tester().hit(scene_pt.x(), scene_pt.y(), self._filters())

    def _wire_edit_active_start_wire(self) -> uuid.UUID | None:
        """The wire the wire-vertex pick is anchored on for tie-breaking.

        Before the first vertex it is the wire ``[E]`` was armed on; after the
        first vertex it pivots to that vertex's wire so a cross-wire second pick
        on a sibling resolves against the chosen start (legacy re-pin behaviour).
        """
        if self._wire_edit_start is not None:
            return self._wire_edit_start.wire_id
        return self._wire_edit_wire_id

    @property
    def _current_group_id(self) -> uuid.UUID:
        if not self._group_stack:
            self._group_stack = [ROOT_GROUP_ID]
        return self._group_stack[-1]

    def _status(self, msg: str, timeout_ms: int = 3000) -> None:
        win = self._view.window()
        bar = getattr(win, "statusBar", None)
        if callable(bar):
            bar().showMessage(msg, timeout_ms)

    @staticmethod
    def _decode_modifiers(event: QMouseEvent) -> tuple[BasisScope, SetOp]:
        mods = event.modifiers()
        ctrl = bool(mods & Qt.KeyboardModifier.ControlModifier)
        shift = bool(mods & Qt.KeyboardModifier.ShiftModifier)
        alt = bool(mods & Qt.KeyboardModifier.AltModifier)
        op = (
            SetOp.REMOVE if (ctrl and shift)
            else SetOp.ADD if ctrl
            else SetOp.SET
        )
        basis = BasisScope.RELATION if alt else BasisScope.SINGLE
        return basis, op

    # -- press --------------------------------------------------------------

    @traced("SelectTool.on_press")
    def on_press(self, event: QMouseEvent, scene_pt: QPointF) -> bool:
        if event.button() != Qt.MouseButton.LeftButton:
            return False

        # 1. Pivot-capture mode (rotate-around-point) wins everything.
        if self._pivot_capture_pending:
            self._rotate_pivot = self._view.snap(scene_pt)
            self._pivot_capture_pending = False
            self._status(
                "Pivot set — Rotate CW/CCW to rotate around it  ·  [Esc] Clear", 0
            )
            return True

        # 1b. An armed sub-tool may claim the click (e.g. float-rotate sets its
        #     shared pad pivot) — it does not change the selection while armed.
        if self._armed is not None and self._armed.on_click(
            Point(scene_pt.x(), scene_pt.y())
        ):
            return True

        # 2. An active mouse interaction (drag / band / pick) owns the press.
        #    Record the press point so on_release can tell a click from a drag,
        #    then forward.  Picks act on the synthesised click (on_release);
        #    drag/band armed their own press at construction.
        if self._mouse is not None and not self._mouse.finished:
            self._press_pt = scene_pt
            self._mouse.on_press(Point(scene_pt.x(), scene_pt.y()), self._mods(event))
            return True

        # 2b. A fragment is selected but no pick is live (between picking the
        #     range and choosing an operation) — a plain board press leaves
        #     wire-edit and falls through to normal selection.
        if self._wire_edit is not None:
            self._wire_edit_clear()

        # 3. Normal selection. Resolve the hit.
        basis, op = self._decode_modifiers(event)
        sel = self._hit_tester().hit(scene_pt.x(), scene_pt.y(), self._filters())

        #   3a. Empty space → arm a rubber-band interaction.
        if sel is None:
            self._mouse = self._arm_band(scene_pt)
            return True

        #   3b. A plain (no-modifier) press arms a drag of the selection — the
        #   controller's own threshold decides drag-vs-click.  Selecting happens
        #   on *press* for instant feedback, with one exception: a press that
        #   lands on something already selected keeps the current selection
        #   intact (so a press-drag moves the *whole* multi-selection instead of
        #   collapsing it to the grabbed item first).  With Ctrl/Shift the press
        #   only mutates the set (no drag).
        if op is SetOp.SET:
            self._deferred_click_sel = None
            if self._is_already_selected(sel):
                # Keep the selection intact for a possible drag; if this turns
                # out to be a click, resolve it on release (collapse / deselect).
                self._deferred_click_sel = sel
            else:
                self._apply_selection(sel, op, basis)
            drag = self._arm_drag(sel, scene_pt)
            if drag is not None:
                self._mouse = drag
            return True

        self._apply_selection(sel, op, basis)
        return True

    def _is_already_selected(self, sel: Selectable) -> bool:
        """True when *sel* is part of the current selection.

        Compared by FSM ``Hit`` identity (what the selection is keyed on).  A
        component grabbed inside an already-selected group counts too — the grab
        moves that group monolith, so the selection is "already on" the target.
        """
        if self._selection is None:
            return False
        hit = sel.to_hit()
        selected_hits = {s.to_hit() for s in self._selection.selected()}
        if hit in selected_hits:
            return True
        # A press on a component locked in a selected group: the drag will move
        # the group, so treat the group's selection as covering this press.
        import uuid as _uuid

        if hit.kind == "component" and isinstance(hit.id, _uuid.UUID):
            gid = self._draggable_group_for(hit.id)
            if gid is not None and gid in self._selected_group_ids:
                return True
        return False

    @traced("SelectTool._apply_selection")
    def _apply_selection(
        self, sel: Selectable, op: SetOp, basis: BasisScope = BasisScope.SINGLE
    ) -> None:
        # RELATION basis (Alt): expand the selection to the relation-correlated
        # members (the whole net, neighbours, …) — `set_many` for SET, else add
        # each.  SINGLE basis: just the clicked object.
        targets = [sel]
        if basis is BasisScope.RELATION:
            targets = self._relation_correlated_selectables(sel) or [sel]

        if op == SetOp.ADD:
            for t in targets:
                self._selection.add(t)
        elif op == SetOp.REMOVE:
            # Phase 2 MVP: remove not yet a first-class FSM op here; treat as
            # a fresh SET so the click is never a silent no-op. Refined later.
            self._selection.set_many(targets)
        elif len(targets) > 1:
            self._selection.set_many(targets)
        else:
            self._selection.set(sel)
        self._refresh_highlight(primary=sel)

    def _refresh_highlight(self, *, primary: Selectable | None = None) -> None:
        """Publish the auto-highlight: the selection plus the primary's net context.

        Any plain SET click highlights the clicked object's relation context (its
        net members) so the user sees "what is connected to this" — the legacy
        auto-highlight behaviour.  ``RelationClass.NONE`` disables the context.
        """
        # Tolerate pre-activate calls (main_window applies user prefs to a
        # freshly built tool before activate() binds _selection/_highlight).
        if self._selection is None or self._highlight is None:
            return
        shown: list[Selectable] = list(self._selection.selected())
        anchor = primary if primary is not None else self._primary_selectable()
        net_id: uuid.UUID | None = None
        if anchor is not None:
            shown.extend(self._relation_correlated_selectables(anchor))
            # Publish the anchor's net so BoardView can light up that net's pads
            # (the pad layer has no other "this net is selected" signal).
            net_id = self._anchor_net_id(anchor)
        self._highlight.show(shown, net_id=net_id)

    def _anchor_net_id(self, anchor: Selectable) -> uuid.UUID | None:
        """The logical net of *anchor* (wire/component/pin), or None.

        Same resolution as :meth:`_primary_net_id` but for an explicit anchor,
        so a SET click that passes ``primary=`` lights up the right net.
        """
        from protoboard.gui.selection.queries import SessionConnQuery

        nets = anchor.net_ids(SessionConnQuery(self._session))
        return next(iter(nets), None)

    def _primary_selectable(self) -> Selectable | None:
        if self._selection is None:
            return None
        sel = self._selection.selected()
        return sel[0] if sel else None

    # -- legacy selection-state contract -----------------------------------
    #
    # ``main_window``/panels reach into the tool for the *raw id-set* view of
    # the selection (``_selected_id``/``_selected_ids``/``_selected_wire_ids``/
    # ``_selected_group_id``) and broadcast/summary helpers.  v2 keeps the
    # selection as ``Selectable``s inside the ``SelectionService``; these
    # accessors *project* that state into the id sets the old tool exposed, and
    # the setters *rebuild* the service from ids (wrapping each id in its
    # adapter via ``set_many``).  This is the compat surface that lets the GUI
    # drive v2 unchanged.

    def _selected_by_kind(self, kind: str) -> set[uuid.UUID]:
        """All selected ids of one Hit kind (``component`` / ``wire`` / ``group``)."""
        out: set[uuid.UUID] = set()
        if self._selection is None:  # pre-activate
            return out
        for sel in self._selection.selected():
            hit = sel.to_hit()
            if hit.kind == kind and isinstance(hit.id, uuid.UUID):
                out.add(hit.id)
        return out

    def _set_selection_from_ids(
        self,
        comp_ids: set[uuid.UUID],
        wire_ids: set[uuid.UUID],
        group_ids: set[uuid.UUID],
    ) -> None:
        """Rebuild the service selection from raw id sets (adapter-wrapped)."""
        from protoboard.gui.selection.adapters import (
            ComponentSelectable,
            GroupSelectable,
            WireSelectable,
        )

        targets: list[Selectable] = []
        for cid in comp_ids:
            pl = self._session.layout.placements.get(cid)
            if pl is not None:
                targets.append(ComponentSelectable(component_id=cid, placement=pl))
        for wid in wire_ids:
            wire = self._wire_by_id(wid)
            if wire is not None:
                targets.append(WireSelectable(wire=wire))
        for gid in group_ids:
            grp = self._session.layout.groups.get(gid)
            if grp is not None:
                targets.append(GroupSelectable(group=grp))
        self._selection.set_many(targets)
        self._refresh_highlight()

    @property
    def _selected_id(self) -> uuid.UUID | None:
        """The primary component id (first selected component), or None."""
        if self._selection is None:
            return None
        for sel in self._selection.selected():
            hit = sel.to_hit()
            if hit.kind == "component" and isinstance(hit.id, uuid.UUID):
                return hit.id
        return None

    @property
    def _selected_ids(self) -> set[uuid.UUID]:
        return self._selected_by_kind("component")

    @_selected_ids.setter
    def _selected_ids(self, value: set[uuid.UUID]) -> None:
        self._set_selection_from_ids(
            set(value), self._selected_by_kind("wire"), self._selected_group_ids
        )

    @property
    def _selected_wire_ids(self) -> set[uuid.UUID]:
        return self._selected_by_kind("wire")

    @_selected_wire_ids.setter
    def _selected_wire_ids(self, value: set[uuid.UUID]) -> None:
        self._set_selection_from_ids(
            self._selected_by_kind("component"), set(value), self._selected_group_ids
        )

    @property
    def _selected_group_ids(self) -> set[uuid.UUID]:
        return self._selected_by_kind("group")

    @property
    def _selected_group_id(self) -> uuid.UUID | None:
        return next(iter(self._selected_group_ids), None)

    @_selected_group_id.setter
    def _selected_group_id(self, value: uuid.UUID | None) -> None:
        self._set_selection_from_ids(
            self._selected_by_kind("component"),
            self._selected_by_kind("wire"),
            {value} if value is not None else set(),
        )

    def _select_primary(self, comp_id: uuid.UUID) -> None:
        """Set *comp_id* as the sole (primary) selection and broadcast it.

        ``BoardView.select_component`` calls this to focus a component from
        elsewhere in the UI.  The properties panel updates by subscribing to
        ``session.selection_changed`` (emitted by :meth:`_notify_selection`).
        """
        self._set_selection_from_ids({comp_id}, set(), set())
        self._notify_selection()

    def _deselect(self) -> None:
        """Drop the component part of the selection (keep wires/groups)."""
        self._set_selection_from_ids(
            set(), self._selected_by_kind("wire"), self._selected_group_ids
        )

    def _deselect_wire(self) -> None:
        """Drop the wire part of the selection (keep components/groups)."""
        self._set_selection_from_ids(
            self._selected_by_kind("component"), set(), self._selected_group_ids
        )

    def _clear_multi_selection(self) -> None:
        """Clear the whole selection."""
        self._selection.clear()
        self._refresh_highlight()

    def _redraw_multi_highlights(self) -> None:
        self._refresh_highlight()

    def _redraw_multi_wire_highlights(self) -> None:
        self._refresh_highlight()

    @property
    def _multi_highlights(self) -> list[QGraphicsItem]:
        """Legacy: selection-highlight overlay items.

        v2 renders the highlight through the ``HighlightStack`` (BoardView
        owns the items), not as tool-local overlay items.  Returned empty so
        the one legacy reader (a debug-log count) stays valid.
        """
        return []

    def _notify_selection(self) -> None:
        """Broadcast the current selection to all panels via the session signal."""
        if self._session is None:
            return
        comp_ids = frozenset(self._selected_by_kind("component"))
        wire_ids = frozenset(self._selected_by_kind("wire"))
        net_id = self._primary_net_id()
        self._session.notify_selection(comp_ids, wire_ids, net_id)

    def _primary_net_id(self) -> uuid.UUID | None:
        """The logical net of the primary wire/component, for the panel signal."""
        primary = self._primary_selectable()
        if primary is None:
            return None
        from protoboard.gui.selection.queries import SessionConnQuery

        nets = primary.net_ids(SessionConnQuery(self._session))
        return next(iter(nets), None)

    def _delete_selection(self, *, force: bool = False) -> bool:
        """Delete the selected wires and/or components.  Returns True if it ran.

        Wires structurally owned by a non-active group are skipped unless
        *force* (Shift+Delete) is set — the legacy group-lock guard.  Both
        kinds go into one atomic macro so a mixed selection undoes in one step.
        """
        from typing import cast

        from protoboard.domain.commands.base import (
            Command,
            ComponentState,
            MacroCommand,
        )
        from protoboard.domain.commands.component_cmds import (
            RemoveComponentCommand,
            RemoveWireCommand,
        )

        existing_wires = {w.id for w in self._session.layout.wires}
        wire_ids = [
            wid for wid in self._selected_by_kind("wire")
            if wid in existing_wires and (force or not self._wire_is_locked(wid))
        ]
        existing_comps = set(self._session.layout.placements)
        comp_ids = [
            cid for cid in self._selected_by_kind("component")
            if cid in existing_comps and not self._is_locked_in_group(cid)
        ]
        if not wire_ids and not comp_ids:
            return False

        cmds: list[Command[ComponentState]] = [
            cast("Command[ComponentState]", RemoveWireCommand(wire_id=wid))
            for wid in wire_ids
        ]
        cmds += [
            cast("Command[ComponentState]", RemoveComponentCommand(component_id=cid))
            for cid in comp_ids
        ]
        cmd: Command[ComponentState] = (
            cmds[0] if len(cmds) == 1
            else cast(
                "Command[ComponentState]",
                MacroCommand(cmds, description="Delete selection"),
            )
        )
        self._wire_edit_clear()
        self._execute_command(cmd)
        self._clear_multi_selection()
        self._notify_selection()
        return True

    def _wire_is_locked(self, wire_id: uuid.UUID) -> bool:
        """True iff *wire_id* is structurally owned by a group viewed from outside.

        Mirrors :meth:`_is_locked_in_group` for components: selecting a locked
        wire (for highlight) stays allowed, but mutating it (delete/edit) gates
        here and skips it.  Verbatim port of the legacy guard.
        """
        from protoboard.domain.layout.group_ownership import OwnershipResolver
        from protoboard.domain.layout.layout import ROOT_GROUP_ID

        wire = self._wire_by_id(wire_id)
        if wire is None:
            return False
        groups = self._session.layout.groups
        active_gid = self._current_group_id
        tree = self._session.group_tree()
        resolver = OwnershipResolver.from_session(self._session)
        for gid, group in groups.items():
            if gid == ROOT_GROUP_ID or gid == active_gid:
                continue
            comp_ids = frozenset(tree.all_component_ids(gid))
            if comp_ids and resolver.is_owned(wire, group, comp_ids):
                return True
        return False

    def _status_msg(self, msg: str, timeout_ms: int = 4000) -> None:
        """Status-bar message (legacy name; v2's primitive is ``_status``)."""
        self._status(msg, timeout_ms)

    def selection_summary(self) -> str:
        """One-line status-bar summary of the selection (parity with legacy)."""
        session = self._session
        if session is None:
            return "select: none"

        comp_ids = self._selected_by_kind("component")
        wire_ids = self._selected_by_kind("wire")
        if not comp_ids and not wire_ids:
            return "select: none"

        sel_id = self._selected_id
        sel_wire = self._selected_wire_id
        if sel_id is not None:
            pri: str | None = f"pri:c:{str(sel_id)[:8]}"
        elif sel_wire is not None:
            wnm = session.cached_wire_net_map()
            net_uuid = wnm.get(sel_wire)
            net_obj = (
                session.schematic.nets.get(net_uuid) if net_uuid is not None else None
            )
            net_label = f" ({net_obj.name})" if net_obj is not None else ""
            pri = f"pri:w:{str(sel_wire)[:8]}{net_label}"
        else:
            pri = None

        net_ids: set[uuid.UUID] = set()
        for net_obj in session.schematic.nets.values():
            if any(pr.component_id in comp_ids for pr in net_obj.pins):
                net_ids.add(net_obj.id)
        if wire_ids:
            wnm = session.cached_wire_net_map()
            for wid in wire_ids:
                n = wnm.get(wid)
                if n is not None:
                    net_ids.add(n)

        group_count = len(self._selected_group_ids)
        multi = (
            f"multi:[c:{len(comp_ids)} w:{len(wire_ids)} "
            f"n:{len(net_ids)} g:{group_count}]"
        )
        if pri is None:
            return f"select: {multi}"
        return f"select: {pri} | {multi}"

    # -- net / connectivity relation axis ----------------------------------

    @property
    def relation(self):  # type: ignore[no-untyped-def]
        """The active :class:`RelationClass` (net/connectivity correlation axis).

        A property (not a method) to match the legacy contract: the status-bar
        widget and main_window read ``tool.relation`` field-style.
        """
        return self._relation

    def set_relation(self, relation) -> None:  # type: ignore[no-untyped-def]
        """Set the correlation axis used by Alt-select and the auto-highlight."""
        from protoboard.gui.selection_axes import RelationClass

        if isinstance(relation, RelationClass):
            self._relation = relation
            self._refresh_highlight()

    @property
    def filter(self):  # type: ignore[no-untyped-def]
        """The active :class:`SelectionFilter` (which object kinds are selectable).

        Shim: the value is held and mirrored to the status-bar widget, but the
        hit-test does not yet enforce it (see ``_filters``).
        """
        return self._filter

    def set_filter(self, f) -> None:  # type: ignore[no-untyped-def]
        """Replace the selection filter wholesale (state only — no enforcement yet)."""
        from protoboard.gui.selection_axes import SelectionFilter

        if isinstance(f, SelectionFilter):
            self._filter = f

    def _relation_source(self, sel: Selectable):  # type: ignore[no-untyped-def]
        """Map a hit Selectable to a :class:`RelationSource` for the queries."""
        from protoboard.gui.selection.queries import RelationSource

        hit = sel.to_hit()
        if hit.kind == "wire" and isinstance(hit.id, uuid.UUID):
            return RelationSource.wire(hit.id)
        if hit.kind == "component" and isinstance(hit.id, uuid.UUID):
            if hit.pin_number is not None:
                return RelationSource.pin(hit.id, hit.pin_number)
            return RelationSource.component(hit.id)
        return None

    def _relation_correlated_selectables(
        self, sel: Selectable
    ) -> list[Selectable]:
        """The relation-correlated components + wires of *sel* as Selectables.

        Resolves ``(comp_ids, wire_ids)`` via :func:`relation_correlated` over
        the session-backed connectivity seams, then wraps each id in its adapter
        so the selection / highlight pipelines consume them uniformly.
        """
        from protoboard.gui.selection.adapters import (
            ComponentSelectable,
            WireSelectable,
        )
        from protoboard.gui.selection.queries import (
            SessionConnQuery,
            SessionNetSource,
            relation_correlated,
        )

        source = self._relation_source(sel)
        if source is None:
            return []
        comp_ids, wire_ids = relation_correlated(
            self._relation,
            source,
            SessionConnQuery(self._session),
            SessionNetSource(self._session),
        )
        out: list[Selectable] = []
        for cid in comp_ids:
            pl = self._session.layout.placements.get(cid)
            if pl is not None:
                out.append(ComponentSelectable(component_id=cid, placement=pl))
        for wid in wire_ids:
            wire = self._wire_by_id(wid)
            if wire is not None:
                out.append(WireSelectable(wire=wire))
        return out

    # -- mouse-interaction routing -----------------------------------------

    @staticmethod
    def _mods(event: QMouseEvent) -> Mods:
        mods = event.modifiers()
        return Mods(
            ctrl=bool(mods & Qt.KeyboardModifier.ControlModifier),
            shift=bool(mods & Qt.KeyboardModifier.ShiftModifier),
            alt=bool(mods & Qt.KeyboardModifier.AltModifier),
        )

    @traced("SelectTool.on_move")
    def on_move(self, event: QMouseEvent, scene_pt: QPointF) -> None:
        if self._mouse is not None:
            self._mouse.on_move(Point(scene_pt.x(), scene_pt.y()), self._mods(event))

    @traced("SelectTool.on_release")
    def on_release(self, event: QMouseEvent, scene_pt: QPointF) -> None:
        mouse = self._mouse
        if mouse is None:
            return
        pt = Point(scene_pt.x(), scene_pt.y())
        mods = self._mods(event)
        # Synthesise a click (press + release with no intervening drag) — the
        # higher-level event click-driven controllers (picks) act on.  Below the
        # threshold the release IS the click; otherwise it's a real release.
        if self._is_click(scene_pt):
            mouse.on_click(pt, mods)
        else:
            mouse.on_release(pt, mods)
        self._press_pt = None
        # A floating drag stays the active interaction (CONFIRM / CANCEL ends
        # it); an unfinished pick keeps collecting.  Otherwise it's done.
        if not mouse.finished:
            return
        self._mouse = None
        self._resolve_deferred_click(mouse)
        self._clear_drag_corner()
        self._report_drag_outcome(mouse)
        self._refresh_highlight()
        if self._view is not None:
            self._view.refresh()

    def _resolve_deferred_click(self, mouse: MouseInputController) -> None:
        """Apply the selection change a press-on-selected deferred to release.

        Only fires when the gesture was a click (no drag happened): collapse the
        selection onto the clicked entity, or — when it was already the sole
        selection — deselect it.  A real drag consumes the press, so the
        deferred change is dropped.
        """
        sel = self._deferred_click_sel
        self._deferred_click_sel = None
        if sel is None:
            return
        if not isinstance(mouse, DragController) or not mouse.was_click:
            return
        selected = self._selection.selected()
        sole = len(selected) == 1 and selected[0].to_hit() == sel.to_hit()
        # Pop the selection back to empty first: the FSM's SET op toggles an
        # already-selected hit OFF, so a plain ``set`` here would clear instead
        # of collapsing.  From empty, a fresh ``set`` lands exactly *sel*.
        self._clear_selection_to_empty()
        if not sole:
            self._selection.set(sel)
        self._refresh_highlight()

    def _clear_selection_to_empty(self) -> None:
        """Pop every selection layer until nothing is selected (NORMAL state)."""
        for _ in range(8):  # bounded: selection depth is tiny (primary+multi)
            if self._selection.is_empty():
                return
            self._selection.clear()

    def _report_drag_outcome(self, mouse: MouseInputController) -> None:
        """Status-bar toast after a drag auto-commits on release.

        Only a drag reports an outcome; the toast names how many wires were
        dropped/edited so a SOFT auto-commit is visible and obviously undoable.
        """
        if not isinstance(mouse, DragController):
            return
        outcome = mouse.last_outcome
        if outcome is None or outcome.phase is not FloatingPhase.COMMITTED:
            return
        n = outcome.dropped_wire_count
        if n:
            self._status(f"Moved — {n} wire(s) dropped/edited  ·  Ctrl+Z undoes", 4000)

    @traced("SelectTool.on_double_press")
    def on_double_press(self, scene_pt: QPointF) -> None:
        """Board double-click → forward to the active interaction.

        (BoardView calls this hook for ``MouseButtonDblClick``.)
        """
        if self._mouse is not None and not self._mouse.finished:
            self._mouse.on_double_click(
                Point(scene_pt.x(), scene_pt.y()), Mods()
            )

    def _is_click(self, scene_pt: QPointF) -> bool:
        """True when the release is within the click threshold of the press."""
        start = self._press_pt
        if start is None:
            return False
        dx = scene_pt.x() - start.x()
        dy = scene_pt.y() - start.y()
        return dx * dx + dy * dy < _CLICK_THRESHOLD * _CLICK_THRESHOLD

    # -- arming gestures ----------------------------------------------------

    @traced("SelectTool._arm_band")
    def _arm_band(self, scene_pt: QPointF) -> RubberBandController:
        press = Point(scene_pt.x(), scene_pt.y())
        return RubberBandController(
            press_pt=press,
            comp_ids=list(self._session.layout.placements),
            bbox=self._session.component_bbox,
            draw=self._draw_band,
            clear=self._clear_band,
            select=self._band_select,
        )

    @traced("SelectTool._arm_drag")
    def _arm_drag(
        self, sel: Selectable, scene_pt: QPointF
    ) -> DragController | None:
        """Build a drag for the current selection grabbed at *sel*.

        When the grabbed entity is a group, or a component locked inside a group
        below the active context, the whole group moves as a monolith; otherwise
        the free component(s) move.  Returns None when the hit isn't draggable
        (e.g. a bare wire-vertex in a later phase) — the press stays a plain
        selection.
        """
        drag_set = self._build_drag_set(sel)
        if drag_set.is_empty:
            return None
        anchor_id = self._anchor_component(sel)
        if anchor_id is None:
            return None
        pl = self._session.layout.placements.get(anchor_id)
        if pl is None:
            return None
        anchor_pos = pl.position
        press = Point(scene_pt.x(), scene_pt.y())
        # Grab offset: where the cursor sits relative to the anchor, so the
        # anchor snaps under the cursor's grab point (legacy anchor-snap).
        grab = (anchor_pos.x - press.x, anchor_pos.y - press.y)

        from typing import cast

        from protoboard.gui.selection.drag_controller import DragContext

        return DragController(
            cast("DragContext", self._session),
            drag_set=drag_set,
            anchor_id=anchor_id,
            anchor_pos=anchor_pos,
            press_pt=press,
            grab_offset=grab,
            snap=self._snap,
            preview=self._drag_preview,
            execute=self._execute_command,
            auto_accept_clear=True,
        )

    def _drag_preview(self, decision: DragCommitDecision | None) -> None:
        """Live drag feedback: glide the scene + show/clear the conflict label.

        Called by :class:`DragController` after every move (and at release) with
        the live conflict decision (``None`` when the drag is cancelled).  We do
        two things, both keyed *only* on the current decision so nothing lingers:

        * emit ``session.drag_moved`` so :meth:`BoardView._on_drag_moved`
          translates the dragged scene items to follow the cursor without a full
          rebuild — the visual feedback that was missing;
        * show a corner label while the drop is conflicting (SOFT amber / HARD
          red) and **hide it the instant the drop is CLEAR again**, so the frame
          can't stick after the cursor leaves the conflict zone.
        """
        # 1) Glide the dragged items to their current (already-mutated) layout
        #    positions.  _on_drag_moved reads layout.placements / wires directly.
        self._session.drag_moved.emit()

        # 2) Conflict label — built lazily, cleared whenever the drop is clear.
        self._show_conflict_corner(decision)

    def _show_conflict_corner(self, decision: DragCommitDecision | None) -> None:
        """Show/clear the corner conflict label for a live preview decision.

        Shared by drag and armed-rotate previews: SOFT amber / HARD red while
        conflicting, hidden the instant the state is CLEAR (or ``None``) so the
        frame never lingers.
        """
        from protoboard.gui.tools.drag_commit_decision import DragSeverity

        if decision is None or decision.severity is DragSeverity.CLEAR:
            self._clear_drag_corner()
            return
        amber = QColor(180, 130, 0, 220)
        red = QColor(180, 40, 25, 230)
        color = red if decision.severity is DragSeverity.HARD else amber
        text = decision.description or (
            "Cannot drop here — overlap  ·  [Esc] cancel"
            if decision.severity is DragSeverity.HARD
            else "Drops/edits some wires on release"
        )
        self._show_drag_corner(text, color)

    def _rotate_preview(self, decision: DragCommitDecision | None) -> None:
        """Live armed-rotate feedback: re-render the rotated bodies + conflict.

        Unlike a drag (pure translation → cheap ``setPos`` glide via
        ``drag_moved``), a rotation changes each body's *orientation* and pin
        layout, which the glide cannot show — so a rotate step needs a real
        ``refresh()`` to re-render at the new angle.  The conflict corner is
        shared with the drag path so HARD/SOFT feedback looks identical.
        """
        if self._view is not None:
            self._view.refresh()  # rebuilds the scene → markers must be re-drawn
        self._draw_pivot_markers()
        self._show_conflict_corner(decision)

    def _draw_pivot_markers(self) -> None:
        """Draw the armed-rotate pivot marker(s): what the selection spins around.

        Shared pad pivot → one marker on that pad; per-object → one on each
        rotating component's centre.  Re-drawn after every preview ``refresh``
        (which clears the scene), and cleared on disarm.  No-op unless an
        :class:`ArmedRotate` is active.
        """
        from protoboard.gui.selection.armed_rotate import ArmedRotate

        self._clear_pivot_markers()
        armed = self._armed
        if not isinstance(armed, ArmedRotate) or self._view is None:
            return
        if armed.pivot is not None:
            self._add_pivot_marker(armed.pivot, shared=True)
        else:
            for cid in armed.comp_ids:
                pl = self._session.layout.placements.get(cid)
                if pl is not None:
                    self._add_pivot_marker(pl.position, shared=False)

    def _add_pivot_marker(self, pos: Point, *, shared: bool) -> None:
        """Add one pivot marker (a ⊕ glyph) at *pos*; track it for cleanup."""
        from PySide6.QtGui import QPen
        from PySide6.QtWidgets import QGraphicsEllipseItem, QGraphicsLineItem

        # Shared pivot is bigger + brighter (it governs the whole selection);
        # per-object markers are small.  Orange = "transforming" (distinct from
        # the cyan static-selection outline).
        orange = QColor(255, 150, 0, 230)
        r = 0.34 if shared else 0.18
        pen = QPen(orange, 0.06 if shared else 0.04)
        ring = QGraphicsEllipseItem(pos.x - r, pos.y - r, 2 * r, 2 * r)
        ring.setPen(pen)
        ring.setZValue(21.0)
        self._view.scene().addItem(ring)
        self._pivot_marker_items.append(ring)
        for dx, dy in ((-r, 0.0), (r, 0.0), (0.0, -r), (0.0, r)):
            cross = QGraphicsLineItem(pos.x, pos.y, pos.x + dx, pos.y + dy)
            cross.setPen(pen)
            cross.setZValue(21.0)
            self._view.scene().addItem(cross)
            self._pivot_marker_items.append(cross)

    def _clear_pivot_markers(self) -> None:
        for item in self._pivot_marker_items:
            try:
                self._view.scene().removeItem(item)
            except RuntimeError:
                pass
        self._pivot_marker_items = []

    def _show_drag_corner(self, text: str, color: QColor) -> None:
        from protoboard.gui.tools.drag_corner_text import DragCornerText

        if self._view is None:
            return
        if self._drag_corner is None:
            self._drag_corner = DragCornerText()
        self._drag_corner.show(self._view.scene(), self._view, text, color)

    def _clear_drag_corner(self) -> None:
        if self._drag_corner is not None and self._view is not None:
            self._drag_corner.hide(self._view.scene())

    def _build_drag_set(self, sel: Selectable) -> DragSet:
        """Snapshot what the grab moves: free components, or whole groups.

        A press on a group (or on a component locked in a group below the active
        context) drags that group as a monolith; otherwise the selected free
        components move.  Built via :meth:`DragSet.from_selection`, which
        partitions locked-in-group components into their group monolith.
        """
        from protoboard.gui.drag_set import DragSet

        group_ids = self._grabbed_group_ids(sel)
        return DragSet.from_selection(
            selected_ids=self._selected_comp_ids(),
            selected_wire_ids=frozenset(),
            selected_group_ids=group_ids,
            groups_map=self._session.layout.groups,
            is_locked_in_group=self._is_locked_in_group,
        )

    def _grabbed_group_ids(self, sel: Selectable) -> frozenset[uuid.UUID]:
        """Group ids to move as monoliths for this grab.

        * A group hit → that group.
        * A component hit that is locked in a group below the active context →
          the outermost such group (so a click inside a nested group drags the
          group you see, not its innermost leaf).
        """
        import uuid as _uuid

        hit = sel.to_hit()
        if hit.kind == "group" and isinstance(hit.id, _uuid.UUID):
            return frozenset({hit.id})
        if hit.kind == "component" and isinstance(hit.id, _uuid.UUID):
            gid = self._draggable_group_for(hit.id)
            if gid is not None:
                return frozenset({gid})
        return frozenset()

    def _draggable_group_for(self, comp_id: uuid.UUID) -> uuid.UUID | None:
        """Outermost group containing *comp_id* below the active context, or None.

        None when the component is free, or when the active context *is* the
        innermost containing group (the user entered it, so members drag freely).
        The "below the active context" groups are those whose recursive member
        set does NOT include the active group — the outermost of these is the
        monolith you see and grab.
        """
        if not self._is_locked_in_group(comp_id):
            return None
        tree = self._session.group_tree()
        active = self._current_group_id
        candidates = [
            gid
            for gid in tree.groups_containing_component(comp_id)
            if gid != active and not self._is_ancestor_group(gid, active, tree)
        ]
        if not candidates:
            return None
        # Outermost = the candidate enclosing the most components.
        return max(candidates, key=lambda g: len(tree.all_component_ids(g)))

    @staticmethod
    def _is_ancestor_group(
        gid: uuid.UUID, ancestor: uuid.UUID, tree: GroupTree
    ) -> bool:
        """True when *ancestor* is *gid* or one of its parent groups.

        Walks the parent chain so a group nested under the active context is not
        chosen as the drag monolith (the user entered the context, so groups
        above it move freely / not at all here).
        """
        if ancestor == ROOT_GROUP_ID:
            return False
        cur: uuid.UUID | None = gid
        while cur is not None:
            if cur == ancestor:
                return True
            cur = tree.parent_group_of(cur)
        return False

    def _is_locked_in_group(self, comp_id: uuid.UUID) -> bool:
        """True when *comp_id* belongs to a group other than the active context."""
        tree = self._session.group_tree()
        containing = tree.groups_containing_component(comp_id)
        if not containing:
            return False
        # Free to drag alone only when the active context is one of its groups.
        return self._current_group_id not in containing

    def _anchor_component(self, sel: Selectable) -> uuid.UUID | None:
        import uuid as _uuid

        hit = sel.to_hit()
        if hit.kind == "component" and isinstance(hit.id, _uuid.UUID):
            return hit.id
        # Group grab: anchor on any member component (delta is shared).
        if hit.kind == "group" and isinstance(hit.id, _uuid.UUID):
            members = self._session.group_tree().all_component_ids(hit.id)
            anchored = next(iter(members), None)
            if anchored is not None:
                return anchored
        ids = self._selected_comp_ids()
        return next(iter(ids), None)

    def _snap(self, raw: Point, exclude: frozenset[uuid.UUID]) -> Point:
        from PySide6.QtCore import QPointF as _QPointF

        snapped = self._view.snap(_QPointF(raw.x, raw.y), exclude_ids=exclude)
        return Point(snapped.x, snapped.y)

    def _execute_command(self, cmd: object) -> bool:
        from typing import cast

        from protoboard.domain.commands.base import (
            Command,
            CommandRejectedError,
            ComponentState,
        )

        try:
            self._session.stack.execute(
                cast("Command[ComponentState]", cmd),
                cast("ComponentState", self._session),
            )
        except CommandRejectedError:
            return False
        self._session.layout_changed.emit()
        return True

    # -- rubber-band Qt seams ----------------------------------------------

    def _draw_band(self, a: Point, b: Point, ltr: bool) -> None:
        from PySide6.QtCore import QRectF

        x0, y0 = min(a.x, b.x), min(a.y, b.y)
        rect = QRectF(x0, y0, abs(b.x - a.x), abs(b.y - a.y))
        if self._band_item is None:
            item = QGraphicsRectItem(rect)
            item.setPen(_BAND_PEN_LR if ltr else _BAND_PEN_RL)
            item.setBrush(_BAND_FILL_LR if ltr else _BAND_FILL_RL)
            self._view.scene().addItem(item)
            self._band_item = item
        else:
            self._band_item.setRect(rect)
            self._band_item.setPen(_BAND_PEN_LR if ltr else _BAND_PEN_RL)
            self._band_item.setBrush(_BAND_FILL_LR if ltr else _BAND_FILL_RL)

    def _band_select(self, comp_ids: list[uuid.UUID]) -> None:
        from protoboard.gui.selection.adapters import ComponentSelectable

        picked: list[Selectable] = []
        for cid in comp_ids:
            pl = self._session.layout.placements.get(cid)
            if pl is not None:
                picked.append(ComponentSelectable(component_id=cid, placement=pl))
        if picked:
            self._selection.set_many(picked)

    # -- intent dispatch (CONFIRM / CANCEL / UNDO) -------------------------

    @staticmethod
    def _key_to_intent(key: int) -> Intent | None:
        """Map a Qt key to a semantic interaction intent.

        The one place keys become intents, so controllers never see raw
        keycodes.  A future revision can resolve this through the app's
        keybinding registry; the defaults match the conventional bindings.
        """
        if key in (Qt.Key.Key_Return, Qt.Key.Key_Enter):
            return Intent.CONFIRM
        if key == Qt.Key.Key_Escape:
            return Intent.CANCEL
        if key == Qt.Key.Key_Backspace:
            return Intent.UNDO
        return None

    @traced("SelectTool._dispatch_intent")
    def _dispatch_intent(self, intent: Intent) -> bool:
        """Route a semantic intent to the active interaction; return if consumed.

        The single path by which the tool drives confirm / cancel / undo on
        whatever controller is live, so the key→intent mapping has one home and
        no controller-type branching leaks into the key handler.
        """
        mouse = self._mouse
        if mouse is None:
            return False
        # Read the floating drop-count before CONFIRM clears the decision, so the
        # post-commit toast can report it.
        dropped = 0
        if (
            intent is Intent.CONFIRM
            and isinstance(mouse, DragController)
            and mouse.is_floating
            and mouse.last_decision is not None
        ):
            dropped = len(mouse.last_decision.free_drop_wire_ids) + len(
                mouse.last_decision.cross_group_disconnect_wire_ids
            ) + len(mouse.last_decision.stationary_group_drop_wire_ids)
        if not mouse.on_event(intent):
            return False
        if mouse.finished:
            self._mouse = None
            self._clear_drag_corner()
            self._refresh_highlight()
            if dropped:
                self._status(
                    f"Removed {dropped} wire(s) (Ctrl+Z to undo)", 4000
                )
            if self._view is not None:
                self._view.refresh()
        return True

    def _cancel_gesture(self) -> None:
        """Abort the active interaction (deactivate / external cancel)."""
        self._dispatch_intent(Intent.CANCEL)

    def _clear_band(self) -> None:
        if self._band_item is not None:
            try:
                self._view.scene().removeItem(self._band_item)
            except RuntimeError:
                pass
            self._band_item = None

    # -- re-drag (bendable lead reposition, a request_pick client) ----------

    @traced("SelectTool.begin_redrag")
    def begin_redrag(self, comp_id: uuid.UUID | None = None) -> None:
        """Arm re-drag: ask for the pad a free (bendable) lead should reach.

        The first real consumer of the pick service — instead of its own click
        loop, it does ``request_pick("pad", Stop.count(1))``.  Picks a single
        board pad; ``on_progress`` draws the reachable-pad overlay + a feedback
        line, ``on_done`` validates the reach and commits a
        :class:`MoveComponentCommand` adjusting the pin offset.
        """
        from protoboard.gui.selection import Stop

        cid = comp_id if comp_id is not None else next(
            iter(self._selected_comp_ids()), None
        )
        if cid is None:
            self._status("Re-drag: select a component with a bendable lead first")
            return
        pin = self._free_bendable_pin(cid)
        if pin is None:
            self._status("Re-drag: this component has no free bendable lead")
            return
        self._redrag_comp_id = cid
        self._redrag_pin = pin
        self._draw_redrag_overlay(cid)
        self._status("Click a pad for the lead  ·  [Esc] Cancel", 0)
        self._arm_pick(
            "pad",
            Stop.count(1),
            on_done=self._redrag_commit,
            on_cancel=self._redrag_clear,
            on_progress=self._redrag_progress,
        )

    @traced("SelectTool._arm_pick")
    def _arm_pick(
        self,
        what: str,
        stop: object,
        *,
        on_done: object,
        on_cancel: object = None,
        on_progress: object = None,
        on_preview: object = None,
    ) -> None:
        """Start a pick and park it as the active mouse interaction.

        Builds a :class:`PickSession` via the service, wraps it in a
        :class:`PointPickController` (a MouseInputController), and routes board
        clicks / intents through ``self._mouse`` like any other interaction.
        The point→Selectable resolver is the tool's :meth:`_pick_at`, so a
        ``pad`` pick yields the snapped coordinate and an entity pick hit-tests.
        ``on_preview`` (``(scene_pt, mods) -> None``) drives a live cursor
        preview on pointer motion (the reroute rubber-band line).
        """
        from collections.abc import Callable
        from typing import cast

        from protoboard.gui.selection.point_pick_controller import (
            PointPickController,
        )
        from protoboard.gui.selection.selectable import PickKind, Stop

        # A pick takes over the interaction — abort any in-flight gesture first.
        if self._mouse is not None and not self._mouse.finished:
            self._cancel_gesture()
        kind = cast("PickKind", what)
        session = self._selection.request_pick(
            kind,
            cast("Stop", stop),
            on_done=cast("Callable[[list[Selectable]], None]", on_done),
            on_cancel=cast("Callable[[], None] | None", on_cancel),
            on_progress=cast(
                "Callable[[list[Selectable]], None] | None", on_progress
            ),
        )

        def _resolve(pt: Point, _mods: Mods) -> Selectable | None:
            return self._pick_at(kind, QPointF(pt.x, pt.y))

        self._mouse = PointPickController(
            session,
            _resolve,
            on_preview=cast(
                "Callable[[Point, Mods], None] | None", on_preview
            ),
        )

    def _free_bendable_pin(self, comp_id: uuid.UUID) -> int | None:
        inst = self._session.instances.get(comp_id)
        pl = self._session.layout.placements.get(comp_id)
        if inst is None or pl is None:
            return None
        defn = self._session.components.get(inst.definition_ref)
        if defn is None or not defn.bendable_pins:
            return None
        # After a vertical flip on a 2-pin part the bend offset lives on pin 1.
        if pl.vertical_flipped and defn.footprint.pin_count() == 2:
            spec = defn.bendable_pins.get(1) or defn.bendable_pins.get(2)
            return 1 if (spec is not None and spec.is_free()) else None
        for pin, spec in sorted(defn.bendable_pins.items()):
            if spec.is_free():
                return pin
        return None

    def _redrag_progress(self, picked: list[Selectable]) -> None:
        # The pick is a single pad; nothing to render mid-collection here (the
        # reachable-pad overlay is drawn once on arm).  Hook kept so a richer
        # live preview (feedback line) can grow without changing the call site.
        return

    def _redrag_commit(self, picked: list[Selectable]) -> None:
        from protoboard.gui.selection.adapters import PointSelectable

        cid, pin = self._redrag_comp_id, self._redrag_pin
        self._redrag_clear()
        if cid is None or pin is None or not picked:
            return
        target = picked[0]
        if not isinstance(target, PointSelectable):
            return
        cmd = self._build_redrag_command(cid, pin, target.point)
        if cmd is None:
            self._status("Re-drag: pad out of the lead's reach")
            return
        self._execute_command(cmd)
        self._refresh_highlight()
        if self._view is not None:
            self._view.refresh()

    def _build_redrag_command(
        self, comp_id: uuid.UUID, pin: int, target: Point
    ) -> object | None:
        """Build the MoveComponentCommand setting *pin*'s offset to reach *target*.

        Validates the snapped pad is within the lead's reach, then inverts the
        placement pipeline (unrotate + bottom-side X-mirror) to express the world
        delta as a local pin offset — mirrors legacy ``_commit_redrag``.
        """
        from protoboard.domain.commands.component_cmds import MoveComponentCommand
        from protoboard.domain.layout.placement import (
            pin_world_position,
            unrotate,
        )

        pl = self._session.layout.placements.get(comp_id)
        inst = self._session.instances.get(comp_id)
        if pl is None or inst is None:
            return None
        defn = self._session.components.get(inst.definition_ref)
        if defn is None:
            return None
        spec = defn.bendable_pins.get(pin)
        snapped = self._snap(target, frozenset())
        if spec is None or not spec.is_free():
            return None
        if not spec.is_reachable_from_origin(pl.position, snapped):
            return None
        # World delta vs. the un-offset baseline → local offset (invert pipeline).
        temp = pl.copy()
        temp.pin_offsets = {k: v for k, v in pl.pin_offsets.items() if k != pin}
        baseline = pin_world_position(defn.footprint, temp, pin)
        world_delta = Point(snapped.x - baseline.x, snapped.y - baseline.y)
        local_off = unrotate(world_delta, pl.rotation)
        if pl.side == "bottom":
            local_off = Point(-local_off.x, local_off.y)
        new_pl = pl.copy()
        new_offsets = dict(pl.pin_offsets)
        if local_off.x == 0.0 and local_off.y == 0.0:
            new_offsets.pop(pin, None)
        else:
            new_offsets[pin] = local_off
        new_pl.pin_offsets = new_offsets
        return MoveComponentCommand(
            component_id=comp_id, new_placement=new_pl, old_placement=pl.copy()
        )

    def _draw_redrag_overlay(self, comp_id: uuid.UUID) -> None:
        from PySide6.QtGui import QBrush, QPen
        from PySide6.QtWidgets import QGraphicsEllipseItem

        self._clear_redrag_overlay()
        pin = self._redrag_pin
        inst = self._session.instances.get(comp_id)
        pl = self._session.layout.placements.get(comp_id)
        if pin is None or inst is None or pl is None:
            return
        defn = self._session.components.get(inst.definition_ref)
        if defn is None:
            return
        spec = defn.bendable_pins.get(pin)
        if spec is None or not spec.is_free():
            return
        r = 0.18
        green = QColor(0, 200, 80, 120)
        for pt in self._view.board_pad_positions():
            if spec.is_reachable_from_origin(pl.position, pt):
                item = QGraphicsEllipseItem(pt.x - r, pt.y - r, 2 * r, 2 * r)
                item.setBrush(QBrush(green))
                item.setPen(QPen(Qt.GlobalColor.transparent, 0))
                item.setZValue(20.0)
                self._view.scene().addItem(item)
                self._redrag_items.append(item)

    def _redrag_clear(self) -> None:
        self._clear_redrag_overlay()
        self._redrag_comp_id = None
        self._redrag_pin = None

    def _clear_redrag_overlay(self) -> None:
        for item in self._redrag_items:
            try:
                self._view.scene().removeItem(item)
            except RuntimeError:
                pass
        self._redrag_items = []

    # -- wire-edit (fragment = selection, operations = actions) -------------

    @traced("SelectTool._begin_edit_selected")
    def _begin_edit_selected(self) -> None:
        """``[E]`` — edit the selected object, dispatched by its kind.

        Edit means different things per type: a group is *entered* (its members
        become editable), a wire opens *wire-edit* (fragment pick).  A free
        component has no edit mode yet (it is moved/rotated directly).  The
        dispatch is on the selection, so one key serves every editable kind.
        """
        if self._selected_group_id is not None:
            self.enter_group()
            return
        if self._selected_wire_id is not None:
            self.arm_wire_edit()
            return
        self._status("Nothing to edit — select a group or wire first")

    @traced("SelectTool.arm_wire_edit")
    def arm_wire_edit(self) -> None:
        """Enter wire-edit on the selected wire — the ``[E]`` entry point.

        Requires a wire in the selection (the whole-wire ``WireSelectable`` from
        a prior click).  Arms a two-vertex ``wire-vertex`` pick: the two clicks
        define the fragment range, and ``on_done`` builds the
        :class:`WireEditState`.  Until both vertices are picked the tool is in
        the pick (intents route to it); once the fragment is selected the
        op-keys (``[W]`` / ``[A]`` / ``[Del]`` / ``[Esc]``) take over.
        """
        from protoboard.gui.selection.hit_test import WireEditCycle
        from protoboard.gui.selection.selectable import Stop

        wire_id = self._selected_wire_id
        if wire_id is None:
            self._status("Wire-edit: select a wire first")
            return
        self._wire_edit_clear()
        self._wire_edit_wire_id = wire_id
        self._wire_edit_cycle = WireEditCycle()
        self._draw_wire_edit_overlay()
        self._status(
            "Pick the fragment: click two wire vertices  ·  [Esc] Cancel", 0
        )
        self._arm_pick(
            "wire-vertex",
            Stop.count(2),
            on_done=self._range_picked,
            on_cancel=self._wire_edit_clear,
            on_progress=self._range_progress,
        )

    @property
    def _selected_wire_id(self) -> uuid.UUID | None:
        for sel in self._selection.selected():
            hit = sel.to_hit()
            if hit.kind == "wire" and isinstance(hit.id, uuid.UUID):
                return hit.id
        return None

    def _range_progress(self, picked: list[Selectable]) -> None:
        """Repaint the overlay as the two range vertices come in."""
        from protoboard.gui.selection.adapters import WireVertexSelectable

        if picked and isinstance(picked[-1], WireVertexSelectable):
            self._wire_edit_start = picked[0] if isinstance(
                picked[0], WireVertexSelectable
            ) else None
        self._draw_wire_edit_overlay()

    def _range_picked(self, picked: list[Selectable]) -> None:
        """Build the :class:`WireEditState` from the two picked vertices."""
        from protoboard.gui.selection.adapters import WireVertexSelectable
        from protoboard.gui.selection.wire_edit import WireEditState

        verts = [p for p in picked if isinstance(p, WireVertexSelectable)]
        if len(verts) < 2:
            self._wire_edit_clear()
            return
        v0, v1 = verts[0], verts[1]
        start_wire, start_idx = v0.wire_id, v0.index
        end_wire, end_idx = v1.wire_id, v1.index
        # Same-wire: normalise so start_idx <= end_idx (legacy swap); a zero-
        # length range is not a fragment.
        if start_wire == end_wire:
            if start_idx == end_idx:
                self._wire_edit_clear()
                return
            if end_idx < start_idx:
                start_idx, end_idx = end_idx, start_idx
        self._wire_edit = WireEditState(
            start_wire_id=start_wire,
            start_idx=start_idx,
            end_wire_id=end_wire,
            end_idx=end_idx,
        )
        self._draw_wire_edit_overlay()
        self._status(
            "Fragment selected  ·  [W] Reroute  ·  [A] Autoroute  ·  "
            "[F] Flip layer  ·  [Del] Delete  ·  [Esc] Cancel",
            0,
        )

    def _wire_edit_on_key(self, event: QKeyEvent) -> bool:
        """Op-table for the selected fragment — one entry per operation.

        Dispatches by *action id* resolved through the KeyMap (not by hard-coded
        keys), so rebinding reroute/autoroute/delete is honoured.  Cancel stays
        on raw Esc / Backspace too, since those are not global shortcuts.
        Returns True when the key was consumed.
        """
        key = event.key()
        if self._key_matches(event, _ACT_REROUTE):
            self._begin_reroute()
        elif self._key_matches(event, _ACT_AUTOROUTE):
            self._autoroute_fragment()
        elif self._key_matches(event, _ACT_FLIP_FRAGMENT):
            self._flip_wire_fragment()
        elif self._key_matches(event, _ACT_DELETE) or key == Qt.Key.Key_Backspace:
            self._delete_fragment()
        elif self._key_matches(event, _ACT_CANCEL) or key == Qt.Key.Key_Escape:
            self._wire_edit_clear()
        else:
            return False
        event.accept()
        return True

    def _begin_reroute(self) -> None:
        """Arm the waypoint pick — ``[W]`` reroute (a second chained pick).

        Collects snapped board waypoints (``point`` / ``Stop.confirm()``);
        ``on_progress`` previews the dashed orange polyline, ``on_preview``
        draws the live cursor segment, Enter (CONFIRM) commits.
        """
        from protoboard.gui.selection.selectable import Stop

        if self._wire_edit is None:
            return
        self._status(
            "Reroute: click waypoints  ·  [Enter] Commit  ·  "
            "[Backspace] Undo  ·  [Esc] Cancel",
            0,
        )
        self._arm_pick(
            "point",
            Stop.confirm(),
            on_done=self._commit_reroute,
            on_cancel=self._reroute_cancel,
            on_progress=self._reroute_progress,
            on_preview=self._reroute_preview,
        )

    def _reroute_progress(self, picked: list[Selectable]) -> None:
        from protoboard.gui.selection.adapters import PointSelectable

        pts = [p.point for p in picked if isinstance(p, PointSelectable)]
        self._draw_reroute_preview(pts)

    def _reroute_preview(self, scene_pt: Point, _mods: Mods) -> None:
        """Live rubber-band from the last anchor to the cursor (snapped)."""
        from PySide6.QtWidgets import QGraphicsLineItem

        self._clear_reroute_live()
        anchor = self._reroute_anchor()
        if anchor is None:
            return
        snapped = self._snap(scene_pt, frozenset())
        line = QGraphicsLineItem(anchor.x, anchor.y, snapped.x, snapped.y)
        line.setPen(_EDIT_PREVIEW_PEN)
        line.setZValue(20.0)
        self._view.scene().addItem(line)
        self._reroute_live = line

    def _reroute_anchor(self) -> Point | None:
        """The point a new reroute segment starts from (last waypoint or start).

        Single source of truth — the live :class:`PickSession`'s collected
        waypoints, the same list ``_reroute_progress`` draws the committed
        polyline from.  ``state.waypoints`` is only populated at commit time
        (``with_waypoints``), so reading it here would make every live segment
        start from the fragment start instead of the last picked pad — the same
        anchoring RouteTool gets for free from its single ``_points`` list.
        """
        state = self._wire_edit
        if state is None:
            return None
        last = self._reroute_last_picked_point()
        if last is not None:
            return last
        wire = self._wire_by_id(state.start_wire_id)
        if wire is None:
            return None
        return wire.path[state.start_idx]

    def _reroute_last_picked_point(self) -> Point | None:
        """The last confirmed waypoint from the active pick, or None.

        Reaches into ``self._mouse`` (the parked ``PointPickController``) for
        its ``PickSession.collected`` list — the authoritative, live record of
        what the user has clicked so far this reroute.
        """
        from protoboard.gui.selection.adapters import PointSelectable

        mouse = self._mouse
        session = getattr(mouse, "session", None)
        if session is None:
            return None
        pts = [p.point for p in session.collected if isinstance(p, PointSelectable)]
        return pts[-1] if pts else None

    def _commit_reroute(self, picked: list[Selectable]) -> None:
        """Commit the reroute: build the path command and execute it."""
        from protoboard.gui.selection.adapters import PointSelectable
        from protoboard.gui.selection.wire_edit import build_reroute_command

        state = self._wire_edit
        if state is None:
            self._wire_edit_clear()
            return
        pts = [p.point for p in picked if isinstance(p, PointSelectable)]
        cmd = build_reroute_command(
            self._session.layout,
            list(self._session.layout.wires),
            self._wire_net_map(),
            state.with_waypoints(pts),
        )
        self._wire_edit_clear()
        if cmd is not None:
            self._execute_command(cmd)
        self._refresh_after_wire_op()

    def _reroute_cancel(self) -> None:
        """Esc during reroute → drop the waypoints, keep the fragment selected."""
        self._clear_reroute_overlay()
        if self._wire_edit is not None:
            self._status(
                "Fragment selected  ·  [W] Reroute  ·  [A] Autoroute  ·  "
                "[F] Flip layer  ·  [Del] Delete  ·  [Esc] Cancel",
                0,
            )

    def _autoroute_fragment(self) -> None:
        """A* re-route the fragment — ``[A]``."""
        from protoboard.gui.selection.wire_edit import (
            autoroute_fragment_path,
            build_reroute_command,
        )

        state = self._wire_edit
        if state is None:
            return
        source, target, side = self._fragment_endpoints(state)
        if source is None or target is None:
            self._wire_edit_clear()
            return
        graph = self._session.connectivity_graph()
        routed = autoroute_fragment_path(graph, source, target, side)
        if routed is None:
            self._wire_edit_clear()
            self._status("Autoroute: no path found")
            self._refresh_after_wire_op()
            return
        # Interior waypoints only — endpoints come from the fragment range.
        waypoints = routed[1:-1]
        cmd = build_reroute_command(
            self._session.layout,
            list(self._session.layout.wires),
            self._wire_net_map(),
            state.with_waypoints(waypoints),
        )
        self._wire_edit_clear()
        if cmd is not None:
            self._execute_command(cmd)
        self._refresh_after_wire_op()

    def _delete_fragment(self) -> None:
        """Delete the selected fragment — ``[Del]`` / ``[Backspace]``."""
        from protoboard.gui.selection.wire_edit import build_delete_command

        state = self._wire_edit
        if state is None:
            return
        cmd = build_delete_command(
            list(self._session.layout.wires), self._wire_net_map(), state
        )
        self._wire_edit_clear()
        if cmd is not None:
            self._execute_command(cmd)
        self._refresh_after_wire_op()

    def _fragment_endpoints(
        self, state: WireEditState
    ) -> tuple[Point | None, Point | None, str]:
        """Resolve the fragment's two boundary points + side for autoroute."""
        start_wire = self._wire_by_id(state.start_wire_id)
        end_wire = self._wire_by_id(state.end_wire_id)
        if start_wire is None or end_wire is None:
            return None, None, "top"
        return (
            start_wire.path[state.start_idx],
            end_wire.path[state.end_idx],
            start_wire.side,
        )

    def _wire_by_id(self, wire_id: uuid.UUID) -> Wire | None:
        return next(
            (w for w in self._session.layout.wires if w.id == wire_id), None
        )

    def _wire_net_map(self) -> dict[uuid.UUID, uuid.UUID]:
        from protoboard.gui.board_view import _build_wire_net_map

        return _build_wire_net_map(self._session)

    def _refresh_after_wire_op(self) -> None:
        self._refresh_highlight()
        if self._view is not None:
            self._view.refresh()

    def _wire_edit_clear(self) -> None:
        """Leave wire-edit: drop the fragment, cycle, overlays, status."""
        from protoboard.gui.selection.hit_test import WireEditCycle

        self._clear_wire_edit_overlay()
        self._clear_reroute_overlay()
        self._wire_edit = None
        self._wire_edit_wire_id = None
        self._wire_edit_start = None
        self._wire_edit_cycle = WireEditCycle()

    # -- wire-edit overlays (tool-drawn, like the redrag overlay) -----------

    def _draw_wire_edit_overlay(self) -> None:
        """Cyan segments for the armed wire + red dots at the range endpoints."""
        from PySide6.QtGui import QBrush
        from PySide6.QtWidgets import QGraphicsEllipseItem, QGraphicsLineItem

        self._clear_wire_edit_overlay()
        # The wire(s) to outline: the armed wire, plus the end wire of a cross-
        # wire fragment once both endpoints are picked.
        marked: dict[uuid.UUID, set[int]] = {}
        if self._wire_edit is not None:
            marked.setdefault(self._wire_edit.start_wire_id, set()).add(
                self._wire_edit.start_idx
            )
            marked.setdefault(self._wire_edit.end_wire_id, set()).add(
                self._wire_edit.end_idx
            )
        elif self._wire_edit_start is not None:
            marked.setdefault(self._wire_edit_start.wire_id, set()).add(
                self._wire_edit_start.index
            )

        wire_ids = set(marked)
        if self._wire_edit_wire_id is not None:
            wire_ids.add(self._wire_edit_wire_id)

        for wid in wire_ids:
            wire = self._wire_by_id(wid)
            if wire is None:
                continue
            pts = wire.path
            for i in range(len(pts) - 1):
                seg = QGraphicsLineItem(
                    pts[i].x, pts[i].y, pts[i + 1].x, pts[i + 1].y
                )
                seg.setPen(_WIRE_EDIT_PEN)
                seg.setZValue(9.0)
                self._view.scene().addItem(seg)
                self._wire_edit_items.append(seg)
            marked_idx = marked.get(wid, set())
            for i, pt in enumerate(pts):
                r = _NODE_RADIUS
                dot = QGraphicsEllipseItem(pt.x - r, pt.y - r, 2 * r, 2 * r)
                if i in marked_idx:
                    dot.setPen(_NODE_SELECTED_PEN)
                    dot.setBrush(QBrush(_NODE_SELECTED_BRUSH))
                else:
                    dot.setPen(_NODE_EDIT_PEN)
                    dot.setBrush(QBrush(_NODE_EDIT_BRUSH))
                dot.setZValue(10.0)
                self._view.scene().addItem(dot)
                self._wire_edit_items.append(dot)

    def _draw_reroute_preview(self, waypoints: list[Point]) -> None:
        """Dashed orange polyline from the fragment start through the waypoints."""
        from PySide6.QtWidgets import QGraphicsLineItem

        self._clear_reroute_overlay()
        anchor = None
        state = self._wire_edit
        if state is not None:
            wire = self._wire_by_id(state.start_wire_id)
            if wire is not None:
                anchor = wire.path[state.start_idx]
        if anchor is None:
            return
        prev = anchor
        for wp in waypoints:
            line = QGraphicsLineItem(prev.x, prev.y, wp.x, wp.y)
            line.setPen(_EDIT_PREVIEW_PEN)
            line.setZValue(20.0)
            self._view.scene().addItem(line)
            self._reroute_items.append(line)
            prev = wp

    def _clear_wire_edit_overlay(self) -> None:
        for item in self._wire_edit_items:
            try:
                self._view.scene().removeItem(item)
            except RuntimeError:
                pass
        self._wire_edit_items = []

    def _clear_reroute_overlay(self) -> None:
        self._clear_reroute_live()
        for item in self._reroute_items:
            try:
                self._view.scene().removeItem(item)
            except RuntimeError:
                pass
        self._reroute_items = []

    def _clear_reroute_live(self) -> None:
        if self._reroute_live is not None:
            try:
                self._view.scene().removeItem(self._reroute_live)
            except RuntimeError:
                pass
            self._reroute_live = None

    # -- keys ---------------------------------------------------------------

    @traced("SelectTool.on_key")
    def on_key(self, event: QKeyEvent) -> None:
        key = event.key()
        mods = event.modifiers()
        ctrl = bool(mods & Qt.KeyboardModifier.ControlModifier)
        shift = bool(mods & Qt.KeyboardModifier.ShiftModifier)
        alt = bool(mods & Qt.KeyboardModifier.AltModifier)

        # An armed sub-tool (float-rotate / keyboard-move) owns its keys first:
        # one uniform dispatch — step / accept / cancel live on the sub-tool, no
        # branching on which mode it is.  Drop it once it reports finished.
        if self._armed is not None and self._armed.on_key(event):
            if self._armed.finished:
                self._disarm()
            event.accept()
            return

        # Pivot mode: Esc clears it first.
        if (self._pivot_capture_pending or self._rotate_pivot is not None):
            if key == Qt.Key.Key_Escape:
                self._clear_pivot()
                self._status("Pivot cleared")
                event.accept()
                return

        # Translate the key into a semantic intent and offer it to the active
        # mouse interaction (a floating drag, or a pick via PointPickController:
        # CONFIRM ends it, CANCEL aborts, UNDO drops the last pick).  One path —
        # no controller-type branching; the controller decides.  A wire-vertex
        # or reroute-waypoint pick is live here, so its Enter/Esc/Backspace
        # route to the pick before the op-keys get a turn.
        intent = self._key_to_intent(key)
        if intent is not None and self._dispatch_intent(intent):
            event.accept()
            return

        # Fragment selected and no pick in flight → the op-keys act on it
        # ([W] reroute · [A] autoroute · [Del] delete · [Esc] cancel).  This is
        # the "operations are actions on the selected fragment" branch.
        if self._wire_edit is not None and self._mouse is None:
            if self._wire_edit_on_key(event):
                return

        if (
            self._key_matches(event, _ACT_DELETE)
            or (key == Qt.Key.Key_Backspace and not ctrl)
        ):
            if self._delete_selection(force=shift):
                event.accept()
            return

        if self._key_matches(event, _ACT_EDIT):
            self._begin_edit_selected()  # edit the selected object
            event.accept()
            return

        if key == Qt.Key.Key_R and ctrl:
            self.begin_redrag()  # Ctrl+R — re-drag the selected lead (legacy parity)
            event.accept()
            return

        if self._key_matches(event, _ACT_MOVE) and not self._selection.is_empty():
            self.arm_move()  # [G] — keyboard float-move (arrows / WSAD)
            event.accept()
            return

        # Alt+R flips (immediate 2-pin flip, kept as a one-shot until flip joins
        # the armed transform mode); the bare rotate action arms float-rotate.
        if self._key_matches(event, _ACT_FLIP) or (key == Qt.Key.Key_R and alt):
            self._flip_selected()
            event.accept()
            return

        if self._key_matches(event, _ACT_ROTATE) and not ctrl:
            self.arm_rotate()
            event.accept()
            return

        if key == Qt.Key.Key_Escape:
            if len(self._group_stack) > 1:
                self.exit_group()
            else:
                self._selection.clear()
                self._refresh_highlight()
            event.accept()

    def on_key_release(self, event: QKeyEvent) -> None:
        pass

    def key_actions(self) -> dict[str, str | None]:
        """The keyboard actions this tool owns (namespaced; see module consts).

        ``None`` inherits the generic global default; ``reroute`` / ``autoroute``
        carry tool-specific shortcuts.  Registered into the view's KeyMap on
        activate; the veto + on_key dispatch resolve against these by id.
        """
        return {
            _ACT_EDIT: None,        # global.action.edit  (default "E")
            _ACT_REROUTE: "W",
            _ACT_AUTOROUTE: "A",
            _ACT_DELETE: None,      # global.action.delete (default "Del")
            _ACT_CANCEL: None,      # global.action.cancel (default "Esc")
            _ACT_ROTATE: "R",       # arm float-rotate
            _ACT_MOVE: "G",         # arm keyboard float-move ("grab")
            _ACT_FLIP: None,        # global.action.flip (default "Alt+R")
            _ACT_FLIP_FRAGMENT: "F",  # wire-edit op: flip fragment to other layer
            # Directional steps inside the armed modes — arrow OR WASD (the
            # ``|`` carries both binds; rebindable).
            _ACT_STEP_LEFT: "Left|A",
            _ACT_STEP_RIGHT: "Right|D",
            _ACT_STEP_UP: "Up|W",
            _ACT_STEP_DOWN: "Down|S",
        }

    def wants_key(self, event: QKeyEvent) -> bool:
        """Claim keys the tool needs contextually, vetoing the global shortcut.

        Without this, Qt fires the ``WindowShortcut`` QActions first (Route on
        the reroute key, Library on the autoroute key, the global Delete), none
        of which is wanted mid wire-edit or with something selected.  We decide
        *dynamically* — by matching the event against the tool's action ids
        resolved through the current :class:`KeyMap` — so rebinding any shortcut
        is honoured.  Returning True on a ``ShortcutOverride`` makes Qt deliver
        the key as a plain ``KeyPress`` to :meth:`on_key`.

        Pick-confirm / cancel / undo (Enter / Esc / Backspace) are raw,
        non-action keys that aren't global shortcuts, so they reach the tool
        anyway and need no veto here.
        """
        # An armed transform (float-rotate / keyboard-move) claims its step +
        # action keys, resolved through the KeyMap — so a global QAction (Route
        # on the step-up/WASD key, Select on 'S', …) never shadows the live
        # mode.  All ids, no raw Qt.Key literals.
        if self._armed is not None and any(
            self._key_matches(event, a) for a in _ARMED_ACTION_IDS
        ):
            return True
        # A selected wire-edit fragment claims its operation keys.
        if self._wire_edit is not None and (
            self._key_matches(event, _ACT_REROUTE)
            or self._key_matches(event, _ACT_AUTOROUTE)
            or self._key_matches(event, _ACT_FLIP_FRAGMENT)
            or self._key_matches(event, _ACT_DELETE)
        ):
            return True
        # Delete acts on the current selection — claim it so the global delete
        # action doesn't shadow the tool's handler.
        if self._key_matches(event, _ACT_DELETE) and not self._selection.is_empty():
            return True
        return False

    def wants_wheel(self, event: QWheelEvent) -> bool:
        """Claim the wheel only when the armed sub-tool consumes it (rotate)."""
        return self._armed is not None and self._armed.wants_wheel(event)

    def on_wheel(self, event: QWheelEvent) -> None:
        """Route a wheel tick to the armed sub-tool (it steps / declines)."""
        if self._armed is None:
            return
        if self._armed.on_wheel(event) and self._armed.finished:
            self._disarm()

    # -- rotate / flip ------------------------------------------------------

    @traced("SelectTool.begin_rotate_around_point")
    def begin_rotate_around_point(self) -> None:
        if self._selection.is_empty():
            self._status("Rotate around point: select component(s) or a group first")
            return
        self._pivot_capture_pending = True
        self._rotate_pivot = None
        self._status("Click a pivot point  ·  [Esc] Cancel", 0)

    def _clear_pivot(self) -> None:
        self._pivot_capture_pending = False
        self._rotate_pivot = None

    # -- armed transform sub-tools ([R] rotate / [G] move) ------------------

    def _disarm(self, *, cancel: bool = False) -> None:
        """Drop the active armed sub-tool, refreshing the view.

        ``cancel=True`` reverts a still-live sub-tool first (used on deactivate /
        tool switch).  Also drops the keyboard-move's parked DragController and
        clears the conflict corner.
        """
        armed = self._armed
        if armed is None:
            return
        if cancel and not armed.finished:
            armed.cancel()
        self._armed = None
        self._mouse = None
        self._clear_pivot_markers()
        self._clear_drag_corner()
        self._refresh_highlight()
        if self._view is not None:
            self._view.refresh()

    @traced("SelectTool.invoke_armed_atomic")
    def _invoke_armed_atomic(
        self,
        arm: "Callable[[], None]",
        action: "Callable[[ArmedSubTool], None]",
    ) -> bool:
        """Run one armed-sub-tool action atomically: arm → action → accept.

        The programmatic form of the ``[arm, action, disarm]`` shape (for menus
        / macros): *arm* enters a sub-tool, *action* runs one operation on it
        (e.g. a 90° step, a flip), then we try to ``accept``.  Per the
        conditional-disarm contract, ``accept`` commits + finishes **only when
        committable** — so if *action* left a conflict the sub-tool **stays
        armed**, parked for the user to resolve (exactly like an interactive
        accept), and this returns ``False``.  Returns ``True`` when it committed
        and disarmed.

        No-op (returns ``False``) if *arm* failed to arm anything — e.g. an empty
        selection — so callers needn't pre-check.  When something else is already
        armed, that mode is cancelled first so the atomic call starts clean.
        """
        if self._armed is not None:
            self._disarm(cancel=True)
        arm()
        armed = self._armed
        if armed is None:
            return False  # arm declined (e.g. empty / non-transformable selection)
        action(armed)
        armed.accept()
        if armed.finished:
            self._disarm()
            return True
        return False  # parked in a conflict — left armed for the user

    @traced("SelectTool.arm_rotate")
    def arm_rotate(self) -> None:
        """Arm float-rotate on the selection — the ``[R]`` entry point.

        The selection stays live: wheel / ``[↑]`` / ``[↓]`` / ``[R]`` step ±90°
        (previewed off the undo stack), clicking a pad sets a shared pivot,
        ``[Enter]`` accepts (commits ONE rotation **only if** the result is
        committable, else parks armed in the conflict) and ``[Esc]`` cancels.
        A legacy ``begin_rotate_around_point`` pivot capture seeds the pivot.
        """
        from typing import cast

        from protoboard.gui.selection.armed_rotate import ArmedRotate
        from protoboard.gui.selection.drag_controller import DragContext

        if self._armed is not None:
            return
        if self._selection.is_empty():
            self._status("Rotate: select component(s) or a group first")
            return
        comp_ids, group_id = self._rotatable_affected()
        if not comp_ids:
            self._status("Rotate: nothing rotatable in the selection")
            return
        self._cancel_gesture()  # drop any stray drag/pick first
        seed = self._rotate_pivot if isinstance(self._rotate_pivot, Point) else None
        self._pivot_capture_pending = False
        self._armed = ArmedRotate(
            cast("DragContext", self._session),
            comp_ids=comp_ids,
            group_id=group_id,
            snap=self._snap_free,
            commit=self._execute_command,
            matches=self._key_matches,
            # CW = step-up / the rotate action; CCW = step-down.
            cw_ids=(_ACT_STEP_UP, _ACT_ROTATE),
            ccw_ids=(_ACT_STEP_DOWN,),
            pivot=seed,
            preview=self._rotate_preview,
            status=lambda msg: self._status(msg, 0),
        )
        self._draw_pivot_markers()  # show the pivot(s) the instant we arm
        self._status(self._armed.status_hint(), 0)

    def _rotatable_affected(self) -> tuple[frozenset[uuid.UUID], uuid.UUID | None]:
        """The components a rotate would actually move (+ a whole-group id).

        A selected group rotates as a monolith (all its components, group id
        returned); otherwise loose components not locked in a sub-group.
        """
        group_id = self._selected_group_id
        if group_id is not None:
            from protoboard.domain.layout.group import all_component_ids

            return (
                all_component_ids(self._session.layout.groups, group_id),
                group_id,
            )
        loose = frozenset(
            cid
            for cid in self._selected_ids
            if not self._is_locked_in_group(cid)
            and self._session.layout.placements.get(cid) is not None
        )
        return loose, None

    def _snap_free(self, raw: Point) -> Point | None:
        """Snap a click to the grid for the rotate pivot (no exclusions)."""
        if self._view is None:
            return None
        from PySide6.QtCore import QPointF

        return self._view.snap(QPointF(raw.x, raw.y))

    @traced("SelectTool.arm_move")
    def arm_move(self) -> None:
        """Arm keyboard float-move on the selection — the ``[G]`` entry point.

        Builds the same :class:`DragController` a mouse drag would (snap /
        conflict-classify / live preview), driven by the keyboard: arrows / WASD
        step the anchor one grid cell.  ``[Enter]`` drops (only when
        committable), ``[Esc]`` cancels.
        """
        from protoboard.gui.selection.armed_move import ArmedMove

        if self._armed is not None:
            return
        if self._selection.is_empty():
            self._status("Move: select component(s) or a group first")
            return
        sel = self._primary_selectable()
        if sel is None:
            self._status("Move: nothing movable in the selection")
            return
        anchor_pt = self._move_anchor_point(sel)
        if anchor_pt is None:
            self._status("Move: selection has no placed anchor")
            return
        drag = self._arm_drag(sel, anchor_pt)
        if drag is None:
            self._status("Move: selection is not draggable")
            return
        self._mouse = drag  # parked so the drag-preview glide path is shared
        self._armed = ArmedMove(
            drag,
            anchor=Point(anchor_pt.x(), anchor_pt.y()),
            matches=self._key_matches,
            steps={
                _ACT_STEP_LEFT: (-1.0, 0.0),
                _ACT_STEP_RIGHT: (1.0, 0.0),
                _ACT_STEP_UP: (0.0, -1.0),
                _ACT_STEP_DOWN: (0.0, 1.0),
            },
        )
        self._status(self._armed.status_hint(), 0)

    def _move_anchor_point(self, sel: Selectable) -> QPointF | None:
        """The current world position of the drag anchor, as a QPointF.

        Keyboard-move grabs the anchor at its own centre (zero grab offset), so
        ``_arm_drag`` receives this as the "press" and derives grab = 0.
        """
        anchor_id = self._anchor_component(sel)
        if anchor_id is None:
            return None
        pl = self._session.layout.placements.get(anchor_id)
        if pl is None:
            return None
        return QPointF(pl.position.x, pl.position.y)

    def _selected_comp_ids(self) -> frozenset[uuid.UUID]:
        import uuid as _uuid

        out: set[_uuid.UUID] = set()
        for sel in self._selection.selected():
            hit = sel.to_hit()
            if hit.kind == "component" and isinstance(hit.id, _uuid.UUID):
                out.add(hit.id)
        return frozenset(out)

    @traced("SelectTool._rotate_selected")
    def _rotate_selected(self, delta: int = 90) -> None:
        from protoboard.domain.commands.group_cmds import (
            RotateAroundPointCommand,
            RotateComponentsCommand,
        )

        comp_ids = self._selected_comp_ids()
        if not comp_ids:
            return
        if self._rotate_pivot is not None:
            cmd: object = RotateAroundPointCommand(
                comp_ids=comp_ids, origin=self._rotate_pivot, delta=delta % 360  # type: ignore[arg-type]
            )
        else:
            cmd = RotateComponentsCommand(comp_ids=comp_ids, delta=delta % 360)
        self._execute(cmd)

    @traced("SelectTool._flip_selected")
    def _flip_selected(self) -> None:
        from protoboard.domain.commands.base import MacroCommand
        from protoboard.domain.commands.component_cmds import MoveComponentCommand
        from protoboard.domain.layout.placement import flip_component

        cmds: list[object] = []
        for cid in self._selected_comp_ids():
            pl = self._session.layout.placements.get(cid)
            inst = self._session.instances.get(cid)
            if pl is None or inst is None:
                continue
            defn = self._session.components.get(inst.definition_ref)
            if defn is None or defn.footprint.pin_count() != 2:
                continue
            cmds.append(
                MoveComponentCommand(
                    component_id=cid,
                    new_placement=flip_component(defn.footprint, pl),
                    old_placement=pl.copy(),
                )
            )
        if not cmds:
            return
        self._execute(MacroCommand(cmds, description=f"Flip {len(cmds)} components"))  # type: ignore[arg-type]

    # -- legacy multi rotate / flip / redrag entry points -------------------
    #
    # ``main_window`` branches on the selection shape: a multi/loose set goes
    # through ``_rotate_multi`` / ``_flip_multi`` (group-aware + group-locked
    # filter + crossing-wire cleanup); ``_begin_rotate_around_point`` and
    # ``_start_redrag`` are the underscore-named aliases the menu/PPM call.

    def _remove_crossing_wires(
        self, moved_comp_ids: set[uuid.UUID]
    ) -> list[object]:
        """RemoveWireCommands for wires crossing alien pads after a move.

        Does not execute — the caller folds them into a macro.  Shows a
        status-bar note when wires are dropped.  Verbatim port of the legacy
        tail used by every multi-move/rotate path.
        """
        from protoboard.domain.commands.component_cmds import RemoveWireCommand

        bad_ids = self._session.conflict_scanner().find_wires_crossing_pads(
            wires=list(self._session.layout.wires),
            moved_comp_ids=moved_comp_ids,
        )
        if not bad_ids:
            return []
        id_set = set(bad_ids)
        cmds: list[object] = [
            RemoveWireCommand(wire_id=w.id)
            for w in self._session.layout.wires
            if w.id in id_set
        ]
        self._status_msg(f"Removed {len(cmds)} colliding wire(s) after move.")
        return cmds

    def _apply_rotate(self, rotate_cmd: object, affected_ids: set[uuid.UUID]) -> None:
        """Execute a rotate command, drop wires it pushed across alien pads, refresh."""
        from protoboard.domain.commands.base import MacroCommand

        self._execute(rotate_cmd)
        remove_cmds = self._remove_crossing_wires(affected_ids)
        if remove_cmds:
            self._execute(
                MacroCommand(remove_cmds, description="Remove crossing wires")  # type: ignore[arg-type]
            )
        self._refresh_highlight()

    def _rotate_multi(self, delta: int = 90) -> None:
        from protoboard.domain.commands.group_cmds import (
            RotateAroundPointCommand,
            RotateComponentsCommand,
            RotateGroupCommand,
        )

        group_id = self._selected_group_id
        if group_id is not None:
            from protoboard.domain.layout.group import all_component_ids

            affected_ids = all_component_ids(self._session.layout.groups, group_id)
        else:
            affected_ids = frozenset(
                cid
                for cid in self._selected_ids
                if not self._is_locked_in_group(cid)
                and self._session.layout.placements.get(cid) is not None
            )
        if not affected_ids:
            return

        # A captured pivot overrides the default origin; otherwise group →
        # around group origin, loose → each in place.
        rotate_cmd: object
        if self._rotate_pivot is not None:
            rotate_cmd = RotateAroundPointCommand(
                comp_ids=frozenset(affected_ids),
                origin=self._rotate_pivot,  # type: ignore[arg-type]
                delta=delta % 360,
            )
        elif group_id is not None:
            rotate_cmd = RotateGroupCommand(group_id=group_id, delta=delta % 360)
        else:
            rotate_cmd = RotateComponentsCommand(
                comp_ids=frozenset(affected_ids), delta=delta % 360
            )
        self._apply_rotate(rotate_cmd, set(affected_ids))

    def _flip_multi(self) -> None:
        from protoboard.domain.commands.base import MacroCommand
        from protoboard.domain.commands.component_cmds import MoveComponentCommand
        from protoboard.domain.layout.placement import flip_component

        ids = [
            cid for cid in self._selected_ids if not self._is_locked_in_group(cid)
        ]
        cmds: list[object] = []
        for cid in ids:
            pl = self._session.layout.placements.get(cid)
            inst = self._session.instances.get(cid)
            if pl is None or inst is None:
                continue
            defn = self._session.components.get(inst.definition_ref)
            if defn is None or defn.footprint.pin_count() != 2:
                continue
            cmds.append(
                MoveComponentCommand(
                    component_id=cid,
                    new_placement=flip_component(defn.footprint, pl),
                    old_placement=pl.copy(),
                )
            )
        if not cmds:
            return
        self._execute(
            MacroCommand(cmds, description=f"Flip {len(cmds)} components")  # type: ignore[arg-type]
        )

    def _begin_rotate_around_point(self) -> None:
        """Underscore-named alias for :meth:`begin_rotate_around_point` (menu/PPM)."""
        self.begin_rotate_around_point()

    def _start_redrag(self, comp_id: uuid.UUID | None = None) -> None:
        """Underscore-named alias for :meth:`begin_redrag` (menu/PPM)."""
        self.begin_redrag(comp_id)

    # -- wire-side ops / wire-pin eligibility / wire hit-test ---------------

    def _hit_test_wire(self, scene_pt: QPointF) -> uuid.UUID | None:
        """Nearest wire id within ``WIRE_HIT_DIST`` (SimplifyTool's hook)."""
        return self._hit_tester()._hit_wire(scene_pt.x(), scene_pt.y())

    def _is_editing_wire(self) -> bool:
        """True when a wire fragment is selected (the wire-edit ``WireEditState``)."""
        return self._wire_edit is not None

    def _endpoint_eligible_for_wire_pin(self, position: Point) -> bool:
        """True iff *position* sits on a plated-through pad (wire-pin invariant)."""
        for p in self._session.cached_plated_through_pads():
            if abs(p.x - position.x) < 1e-6 and abs(p.y - position.y) < 1e-6:
                return True
        return False

    def _flip_wire_fragment(self, target_side: str | None = None) -> None:
        """Flip the selected wire *fragment* to the opposite layer — ``[F]``.

        A wire-edit op (alongside reroute / autoroute / delete): re-sides the
        selected contiguous fragment of one wire (boundary vertices become
        vias) via :class:`FlipWireFragmentCommand`.  Same-wire only — a
        cross-wire selection has no single wire to flip.  ``target_side``
        defaults to the opposite of the wire's current side.  Falls back to the
        whole-wire layer move when no fragment is armed.
        """
        from protoboard.gui.selection.wire_edit import build_flip_fragment_command

        state = self._wire_edit
        if state is None:
            self._move_selected_wires_to_side(target_side)
            return
        cmd = build_flip_fragment_command(
            list(self._session.layout.wires), state, target_side
        )
        if cmd is None:
            self._status_msg(
                "Flip: pick a fragment on a single wire (two vertices apart)."
            )
            return
        self._wire_edit_clear()
        self._execute_command(cmd)
        self._refresh_after_wire_op()

    def _move_selected_wires_to_side(self, target_side: str | None) -> None:
        """Move all selected wires to a layer (``None`` toggles a shared side)."""
        from protoboard.domain.commands.base import MacroCommand
        from protoboard.domain.commands.component_cmds import SetWireSideCommand

        wire_ids = self._selected_by_kind("wire")
        if not wire_ids:
            return
        wires = [w for w in self._session.layout.wires if w.id in wire_ids]
        if not wires:
            return

        if target_side is None:
            sides = {w.side for w in wires}
            if len(sides) > 1:
                self._status_msg(
                    "Cannot flip: selected wires are on different layers."
                )
                return
            current_side = next(iter(sides))
            resolved_side = "bottom" if current_side == "top" else "top"
        else:
            resolved_side = target_side

        wires_to_move = [w for w in wires if w.side != resolved_side]
        if not wires_to_move:
            self._status_msg(f"Selected wires already on {resolved_side} layer.")
            return

        raw: list[object] = [
            SetWireSideCommand(wire_id=w.id, side=resolved_side)
            for w in wires_to_move
        ]
        if len(raw) == 1:
            cmd: object = raw[0]
        else:
            desc = f"Move {len(raw)} wires to {resolved_side}"
            cmd = MacroCommand(raw, description=desc)  # type: ignore[arg-type]
        self._exec_command(cmd)
        self._session.layout_changed.emit()
        self._status_msg(
            f"Moved {len(wires_to_move)} wire(s) to {resolved_side} layer."
        )

    # -- group creation dialog (Ctrl+G) -------------------------------------

    def _select_group(self, group_id: uuid.UUID) -> None:
        """Select a single group as a monolith (post-create / post-merge)."""
        self._set_selection_from_ids(set(), set(), {group_id})

    def _group_for_selection(self) -> uuid.UUID | None:
        """Group the current selection: dialog to create new or add to existing.

        Verbatim port of the legacy method; only the selection-state reads use
        the v2 accessors.  All collaborators (GroupTree.promote_selection_to_top_level,
        external_pins_for_group, the group commands, GroupTargetDialog) are reused.
        """
        ids = frozenset(self._selected_ids)
        sel_id = self._selected_id
        if sel_id and sel_id not in ids:
            ids = ids | {sel_id}
        if not ids:
            return None

        from protoboard.domain.commands.group_cmds import (
            AddToGroupCommand,
            CreateGroupCommand,
            RemoveFromGroupCommand,
            SetExportedPinsCommand,
        )
        from protoboard.domain.layout.group import ComponentGroup
        from protoboard.domain.layout.group_tree import GroupTree
        from protoboard.domain.schematic.net import PinRef
        from protoboard.gui.dialogs.group_target_dialog import GroupTargetDialog

        groups = self._session.layout.groups
        active_gid = self._current_group_id
        active = groups.get(active_gid)
        child_groups = [
            groups[gid]
            for gid in (active.child_group_ids if active else frozenset())
            if gid in groups and gid != ROOT_GROUP_ID
        ]

        default_name = f"Group{len(groups) + 1}"
        dlg = GroupTargetDialog(
            existing_groups=child_groups,
            default_name=default_name,
            parent=self._view.window(),
        )
        if dlg.exec() != GroupTargetDialog.DialogCode.Accepted:
            return None

        if dlg.mode == "new":
            promoted_free, promoted_groups = (
                self._session.group_tree().promote_selection_to_top_level(
                    active_gid, ids
                )
            )
            _log.info(
                "group_for_selection.promotion",
                active_gid=str(active_gid)[:8],
                n_selected=len(ids),
                n_free_after=len(promoted_free),
                n_promoted_groups=len(promoted_groups),
            )
            new_group_id = uuid.uuid4()
            exported: frozenset[PinRef] = frozenset()
            if dlg.auto_export_external:
                transient = ComponentGroup(
                    id=new_group_id,
                    name=dlg.new_name or default_name,
                    component_ids=promoted_free,
                    child_group_ids=promoted_groups,
                )
                groups_with_new = dict(self._session.layout.groups)
                groups_with_new[new_group_id] = transient
                exported = GroupTree(groups_with_new).external_pins_for_group(
                    new_group_id, self._session.schematic
                )
            new_group = ComponentGroup(
                id=new_group_id,
                name=dlg.new_name or default_name,
                component_ids=promoted_free,
                child_group_ids=promoted_groups,
                exported_pins=exported,
            )
            self._exec_command(CreateGroupCommand(group=new_group))
            for promoted_gid in promoted_groups:
                old_parent = self._session.group_tree().parent_group_of(promoted_gid)
                if old_parent is not None:
                    self._exec_command(
                        RemoveFromGroupCommand(
                            group_id=old_parent,
                            child_group_ids=frozenset({promoted_gid}),
                        )
                    )
            self._exec_command(
                AddToGroupCommand(
                    group_id=active_gid, child_group_ids=frozenset({new_group.id})
                )
            )
            if promoted_free:
                self._exec_command(
                    RemoveFromGroupCommand(
                        group_id=active_gid, component_ids=promoted_free
                    )
                )
            self._session.layout_changed.emit()
            self._select_group(new_group.id)
            return new_group.id

        # Add to an existing group.
        target_id = dlg.target_group_id
        if target_id is None:
            return None
        self._exec_command(
            AddToGroupCommand(group_id=target_id, component_ids=ids)
        )
        if target_id != active_gid:
            self._exec_command(
                RemoveFromGroupCommand(group_id=active_gid, component_ids=ids)
            )
        if dlg.auto_export_external:
            new_exported = self._session.group_tree().external_pins_for_group(
                target_id, self._session.schematic
            )
            if new_exported != self._session.layout.groups[target_id].exported_pins:
                self._exec_command(
                    SetExportedPinsCommand(
                        group_id=target_id, new_exported=new_exported
                    )
                )
        self._session.layout_changed.emit()
        self._select_group(target_id)
        return target_id

    # -- worker-backed wire operations (simplify / unweave / merge) ---------
    #
    # The heavy algorithms (generic_simplify_wire, unweave_wires, the fuse +
    # fragment-coalesce dance) already live in services; these methods prepare
    # read-only inputs and launch the worker, then apply the result on the
    # main thread.  Simplify's input prep is extracted to
    # :func:`build_simplify_inputs` (it used to be inline bloat predating the
    # session caches); unweave/merge read session state directly.

    def _simplify_wires(
        self,
        wire_ids: set[uuid.UUID] | frozenset[uuid.UUID],
        *,
        mode_override: str | None = None,
    ) -> None:
        """Run generic_simplify_wire on each wire id (in a worker), apply changes."""
        from protoboard.gui.selection.wire_ops import build_simplify_inputs
        from protoboard.gui.workers.operation_worker import run_with_progress
        from protoboard.services.routing.postprocess import (
            SimplifyMode,
            generic_simplify_wire,
        )

        session = self._session
        if mode_override == "chebyshev":
            mode = SimplifyMode.CHEBYSHEV
        elif mode_override == "bare_parity":
            mode = SimplifyMode.BARE_PARITY
        else:
            mode = getattr(
                session.routing_settings, "simplify_mode", SimplifyMode.BARE_PARITY
            )

        inputs = build_simplify_inputs(session)
        all_wires = list(session.layout.wires)
        wid_list = list(wire_ids)

        def _simplify_in_worker(progress_cb):  # type: ignore[no-untyped-def]
            results: list[tuple[uuid.UUID, list[Point], list[Point]]] = []
            n = len(wid_list)
            for i, wid in enumerate(wid_list):
                wire = next((w for w in all_wires if w.id == wid), None)
                if wire is None:
                    progress_cb(i + 1, n)
                    continue
                new_wire = generic_simplify_wire(
                    wire,
                    other_wires=[w for w in all_wires if w.id != wid],
                    pin_positions=inputs.pin_positions,
                    pad_to_net=inputs.pad_to_net,
                    mode=mode,
                    board_pads=inputs.board_pads,
                    wire_net_id=inputs.wire_to_net.get(wid),
                    other_wire_nets=inputs.wire_to_net,
                    pin_net_map=inputs.pad_to_net,
                    pad_index=inputs.pad_index,
                )
                if new_wire is not wire and new_wire.path != wire.path:
                    results.append((wid, list(new_wire.path), list(wire.path)))
                progress_cb(i + 1, n)
            return results

        worker_out = run_with_progress(
            parent=self._view,
            title="Simplify",
            fn=_simplify_in_worker,
            label=f"Simplifying {len(wid_list)} wire(s)…",
            with_progress=True,
        )
        if worker_out is None:
            return

        from protoboard.gui.selection.wire_edit import replace_wire_path_cmd

        cmds: list[object] = []
        for wid, new_path, orig_path in worker_out:
            wire = next((w for w in session.layout.wires if w.id == wid), None)
            if wire is None:
                continue  # vanished mid-flight (concurrent edit)
            cmds.append(
                replace_wire_path_cmd(session.layout, wire, new_path, orig_path)
            )
        if not cmds:
            self._status_msg("Simplify: nothing to shorten.")
            return
        self._commit_wire_macro(cmds, f"Simplify {len(cmds)} wire(s)")

    def _unweave_wires(
        self, wire_ids: set[uuid.UUID] | frozenset[uuid.UUID]
    ) -> None:
        """Topologically un-weave mutual crossings of the selected wires."""
        from protoboard.domain.commands.component_cmds import (
            AddWireCommand,
            RemoveWireCommand,
        )
        from protoboard.gui.workers.operation_worker import run_with_progress
        from protoboard.services.routing.unweave import (
            count_mutual_crossings,
            unweave_wires,
        )

        session = self._session
        targets = [w for w in session.layout.wires if w.id in wire_ids]
        if len(targets) < 2:
            self._status_msg("Unweave: at least 2 valid wires required.")
            return
        before = count_mutual_crossings(targets)
        if before == 0:
            self._status_msg("Unweave: no crossings to resolve.")
            return

        schematic = session.schematic
        graph = session.connectivity_graph()
        allow_layer_change = session.routing_settings.allow_layer_change
        allow_side_flip = session.routing_settings.visual_unweave_side_flip

        def _unweave_in_worker() -> object:
            return unweave_wires(
                targets,
                schematic=schematic,
                graph=graph,
                allow_layer_change=allow_layer_change,
                allow_side_flip=allow_side_flip,
            )

        out = run_with_progress(
            parent=self._view,
            title="Unweave",
            fn=_unweave_in_worker,
            label=f"Unweaving {len(targets)} wire(s)…",
        )
        if out is None:
            return
        after = count_mutual_crossings(out)

        same = len(out) == len(targets) and all(
            a.path == b.path and a.side == b.side and a.insulated == b.insulated
            for a, b in zip(targets, out)
        )
        if same:
            self._status_msg(
                f"Unweave: {before} crossings already at topological minimum."
            )
            return

        cmds: list[object] = [RemoveWireCommand(wire_id=w.id) for w in targets]
        cmds += [AddWireCommand(wire=w) for w in out]
        self._commit_wire_macro(
            cmds, f"Unweave {len(targets)} → {len(out)} wire(s)", invalidate=True
        )
        seg = (
            f" ({len(targets)}→{len(out)} segments)"
            if len(targets) != len(out)
            else ""
        )
        self._status_msg(f"Unweave: {before} → {after} crossings{seg}")

    def _merge_wires_and_fragments(
        self, wire_ids: frozenset[uuid.UUID] | None
    ) -> None:
        """Two-phase wire merge — fuse touching wires + coalesce same-side fragments."""
        from typing import cast

        from protoboard.domain.commands.base import ComponentState
        from protoboard.domain.commands.component_cmds import (
            FuseTouchingWiresCommand,
            ReplaceWireFragmentsCommand,
        )
        from protoboard.domain.layout.wire import Fragment
        from protoboard.domain.layout.wire_utils import coalesce_same_side_fragments

        session = self._session
        state = cast("ComponentState", session)

        # Phase 1: apply the fuse to observe the post-fuse layout, build the
        # phase-2 fragment commands against it, then revert so the final macro
        # re-applies both phases atomically.
        fuse_cmd = FuseTouchingWiresCommand(wire_ids=wire_ids)
        fuse_cmd.apply(state)
        n_fused = len(fuse_cmd._records)  # noqa: SLF001 — legacy command contract

        if wire_ids is None:
            post_fuse_ids: set[uuid.UUID] = {w.id for w in session.layout.wires}
        else:
            survived = {wid for wid in wire_ids if wid in session.layout.wires}
            merged = {
                rec.merged_wire_id
                for rec in fuse_cmd._records  # noqa: SLF001
                if rec.merged_wire_id is not None
            }
            post_fuse_ids = survived | merged

        all_wires = session.layout.wires
        frag_cmds: list[object] = []
        n_fragment_wires = 0
        n_fragments_removed = 0
        for wid in post_fuse_ids:
            wire = next((w for w in all_wires if w.id == wid), None)
            if wire is None or len(wire.fragments) < 2:
                continue
            coalesced = coalesce_same_side_fragments(wire.fragments)
            if len(coalesced) == len(wire.fragments):
                continue
            n_fragment_wires += 1
            n_fragments_removed += len(wire.fragments) - len(coalesced)
            session.layout.ensure_wire_knots(wire)
            old_fragments = tuple(
                Fragment(path=list(f.path), side=f.side) for f in wire.fragments
            )
            frag_cmds.append(
                ReplaceWireFragmentsCommand(
                    wire_id=wire.id,
                    new_fragments=tuple(coalesced),
                    old_fragments=old_fragments,
                )
            )

        fuse_cmd.revert(state)

        if n_fused == 0 and not frag_cmds:
            scope_label = "selection" if wire_ids is not None else "layout"
            self._status_msg(f"Merge wires: nothing to merge in {scope_label}.")
            return

        parts_cmds: list[object] = []
        if n_fused > 0:
            parts_cmds.append(fuse_cmd)
        parts_cmds += frag_cmds
        desc = f"Merge wires ({n_fused} fused, {n_fragment_wires} fragment-collapsed)"
        self._commit_wire_macro(parts_cmds, desc)

        parts = []
        if n_fused:
            parts.append(f"fused {n_fused} wire pair(s)")
        if n_fragment_wires:
            parts.append(
                f"collapsed {n_fragments_removed} fragment boundaries in "
                f"{n_fragment_wires} wire(s)"
            )
        self._status_msg("Merge wires: " + "; ".join(parts) + ".")

    def _optimize_selected_wires(
        self,
        wire_ids: set[uuid.UUID] | frozenset[uuid.UUID],
        opt_name: str,
    ) -> None:
        """Run a registered WireOptimizer plugin on the selected wires.

        Builds an immutable RouteRequest + a synthetic AutorouteResult of just
        the selected wires, runs the optimizer, then diffs input vs output into
        Remove/Add commands.  Synchronous (PPM optimize-with acts on a handful
        of wires).  Port of the legacy method; collaborators reused verbatim.
        """
        from protoboard.domain.commands.component_cmds import (
            AddWireCommand,
            RemoveWireCommand,
        )
        from protoboard.services.routing.autorouter import AutorouteResult
        from protoboard.services.routing.optimizer import registry as _opt_registry
        from protoboard.services.routing.protocol import ProgressSink
        from protoboard.services.routing.snapshot import RouteRequest

        session = self._session
        if not wire_ids:
            self._status_msg("Optimize: nothing selected.")
            return
        try:
            optimizer = _opt_registry.get(opt_name)
        except KeyError:
            self._status_msg(f"Optimize: plugin {opt_name!r} is not registered.")
            return

        request = RouteRequest.from_session(
            board=session.board,
            layout=session.layout,
            schematic=session.schematic,
            components=session.components,
            instances=session.instances,
            routing_settings=session.routing_settings,
        )
        selected_wires = [w for w in session.layout.wires if w.id in wire_ids]
        if not selected_wires:
            self._status_msg("Optimize: no matching wires in layout.")
            return
        result_in = AutorouteResult(
            wires=list(selected_wires), unrouted_nets=[], pass_stats=[]
        )
        try:
            result_out = optimizer.optimize(request, result_in, ProgressSink())
        except Exception as exc:  # noqa: BLE001
            self._status_msg(f"Optimize: plugin failed — {exc}")
            return

        out_by_id = {w.id: w for w in result_out.wires}
        cmds: list[object] = []
        for wire_in in selected_wires:
            wire_out = out_by_id.pop(wire_in.id, None)
            if wire_out is None:
                cmds.append(RemoveWireCommand(wire_id=wire_in.id))
                continue
            if (
                wire_out.path == wire_in.path
                and wire_out.side == wire_in.side
                and wire_out.insulated == wire_in.insulated
            ):
                continue
            # Side/insulation change → full Remove+Add (path-only could reuse
            # ReplaceWireFragments, but mixing complicates undo grouping).
            cmds.append(RemoveWireCommand(wire_id=wire_in.id))
            cmds.append(AddWireCommand(wire=wire_out))
        for new_wire in out_by_id.values():
            cmds.append(AddWireCommand(wire=new_wire))

        label = getattr(optimizer, "display_name", opt_name)
        if not cmds:
            self._status_msg(f"Optimize ({label}): nothing changed.")
            return
        self._commit_wire_macro(
            cmds, f"Optimize {len(selected_wires)} wire(s) via {label}"
        )

    def _commit_wire_macro(
        self, cmds: list[object], description: str, *, invalidate: bool = False
    ) -> None:
        """Execute one-or-many wire commands as a macro, then refresh the view."""
        from protoboard.domain.commands.base import MacroCommand

        if not cmds:
            return
        cmd = cmds[0] if len(cmds) == 1 else MacroCommand(cmds, description=description)  # type: ignore[arg-type]
        self._exec_command(cmd)
        if invalidate:
            self._session.invalidate_graph()
        self._session.layout_changed.emit()
        self._refresh_after_wire_op()

    def _exec_command(self, cmd: object) -> None:
        """Execute one command against the session (no emit/refresh).

        Lets a multi-command path (group dialog, wire-side move) issue several
        commands and emit ``layout_changed`` once at the end, as the legacy
        tool did.  Casts the session to the ``ComponentState`` command target.
        """
        from typing import cast

        from protoboard.domain.commands.base import Command, ComponentState

        self._session.stack.execute(
            cast("Command[ComponentState]", cmd),
            cast("ComponentState", self._session),
        )

    @traced("SelectTool._execute")
    def _execute(self, cmd: object) -> None:
        self._exec_command(cmd)
        self._session.layout_changed.emit()
        self._refresh_highlight()

    # -- group navigation ---------------------------------------------------

    @traced("SelectTool.enter_group")
    def enter_group(self, group_id: uuid.UUID | None = None) -> bool:
        """Enter a group's edit context — its members become directly editable.

        ``group_id`` defaults to the selected group (``[E]`` on a group).  Pushes
        the group onto the navigation stack, syncs the view's active-group render
        context, and clears the selection (the old monolith no longer applies
        inside its own context).  Returns True when a group was entered.
        """
        gid = group_id if group_id is not None else self._selected_group_id
        if gid is None or gid not in self._session.layout.groups:
            return False
        if gid == self._current_group_id:
            return False
        self._group_stack.append(gid)
        self._sync_active_group()
        self._selection.clear()
        self._refresh_highlight()
        self._status("Entered group — [Esc] to leave")
        return True

    @traced("SelectTool.exit_group")
    def exit_group(self) -> None:
        if len(self._group_stack) > 1:
            self._group_stack.pop()
            self._sync_active_group()
            self._selection.clear()
            self._refresh_highlight()

    def _sync_active_group(self) -> None:
        """Push the current group context to the view so it re-renders."""
        if self._view is not None and hasattr(self._view, "set_active_group"):
            self._view.set_active_group(self._current_group_id)
