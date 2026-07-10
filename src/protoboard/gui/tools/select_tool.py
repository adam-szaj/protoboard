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
from PySide6.QtCore import Qt
from PySide6.QtGui import QColor

from protoboard.domain.layout.layout import ROOT_GROUP_ID
from protoboard.geometry import Point
from protoboard.gui.board_view_tool import _Tool
from protoboard.gui.highlight.controller import HighlightController
from protoboard.gui.ports import (
    LEFT_BUTTON,
    ActionOption,
    InteractionContext,
    KeyInput,
    PointerInput,
    StatusEvent,
    WheelInput,
)
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

    from collections.abc import Mapping

    from PySide6.QtGui import QKeyEvent
    from PySide6.QtWidgets import QGraphicsItem

    from protoboard.domain.components.definition import ComponentDefinition
    from protoboard.domain.components.instance import ComponentInstance
    from protoboard.domain.components.package import RedragResult
    from protoboard.domain.layout.group import ComponentGroup
    from protoboard.domain.layout.group_tree import GroupTree
    from protoboard.domain.layout.placement import ComponentPlacement
    from protoboard.domain.layout.wire import Wire
    from protoboard.gui.board_view_tool import AbstractBoardView
    from protoboard.gui.drag_set import DragSet
    from protoboard.gui.selection.adapters import WireVertexSelectable
    from protoboard.gui.selection.armed import ArmedSubTool
    from protoboard.gui.selection.mouse_input import MouseInputController
    from protoboard.gui.selection.selectable import Selectable
    from protoboard.gui.selection.wire_edit import WireEditState
    from protoboard.gui.session import EditorSession
    from protoboard.gui.tools.drag_commit_decision import DragCommitDecision
    from protoboard.gui.tools.drag_corner_text import DragCornerText
    from protoboard.gui.tools.select_preview import SelectPreview
    from protoboard.gui.tools.select_prompts import GroupTargetPrompt

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

# Mouse-chord actions (Stage 2): the armed transform's wheel stepping and the
# unified RMB context menu, layered over the view's zoom / canvas-menu.
_ACT_WHEEL_ROTATE = "select-tool.action.wheel_rotate"
_ACT_CONTEXT_MENU = "select-tool.action.context_menu"
# Global relation-cycle actions (backtick / Shift+backtick) — the ids live in
# keybindings.py with their own defaults; the tool dispatches them in on_key.
_ACT_RELATION_CYCLE = "highlight_cycle"
_ACT_RELATION_CYCLE_BACK = "relation_cycle_back"
# Every action the armed sub-tools dispatch / veto against (the superset).
_ARMED_ACTION_IDS: tuple[str, ...] = (
    _ACT_STEP_LEFT, _ACT_STEP_RIGHT, _ACT_STEP_UP, _ACT_STEP_DOWN,
    _ACT_ROTATE, _ACT_FLIP, _ACT_CANCEL,
)

def _segment_intersects_rect(
    a: Point, b: Point, rx0: float, ry0: float, rx1: float, ry1: float
) -> bool:
    """True if segment a→b intersects the axis-aligned rect [rx0,rx1]×[ry0,ry1].

    Used by the rubber-band's RTL wire catch: a wire whose endpoints are both
    outside the band but whose body crosses it still counts as touched.  Cheap
    Liang–Barsky-style clip against the four slabs.
    """
    dx, dy = b.x - a.x, b.y - a.y
    p = [-dx, dx, -dy, dy]
    q = [a.x - rx0, rx1 - a.x, a.y - ry0, ry1 - a.y]
    t0, t1 = 0.0, 1.0
    for pi, qi in zip(p, q, strict=True):
        if pi == 0:
            if qi < 0:
                return False  # parallel and outside this slab
        else:
            t = qi / pi
            if pi < 0:
                t0 = max(t0, t)
            else:
                t1 = min(t1, t)
            if t0 > t1:
                return False
    return True


class SelectTool(_Tool):
    """Selection tool built on the SelectionService / HitTester / Highlight stack."""

    def __init__(
        self,
        preview: SelectPreview | None = None,
        group_prompt: GroupTargetPrompt | None = None,
    ) -> None:
        super().__init__()  # sets _session / _view to None up-front
        self._selection: SelectionService = None  # type: ignore[assignment]
        self._highlight: HighlightController = None  # type: ignore[assignment]
        self._group_stack: list[uuid.UUID] = [ROOT_GROUP_ID]
        # The in-flight mouse interaction (drag, rubber-band, pick, …) or None.
        # All pointer + intent events route here uniformly; the controller owns
        # its own start threshold and outcome.
        self._mouse: MouseInputController | None = None
        self._press_pt: Point | None = None
        # Transient overlays (pivots, band, redrag, wire-edit, reroute) behind
        # the semantic preview seam.  An injected preview (tests) survives
        # re-activation; the default Qt one binds the current view's scene on
        # activate().
        self._preview: SelectPreview | None = preview
        self._preview_injected = preview is not None
        # The group-target decision (qt-ports D4) — injected callable; the
        # default Qt prompt (GroupTargetDialog) binds the view's window on
        # activate().
        self._group_prompt: GroupTargetPrompt | None = group_prompt
        self._group_prompt_injected = group_prompt is not None
        # A plain press that landed on an *already-selected* entity defers its
        # selection change to release: if the gesture turns out to be a click
        # (no drag), the release collapses the selection onto this entity — or
        # deselects it when it was the only thing selected.  None when the press
        # was on something not selected (selection already happened on press).
        self._deferred_click_sel: Selectable | None = None
        # The net context of the current selection — the single net whose pads
        # get the bright highlight.  Set only by a click that lands on a
        # *pin-pad* (→ that pin's net) or a *wire* (→ that wire's net); a body
        # click / rubber-band / group / empty press clears it.  Persists across
        # refresh / rotate / drag until the selection changes.  ``None`` ⇒ no
        # net context ⇒ no bright pad discs.
        self._context_net_id: uuid.UUID | None = None
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
        # overlay lives in the preview, the pick state lives in the service.
        self._redrag_comp_id: uuid.UUID | None = None
        self._redrag_pin: int | None = None
        # Wire-edit: the selected fragment (tool-local, like _rotate_pivot) plus
        # the cluster-cycle state threaded through the wire-vertex pick.  The
        # operations on the fragment (reroute / autoroute / delete) are actions
        # dispatched by `_wire_edit_on_key` once the fragment is selected.
        from protoboard.gui.selection.hit_test import WireEditCycle

        self._wire_edit: WireEditState | None = None
        self._wire_edit_cycle: WireEditCycle = WireEditCycle()
        self._wire_edit_wire_id: uuid.UUID | None = None  # wire [E] armed on
        self._wire_edit_start: WireVertexSelectable | None = None  # first vertex
        # Wires currently ghost-dimmed during a drag (drop / disconnect preview).
        # Tracked so a wire that stops being a drop-candidate — or the drag end —
        # is reset to full opacity, not left dimmed.
        self._ghosted_wire_ids: set[uuid.UUID] = set()
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

    @property
    def preview(self) -> SelectPreview:
        assert self._preview is not None  # set by activate() / injection
        return self._preview

    @traced()
    def activate(self, session: EditorSession, view: AbstractBoardView) -> None:
        self._session = session
        self._view = view
        if not self._preview_injected:
            from protoboard.gui.tools.select_preview import QtSelectPreview

            self._preview = QtSelectPreview(view.scene)
        if not self._group_prompt_injected:
            from protoboard.gui.tools.select_prompts import (
                qt_group_target_prompt,
            )

            self._group_prompt = qt_group_target_prompt(
                lambda: self._view.window()
            )
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

    @traced()
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
        # The scene was rebuilt — our overlay QGraphicsItems are gone; drop the
        # stale references so a later clear doesn't touch freed items.
        self.preview.on_scene_cleared()
        self._drag_corner = None

    # -- helpers ------------------------------------------------------------

    def _hit_tester(self) -> HitTester:
        from typing import cast

        from protoboard.types import Side

        return HitTester(
            self._session, camera_side=cast(Side, self._view.camera_side)
        )

    def _filters(self) -> SelectionFilters:
        """Translate the held :class:`SelectionFilter` into the hit-test's
        per-kind gate, so the status-bar filter actually excludes kinds from
        selection (a component-only filter ignores wire/group hits, etc.)."""
        from protoboard.gui.selection_axes import SelectionFilter

        f = self._filter
        return SelectionFilters(
            components=bool(f & SelectionFilter.COMPONENTS),
            wires=bool(f & SelectionFilter.WIRES),
            groups=bool(f & SelectionFilter.GROUPS),
        )

    @traced()
    def _pick_at(
        self, what: object, scene_pt: Point
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

            return PointSelectable(point=self._snap(scene_pt, frozenset()))
        if what == "wire-vertex":
            sel, cycle = self._hit_tester().hit_wire_vertex(
                scene_pt.x,
                scene_pt.y,
                start_wire_id=self._wire_edit_active_start_wire(),
                wire_net_map=self._session.cached_wire_net_map(),
                cycle=self._wire_edit_cycle,
            )
            self._wire_edit_cycle = cycle
            return sel
        return self._hit_tester().hit(scene_pt.x, scene_pt.y, self._filters())

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
        if self._view is None:
            return
        self._view.notify_status(
            StatusEvent("select.status", {"text": msg, "timeout_ms": timeout_ms})
        )

    @staticmethod
    def _decode_modifiers(mods: Mods) -> tuple[BasisScope, SetOp]:
        op = (
            SetOp.REMOVE if (mods.ctrl and mods.shift)
            else SetOp.ADD if mods.ctrl
            else SetOp.SET
        )
        basis = BasisScope.RELATION if mods.alt else BasisScope.SINGLE
        return basis, op

    # -- press --------------------------------------------------------------

    @traced()
    def on_press(self, inp: PointerInput) -> bool:
        if inp.button != LEFT_BUTTON:
            return False
        scene_pt = inp.scene_pos

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
        if self._armed is not None and self._armed.on_click(scene_pt):
            return True

        # 2. An active mouse interaction (drag / band / pick) owns the press.
        #    Record the press point so on_release can tell a click from a drag,
        #    then forward.  Picks act on the synthesised click (on_release);
        #    drag/band armed their own press at construction.
        if self._mouse is not None and not self._mouse.finished:
            self._press_pt = scene_pt
            self._mouse.on_press(scene_pt, inp.mods)
            return True

        # 2b. A fragment is selected but no pick is live (between picking the
        #     range and choosing an operation) — a plain board press leaves
        #     wire-edit and falls through to normal selection.
        if self._wire_edit is not None:
            self._wire_edit_clear()

        # 3. Normal selection. Resolve the hit.
        basis, op = self._decode_modifiers(inp.mods)
        sel = self._hit_tester().hit(scene_pt.x, scene_pt.y, self._filters())

        # Net context (drives the bright pad highlight) comes from the click
        # itself: a pin-pad click → that pin's net, a wire click → the wire's
        # net, anything else → none.  Set here, before the defer/toggle branches,
        # so re-clicking a *different pin* of an already-selected component still
        # moves the context to the new pin's net.
        self._context_net_id = self._compute_context_net(sel)

        #   Free-pin click feedback (status hint + light ring), or clear it when
        #   the press is not on a free pin-pad.  Runs for every press — including
        #   ``sel is None`` — so the ring never lingers.
        self._hint_free_pin_under(sel, scene_pt)

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
        if op is SetOp.SET and basis is not BasisScope.RELATION:
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

        # Alt+LMB (RELATION basis): the user explicitly asks to expand the
        # highlight/selection to the clicked object's whole net — apply it
        # immediately even when the object is already selected.  Without this,
        # the "click wire to select, then Alt+click to extend to the net"
        # sequence silently no-ops, because the plain-SET branch above would
        # defer it to a drag-or-release decision (legacy parity regression).

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

    @traced()
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
            # Union the targets into the current selection, relation-agnostic so
            # successive Ctrl+Alt clicks accumulate across *different* nets (the
            # FSM's add() only extends within one relation and toggles, which
            # dropped prior nets — the audit regression).  Rebuild from empty:
            # set_many re-set()s an already-selected first item OFF otherwise.
            union = list(self._selection.selected())
            seen = {s.to_hit() for s in union}
            for t in targets:
                if t.to_hit() not in seen:
                    union.append(t)
                    seen.add(t.to_hit())
            self._clear_selection_to_empty()
            self._selection.set_many(union)
        elif op == SetOp.REMOVE:
            # Phase 2 MVP: remove not yet a first-class FSM op here; treat as
            # a fresh SET so the click is never a silent no-op. Refined later.
            self._selection.set_many(targets)
        elif len(targets) > 1:
            self._selection.set_many(targets)
        else:
            self._selection.set(sel)
        self._refresh_highlight(primary=sel)
        # Broadcast to the panels: every click-driven selection change funnels
        # through here, so this is the one spot that keeps board -> panel sync
        # honest for plain mouse clicks (the highlight above is board-only).
        self._notify_selection()

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

    def _compute_context_net(self, sel: Selectable | None) -> uuid.UUID | None:
        """The net context a click on *sel* establishes (drives bright pads).

        A pin-pad hit (component Selectable with a ``pin_number``) → that pin's
        single net.  A wire hit → the wire's net.  A *body* component hit
        (``pin_number is None``), a group, or an empty press → ``None`` (no
        context, so no bright pad discs).  This is intentionally narrower than
        :meth:`_anchor_net_id`, which unions all of a component's nets.
        """
        if sel is None:
            return None
        hit = sel.to_hit()
        if hit.kind == "component" and hit.pin_number is None:
            return None  # body click — no net context
        return self._anchor_net_id(sel)

    @property
    def context_net_id(self) -> uuid.UUID | None:
        """The selection's net context (BoardView reads this to draw bright pads)."""
        return self._context_net_id

    def _clear_free_pad_ring(self) -> None:
        """Tell BoardView to drop the clicked-free-pad ring (no-op headless)."""
        setter = getattr(self._view, "set_clicked_free_pad", None)
        if setter is not None:
            setter(None)

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
        self._context_net_id = None
        self._refresh_highlight()
        self._notify_selection()

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
        """Set the correlation axis used by Alt-select and the auto-highlight.

        Emits ``session.highlight_mode_changed`` with the relation's label so the
        status bar reflects the active mode — the legacy contract (lost in the v2
        rewrite, restored by the parity test)."""
        from protoboard.gui.selection_axes import RELATION_LABELS, RelationClass

        if isinstance(relation, RelationClass):
            self._relation = relation
            self._refresh_highlight()
            if self._session is not None:
                self._session.highlight_mode_changed.emit(
                    RELATION_LABELS[relation]
                )

    def _cycle_relation(self, *, forward: bool = True) -> None:
        """Step the relation axis to the next/previous class — backtick / Shift+`.

        The keyboard counterpart to the status-bar relation widget; lost in the
        v2 rewrite (the action ids existed in the KeyMap but nothing dispatched
        them), restored by the parity test."""
        from protoboard.gui.selection_axes import RelationClass

        relations = list(RelationClass)
        idx = relations.index(self._relation)
        step = 1 if forward else -1
        self.set_relation(relations[(idx + step) % len(relations)])

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
        from protoboard.gui.selection_axes import SelectionFilter

        # Respect the object filter: an Alt+click relation-expansion includes
        # only the kinds the filter admits (wire filter → just the net's wires;
        # component filter → just its components) — spec rule 5.
        want_comps = bool(self._filter & SelectionFilter.COMPONENTS)
        want_wires = bool(self._filter & SelectionFilter.WIRES)
        out: list[Selectable] = []
        if want_comps:
            for cid in comp_ids:
                pl = self._session.layout.placements.get(cid)
                if pl is not None:
                    out.append(ComponentSelectable(component_id=cid, placement=pl))
        if want_wires:
            for wid in wire_ids:
                wire = self._wire_by_id(wid)
                if wire is not None:
                    out.append(WireSelectable(wire=wire))
        return out

    # -- mouse-interaction routing -----------------------------------------

    @traced()
    def on_move(self, inp: PointerInput) -> None:
        if self._mouse is not None:
            self._mouse.on_move(inp.scene_pos, inp.mods)

    @traced()
    def on_release(self, inp: PointerInput) -> None:
        mouse = self._mouse
        if mouse is None:
            return
        pt = inp.scene_pos
        mods = inp.mods
        # Synthesise a click (press + release with no intervening drag) — the
        # higher-level event click-driven controllers (picks) act on.  Below the
        # threshold the release IS the click; otherwise it's a real release.
        if self._is_click(pt):
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
        self._clear_on_empty_band_click(mouse)
        self._clear_drag_corner()
        self._report_drag_outcome(mouse)
        self._refresh_highlight()
        if self._view is not None:
            self._view.refresh()

    def _clear_on_empty_band_click(self, mouse: MouseInputController) -> None:
        """A plain click on the empty board clears the selection.

        An empty-space press arms a rubber-band; a sub-threshold release ends it
        as a *click* (``was_click``) that caught nothing, so it never ran the
        band's select.  That click is the deselect gesture — clear here.
        """
        if (
            isinstance(mouse, RubberBandController)
            and getattr(mouse, "was_click", False)
        ):
            self._clear_selection_to_empty()
            self._refresh_highlight()
            self._notify_selection()

    def _resolve_deferred_click(self, mouse: MouseInputController) -> None:
        """Apply the selection change a press-on-selected deferred to release.

        Only fires when the gesture was a click (no drag happened).  A click on
        an item that is part of a *multi*-selection collapses the selection onto
        that single item.  A click on the *sole* selected item is a no-op — it
        stays selected (clicking never toggles an object off; that read as
        "I selected by mistake").  Explicit deselect is Shift+S / clicking the
        empty board.  A real drag consumes the press, so the deferred change is
        dropped entirely.
        """
        sel = self._deferred_click_sel
        self._deferred_click_sel = None
        if sel is None:
            return
        if not isinstance(mouse, DragController) or not mouse.was_click:
            return
        selected = self._selection.selected()
        sole = len(selected) == 1 and selected[0].to_hit() == sel.to_hit()
        if sole:
            return  # already the only selection — keep it, don't toggle off
        # Multi-selection click: collapse onto the clicked item.  Pop to empty
        # first — the FSM's SET op toggles an already-selected hit OFF, so a
        # plain ``set`` would clear instead of collapse.
        self._clear_selection_to_empty()
        self._selection.set(sel)
        self._refresh_highlight()
        self._notify_selection()

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

    @traced()
    def on_double_press(self, scene_pt: Point) -> None:
        """Board double-click → enter the group under the cursor, else forward.

        (BoardView calls this hook for ``MouseButtonDblClick``.)  Double-clicking
        a group steps into its edit context — the affordance the legacy tool gave
        via a 1-second long-press, restored here as a double-click (more
        discoverable, the common CAD convention).  An active pick still gets the
        double-click first.
        """
        if self._mouse is not None and not self._mouse.finished:
            self._mouse.on_double_click(scene_pt, Mods())
            return
        gid = self._group_under(scene_pt)
        if gid is not None:
            self.enter_group(gid)

    def _group_under(self, scene_pt: Point) -> uuid.UUID | None:
        """The non-active group whose member-bbox the point falls in, or None.

        Skips the active group (already inside it) and root.  Used by
        double-click-to-enter; resolves via member bboxes so a click anywhere on
        the group's footprint counts, not only on a component body.
        """
        from protoboard.domain.layout.layout import ROOT_GROUP_ID

        px, py = scene_pt.x, scene_pt.y
        active = self._current_group_id
        tree = self._session.group_tree()
        best: uuid.UUID | None = None
        best_area = 0.0
        for gid, _grp in self._session.layout.groups.items():
            if gid in (ROOT_GROUP_ID, active):
                continue
            bbs = [
                b for b in (
                    self._session.component_bbox(m)
                    for m in tree.all_component_ids(gid)
                ) if b is not None
            ]
            if not bbs:
                continue
            x0 = min(b.x for b in bbs)
            y0 = min(b.y for b in bbs)
            x1 = max(b.x + b.width for b in bbs)
            y1 = max(b.y + b.height for b in bbs)
            if x0 <= px <= x1 and y0 <= py <= y1:
                # Prefer the tightest (smallest) enclosing group when nested.
                area = (x1 - x0) * (y1 - y0)
                if best is None or area < best_area:
                    best, best_area = gid, area
        return best

    def _is_click(self, scene_pt: Point) -> bool:
        """True when the release is within the click threshold of the press."""
        start = self._press_pt
        if start is None:
            return False
        dx = scene_pt.x - start.x
        dy = scene_pt.y - start.y
        return dx * dx + dy * dy < _CLICK_THRESHOLD * _CLICK_THRESHOLD

    # -- arming gestures ----------------------------------------------------

    @traced()
    def _arm_band(self, scene_pt: Point) -> RubberBandController:
        press = scene_pt
        return RubberBandController(
            press_pt=press,
            comp_ids=list(self._session.layout.placements),
            bbox=self._session.component_bbox,
            draw=self._draw_band,
            clear=self._clear_band,
            select=self._band_select,
            extra_catch=self._band_catch_extras,
        )

    def _collect_caught_wires(
        self, rx0: float, ry0: float, rx1: float, ry1: float, ltr: bool
    ) -> list[object]:
        """Wires the band rectangle catches, as Selectables.

        LTR: only fully-enclosed (every wire vertex inside).  RTL: anything
        touching (a wire vertex inside or a segment crossing the rect).
        """
        from protoboard.gui.selection.adapters import WireSelectable

        out: list[object] = []
        for wire in self._session.layout.wires:
            pts = wire.path
            if not pts:
                continue
            if ltr:
                hit = all(rx0 <= p.x <= rx1 and ry0 <= p.y <= ry1 for p in pts)
            else:
                hit = any(rx0 <= p.x <= rx1 and ry0 <= p.y <= ry1 for p in pts) or any(
                    _segment_intersects_rect(pts[i], pts[i + 1], rx0, ry0, rx1, ry1)
                    for i in range(len(pts) - 1)
                )
            if hit:
                out.append(WireSelectable(wire=wire))
        return out

    def _collect_caught_groups(
        self, rx0: float, ry0: float, rx1: float, ry1: float, ltr: bool
    ) -> list[object]:
        """Groups the band rectangle catches, as Selectables.

        LTR: only fully-enclosed (all group members inside).  RTL: anything
        touching (any group member intersecting the rect).
        """
        from protoboard.domain.layout.layout import ROOT_GROUP_ID
        from protoboard.gui.selection.adapters import GroupSelectable

        out: list[object] = []
        tree = self._session.group_tree()
        for gid, grp in self._session.layout.groups.items():
            if gid == ROOT_GROUP_ID:
                continue
            bbs = [
                b for b in (
                    self._session.component_bbox(m)
                    for m in tree.all_component_ids(gid)
                ) if b is not None
            ]
            if not bbs:
                continue
            if ltr:
                hit = all(
                    b.x >= rx0 and b.y >= ry0
                    and b.x + b.width <= rx1 and b.y + b.height <= ry1
                    for b in bbs
                )
            else:
                hit = any(
                    b.x < rx1 and b.x + b.width > rx0
                    and b.y < ry1 and b.y + b.height > ry0
                    for b in bbs
                )
            if hit:
                out.append(GroupSelectable(group=grp))
        return out

    def _band_catch_extras(
        self, rx0: float, ry0: float, rx1: float, ry1: float, ltr: bool
    ) -> list[object]:
        """Wires + groups the band rectangle catches, as Selectables.

        LTR (left-to-right): only fully-enclosed (every wire vertex inside / all
        group members inside).  RTL: anything touching (a wire vertex inside or a
        segment crossing the rect / any group member intersecting).  Mirrors the
        legacy band's wire + group catching, lost in the v2 rewrite where the
        band only collected component bboxes.
        """
        wires = self._collect_caught_wires(rx0, ry0, rx1, ry1, ltr)
        groups = self._collect_caught_groups(rx0, ry0, rx1, ry1, ltr)
        return wires + groups

    @traced()
    def _arm_drag(
        self, sel: Selectable, scene_pt: Point
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
        press = scene_pt
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

        # 2) Conflict label + outline colour — cleared whenever the drop is clear.
        self._show_conflict_corner(decision)

        # 3) Ghost-dim the wires this drop would drop/disconnect, so the user
        #    sees *which* wires are affected before releasing.
        self._update_wire_ghosting(decision)

    def _update_wire_ghosting(self, decision: DragCommitDecision | None) -> None:
        """Dim wires the current drop would remove ("drop") or tear apart
        ("disconnect"); reset any previously-ghosted wire that no longer
        qualifies.  No-op when BoardView lacks the setter (headless test)."""
        view = self._view
        setter = getattr(view, "set_wire_drag_state", None)
        if setter is None:
            return
        states: dict[uuid.UUID, str] = {}
        if decision is not None:
            for wid in (
                *decision.free_drop_wire_ids,
                *decision.stationary_group_drop_wire_ids,
                *decision.auto_remove_wire_ids,
            ):
                states[wid] = "drop"
            for wid in decision.cross_group_disconnect_wire_ids:
                states.setdefault(wid, "disconnect")
        # Reset wires we dimmed last tick that aren't dimmed now.
        for wid in self._ghosted_wire_ids - set(states):
            states[wid] = "normal"
        if states:
            setter(states)
        self._ghosted_wire_ids = {
            wid for wid, st in states.items() if st != "normal"
        }

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
        # Flip the selection-outline colour to match the conflict severity —
        # red HARD / amber SOFT — so the dragged bodies' own highlight signals
        # the drop's consequence (replaces the legacy dashed conflict overlay).
        self._set_drag_outline(color.name(QColor.NameFormat.HexArgb))

    def _set_drag_outline(self, color: str | None) -> None:
        """Drive BoardView's selection-outline override (None → back to cyan).

        *color* is hex ``#AARRGGBB`` — the seam's colour format (qt-ports §2.7).
        """
        view = self._view
        setter = getattr(view, "set_selection_outline_override", None)
        if setter is not None:
            setter(color)

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
        (which clears the scene), and cleared on disarm.  No-op unless a
        :class:`FloatTransform` is armed in its rotate sub-mode.
        """
        from protoboard.gui.selection.float_transform import FloatTransform

        self._clear_pivot_markers()
        armed = self._armed
        if not isinstance(armed, FloatTransform) or self._view is None:
            return
        if armed.mode != "rotate":
            return
        if armed.pivot is not None:
            self._add_pivot_marker(armed.pivot, shared=True)
        else:
            for cid in armed.comp_ids:
                pl = self._session.layout.placements.get(cid)
                if pl is not None:
                    self._add_pivot_marker(pl.position, shared=False)

    def _add_pivot_marker(self, pos: Point, *, shared: bool) -> None:
        """Add one pivot marker (a ⊕ glyph) at *pos*."""
        self.preview.show_pivot(pos, shared=shared)

    def _clear_pivot_markers(self) -> None:
        self.preview.clear_pivots()

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
        # The conflict outline colour travels with the corner label — clear it
        # together so the selection returns to cyan when the conflict resolves
        # or the drag ends (every drag-end path routes through here).
        self._set_drag_outline(None)
        # Restore any ghost-dimmed wires to full opacity (the CLEAR-decision tick
        # and every drag-end path route through here).
        if self._ghosted_wire_ids:
            setter = getattr(self._view, "set_wire_drag_state", None)
            if setter is not None:
                setter({wid: "normal" for wid in self._ghosted_wire_ids})
            self._ghosted_wire_ids = set()

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
        """True when *comp_id* is a direct member of a group we view from outside.

        A group is a "hand-crafted IC": from the outside it is a sealed package —
        selection (for inspection / properties) is allowed, but layout-mutating
        ops (drag, delete, rotate, flip) are not.  Stepping inside the group
        (``_current_group_id`` advances to it) unlocks its direct members.

        Locked iff the component's *direct* group is NOT the active group.

        NB: do **not** use ``groups_containing_component`` here — it folds in the
        parent chain up to ROOT, so for any grouped component ROOT is always in
        the set, and with the default ROOT context the lock would never fire
        (the v2-rewrite regression this guard was restored to catch).  The
        direct-membership check, skipping ROOT, is the correct gate.
        """
        from protoboard.domain.layout.layout import ROOT_GROUP_ID

        active_gid = self._current_group_id
        for gid, g in self._session.layout.groups.items():
            if gid == ROOT_GROUP_ID:
                continue
            if comp_id not in g.component_ids:
                continue
            # Direct member of group `g`: locked unless we're inside `g`.
            return gid != active_gid
        return False

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
        return self._view.snap(raw, exclude_ids=exclude)

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
        self.preview.show_band(a, b, ltr)

    def _band_select(self, hit: object, mods: object) -> None:
        """Commit a rubber-band result under the decoded modifier op.

        Plain band → SET (replace).  Ctrl+band → ADD (union into the current
        selection).  Ctrl+Shift+band → REMOVE (subtract).  Catches components
        (by bbox) plus wires and groups (the controller's extra-catch) — the
        legacy band's full reach, lost when v2's band only set components.

        ADD/REMOVE go through ``set_many`` (relation-agnostic): the FSM's
        ``add`` only extends within the same relation, but a band is an explicit
        "these exact items" set that must union/subtract regardless of net.
        """
        from typing import cast

        from protoboard.gui.selection.adapters import ComponentSelectable
        from protoboard.gui.selection.mouse_input import Mods
        from protoboard.gui.selection.rubber_band_controller import BandHit

        if not isinstance(hit, BandHit):
            return
        picked: list[Selectable] = []
        for cid in hit.comp_ids:
            pl = self._session.layout.placements.get(cid)
            if pl is not None:
                picked.append(ComponentSelectable(component_id=cid, placement=pl))
        picked.extend(cast("list[Selectable]", hit.extras))  # wires + groups

        ctrl = bool(isinstance(mods, Mods) and mods.ctrl)
        shift = bool(isinstance(mods, Mods) and mods.shift)
        if ctrl and shift:  # REMOVE — current minus the caught set
            drop = {p.to_hit() for p in picked}
            final = [
                s for s in self._selection.selected() if s.to_hit() not in drop
            ]
        elif ctrl:  # ADD — union of current + caught (dedup by hit)
            final = list(self._selection.selected())
            seen = {s.to_hit() for s in final}
            for sel in picked:
                if sel.to_hit() not in seen:
                    final.append(sel)
                    seen.add(sel.to_hit())
        else:  # SET — replace
            final = picked
        # Rebuild from empty: set_many starts with set(first), and the FSM SET op
        # toggles an already-selected hit OFF — so re-setting a still-selected
        # first item would drop it.  Clearing first makes set_many land exactly
        # *final*.
        self._clear_selection_to_empty()
        if final:
            self._selection.set_many(final)
        self._refresh_highlight()
        self._notify_selection()

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

    @traced()
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
        self.preview.clear_band()

    # -- re-drag (bendable lead reposition, a request_pick client) ----------

    @traced()
    def begin_redrag(self, comp_id: uuid.UUID | None = None) -> None:
        """Arm re-drag: choose a pin, then the pad it should reach.

        Entry is **click-a-pin** (no silent auto-pick).  The grabbed pin is the
        one the component was selected by clicking — *any* pin, rigid or
        bendable: grabbing a bendable pin bends that lead, grabbing a rigid pin
        translates the body while the other leads absorb the shift (feasibility
        is decided per target by :func:`redrag_feasibility`).  The component must
        have at least one movable lead for a re-drag to be possible at all.

        On a body click (no specific pin) the grab is ambiguous: the sole movable
        lead is used as a convenience, otherwise the user picks a pin first (the
        pick-a-pin sub-step).  Re-clicking another pin during the pad pick
        switches the grab.
        """
        cid = comp_id if comp_id is not None else next(
            iter(self._selected_comp_ids()), None
        )
        if cid is None:
            self._status("Re-drag: select a component with a bendable lead first")
            return
        movable = self._movable_pins(cid)
        if not movable:
            self._status("Re-drag: this component has no movable lead or mount")
            return

        clicked = self._selected_clicked_pin(cid)
        if clicked is not None:
            # Grab the clicked pin directly — rigid pins translate the body.
            self._arm_redrag_pad_pick(cid, clicked)
        elif len(movable) == 1:
            # Body click, one free lead → no ambiguity; grab it.
            self._arm_redrag_pad_pick(cid, next(iter(movable)))
        else:
            self._begin_redrag_pick_pin(cid)

    def _begin_redrag_pick_pin(self, comp_id: uuid.UUID) -> None:
        """Pick-a-pin sub-step: arm a component pick to choose the grabbed pin.

        Reached on a body click when more than one lead can bend, so the grab is
        ambiguous.  The pick resolves a board entity; we accept it only when it
        is a pin of *comp_id* (rigid or bendable — a rigid grab translates the
        body), then arm the pad pick for that pin.
        """
        from protoboard.gui.selection import Stop

        self._redrag_comp_id = comp_id
        self._status("Click a pin to re-drag  ·  [Esc] Cancel", 0)

        def _on_pin(picked: list[Selectable]) -> None:
            pin = self._picked_pin(comp_id, picked)
            if pin is None:
                self._redrag_clear()
                self._status("Re-drag: that is not a pin of this component")
                return
            self._arm_redrag_pad_pick(comp_id, pin)

        self._arm_pick(
            "component",
            Stop.count(1),
            on_done=_on_pin,
            on_cancel=self._redrag_clear,
        )

    def _arm_redrag_pad_pick(self, comp_id: uuid.UUID, pin: int) -> None:
        """Arm the pad pick for *pin*; a re-click on another pin switches grab.

        The resolver intercepts clicks landing on a pin of the same component and
        switches the grab (redrawing the overlay) instead of treating the click
        as a pad target; any other click resolves as a pad.
        """
        from protoboard.gui.selection import Stop

        self._redrag_comp_id = comp_id
        self._redrag_pin = pin
        self._draw_redrag_overlay(comp_id)
        self._status("Click a pad for the lead  ·  [Esc] Cancel", 0)
        self._arm_pick(
            "pad",
            Stop.count(1),
            on_done=self._redrag_commit,
            on_cancel=self._redrag_clear,
            on_progress=self._redrag_progress,
            on_preview=self._redrag_preview,
            resolve=self._resolve_redrag_pad,
        )

    def _resolve_redrag_pad(self, pt: Point, _mods: Mods) -> Selectable | None:
        """Redrag pad resolver: re-click on another pin switches grab.

        Returns ``None`` (swallowing the click) when it lands on a pin of the
        redrag component — after switching ``_redrag_pin`` and redrawing — so the
        single-pad pick is not completed by a pin re-click.  Otherwise it
        resolves to the snapped pad like a normal ``pad`` pick.
        """
        cid = self._redrag_comp_id
        if cid is not None:
            hit = self._hit_tester().hit(pt.x, pt.y, self._filters())
            new_pin = self._selectable_pin(cid, hit)
            if new_pin is not None and new_pin != self._redrag_pin:
                self._redrag_pin = new_pin
                self._draw_redrag_overlay(cid)
                self._status("Click a pad for the lead  ·  [Esc] Cancel", 0)
                return None
        return self._pick_at("pad", pt)

    def _movable_pins(self, comp_id: uuid.UUID) -> set[int]:
        """The pins of *comp_id* whose lead can bend (free reach), package-aware.

        Specs come from :func:`effective_bend_specs`, so package-driven mounts
        are seen even when ``bendable_pins`` is empty (the loader clears it for
        rigid packages).  Replaces the legacy single-pin ``_redraggable_pin``.
        """
        from protoboard.domain.components.package import effective_bend_specs

        inst = self._session.instances.get(comp_id)
        pl = self._session.layout.placements.get(comp_id)
        if inst is None or pl is None:
            return set()
        defn = self._session.components.get(inst.definition_ref)
        if defn is None:
            return set()
        specs = effective_bend_specs(
            defn, inst, getattr(self._session, "package_families", None)
        )
        return {pin for pin, spec in specs.items() if spec.is_free()}

    def _selected_clicked_pin(self, comp_id: uuid.UUID) -> int | None:
        """The pin *comp_id* was selected by clicking, or ``None``.

        Reads the ``pin_number`` the hit-test recorded on the current selection
        (set only on a true pad click, ``None`` for a body click).
        """
        from protoboard.gui.selection.adapters import ComponentSelectable

        if self._selection is None:
            return None
        for sel in self._selection.selected():
            if (
                isinstance(sel, ComponentSelectable)
                and sel.component_id == comp_id
            ):
                return sel.pin_number
        return None

    def _selectable_pin(
        self, comp_id: uuid.UUID, sel: Selectable | None
    ) -> int | None:
        """The pin number if *sel* is a pin of *comp_id* (any pin), else ``None``.

        Any pin is a valid re-drag grab: a bendable pin bends its lead, a rigid
        pin translates the body (feasibility is decided later per target).
        """
        from protoboard.gui.selection.adapters import ComponentSelectable

        if (
            isinstance(sel, ComponentSelectable)
            and sel.component_id == comp_id
            and sel.pin_number is not None
        ):
            return sel.pin_number
        return None

    def _picked_pin(
        self, comp_id: uuid.UUID, picked: list[Selectable]
    ) -> int | None:
        """Extract a pin of *comp_id* from a completed component pick."""
        if not picked:
            return None
        return self._selectable_pin(comp_id, picked[0])

    @traced()
    def _arm_pick(
        self,
        what: str,
        stop: object,
        *,
        on_done: object,
        on_cancel: object = None,
        on_progress: object = None,
        on_preview: object = None,
        resolve: object = None,
    ) -> None:
        """Start a pick and park it as the active mouse interaction.

        Builds a :class:`PickSession` via the service, wraps it in a
        :class:`PointPickController` (a MouseInputController), and routes board
        clicks / intents through ``self._mouse`` like any other interaction.
        The point→Selectable resolver defaults to the tool's :meth:`_pick_at`
        (so a ``pad`` pick yields the snapped coordinate and an entity pick
        hit-tests); pass *resolve* (``(Point, Mods) -> Selectable | None``) to
        override it — re-drag uses this so a click on another movable pin
        switches the grab instead of completing the pad pick.
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

        custom_resolve = cast(
            "Callable[[Point, Mods], Selectable | None] | None", resolve
        )

        def _resolve(pt: Point, _mods: Mods) -> Selectable | None:
            if custom_resolve is not None:
                return custom_resolve(pt, _mods)
            return self._pick_at(kind, pt)

        self._mouse = PointPickController(
            session,
            _resolve,
            on_preview=cast(
                "Callable[[Point, Mods], None] | None", on_preview
            ),
        )

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
        """Build the MoveComponentCommand re-dragging *pin* to *target*.

        Delegates the geometry + legality to the pure-domain
        :func:`redrag_feasibility` (the same predicate the overlay uses, so the
        lit pads and the committable pads cannot diverge).  A bendable grab bends
        that one lead; a rigid grab translates the body, the other leads
        absorbing the shift within their own discs.  Returns ``None`` when the
        move is illegal.
        """
        from protoboard.domain.commands.component_cmds import MoveComponentCommand

        pl = self._session.layout.placements.get(comp_id)
        inst = self._session.instances.get(comp_id)
        if pl is None or inst is None:
            return None
        result = self._redrag_feasibility(comp_id, pin, target)
        if result is None or not result.ok or result.new_placement is None:
            return None
        return MoveComponentCommand(
            component_id=comp_id,
            new_placement=result.new_placement,
            old_placement=pl.copy(),
        )

    def _redrag_feasibility(
        self, comp_id: uuid.UUID, pin: int, target: Point
    ) -> "RedragResult | None":
        """Evaluate a candidate re-drag of *pin* to *target* (snapped).

        Resolves the component's footprint + bend specs and calls the pure-domain
        predicate.  Shared by the commit and the overlay so both agree on
        legality.  ``None`` when the component cannot be resolved.
        """
        from protoboard.domain.components.package import (
            effective_bend_specs,
            redrag_feasibility,
        )

        pl = self._session.layout.placements.get(comp_id)
        inst = self._session.instances.get(comp_id)
        if pl is None or inst is None:
            return None
        defn = self._session.components.get(inst.definition_ref)
        if defn is None:
            return None
        families = getattr(self._session, "package_families", None)
        specs = effective_bend_specs(defn, inst, families)
        snapped = self._snap(target, frozenset())
        from protoboard.domain.components.package import resolved_footprint
        return redrag_feasibility(
            footprint=resolved_footprint(defn, inst, families),
            placement=pl,
            specs=specs,
            grabbed_pin=pin,
            target=snapped,
            free_pads=self._board_pad_keys(),
        )

    def _board_pad_keys(self) -> frozenset[tuple[float, float]]:
        """Rounded (x, y) keys of every board pad — landing set for a moved pin."""
        if self._view is None:
            return frozenset()
        from protoboard.geometry import round_coord

        return frozenset(
            (round_coord(p.x), round_coord(p.y))
            for p in self._view.board_pad_positions()
        )

    def _draw_reach_disc_outline(
        self,
        defn: ComponentDefinition,
        inst: ComponentInstance,
        pl: ComponentPlacement,
        pin: int,
    ) -> None:
        """Outline the grabbed pin's reach disc, centred on its default hole.

        Only drawn for a bendable (free) grab with a positive reach; a no-op
        otherwise.
        """
        from protoboard.domain.components.package import (
            effective_bend_specs,
            resolved_footprint,
        )
        from protoboard.domain.layout.placement import pin_world_position

        families = getattr(self._session, "package_families", None)
        spec = effective_bend_specs(defn, inst, families).get(pin)
        if spec is None or not spec.is_free() or spec.max_reach <= 0.0:
            return
        temp = pl.copy()
        temp.pin_offsets = {
            k: v for k, v in pl.pin_offsets.items() if k != pin
        }
        centre = pin_world_position(
            resolved_footprint(defn, inst, families), temp, pin
        )
        self.preview.show_reach_disc(centre, spec.max_reach)

    def _draw_redrag_overlay(self, comp_id: uuid.UUID) -> None:
        """Light every legal landing pad + outline the grabbed pin's reach disc.

        A pad is lit iff :func:`redrag_feasibility` accepts it as the target —
        the same predicate the commit uses, so the lit set IS the committable
        set.  For a bendable grab the grabbed pin's reach disc is outlined,
        centred on that pin's default hole.
        """
        self._clear_redrag_overlay()
        pin = self._redrag_pin
        inst = self._session.instances.get(comp_id)
        pl = self._session.layout.placements.get(comp_id)
        if pin is None or inst is None or pl is None:
            return
        defn = self._session.components.get(inst.definition_ref)
        if defn is None:
            return

        # Legal landing pads — feasibility per pad (overlay == commit).
        for pt in self._view.board_pad_positions():
            result = self._redrag_feasibility(comp_id, pin, pt)
            if result is not None and result.ok:
                self.preview.show_redrag_pad(pt)

        # Reach-disc outline for a bendable grab, centred on the pin's default.
        self._draw_reach_disc_outline(defn, inst, pl, pin)

    def _redrag_preview(self, scene_pt: Point, _mods: Mods) -> None:
        """Live validity tint under the cursor during the re-drag pad pick.

        Green ring = the snapped pad is a legal landing; red ring with a central
        cross = illegal.  The cross is a non-colour cue so the legal/illegal
        signal does not rely on colour alone (accessibility).
        """
        self._clear_redrag_live()
        cid, pin = self._redrag_comp_id, self._redrag_pin
        if cid is None or pin is None:
            return
        # A re-click on another pin is a grab-switch, not a target — skip tint.
        hit = self._hit_tester().hit(scene_pt.x, scene_pt.y, self._filters())
        if self._selectable_pin(cid, hit) is not None:
            return
        snapped = self._snap(scene_pt, frozenset())
        result = self._redrag_feasibility(cid, pin, snapped)
        ok = result is not None and result.ok
        self.preview.show_redrag_live(snapped, ok)

    def _redrag_clear(self) -> None:
        self._clear_redrag_overlay()
        self._redrag_comp_id = None
        self._redrag_pin = None

    def _clear_redrag_overlay(self) -> None:
        self.preview.clear_redrag()

    def _clear_redrag_live(self) -> None:
        self.preview.clear_redrag_live()

    # -- wire-edit (fragment = selection, operations = actions) -------------

    @traced()
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

    @traced()
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
        self._ensure_wire_ready_for_edit(wire_id)
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

    def _ensure_wire_ready_for_edit(self, wire_id: uuid.UUID) -> None:
        """Materialise every board pad on a wire's path as a vertex.

        Shortcut / post-simplify wires carry only their endpoints even though
        the physical conductor rests on intermediate pads; wire-edit hit-test
        only matches existing vertices, so those pads would be unselectable.
        Re-insert them with one ReplaceWireFragmentsCommand — idempotent (no
        command when the path is already enriched).  Pads from BOTH sides are
        considered (a plated-through pad on the far side still anchors a vertex).
        Restored from the legacy tool; dropped in the v2 hit-test rewrite.
        """
        from protoboard.domain.layout.wire_utils import (
            board_pads_for_side,
            insert_pad_nodes,
        )
        from protoboard.gui.selection.wire_edit import replace_wire_path_cmd

        wire = self._wire_by_id(wire_id)
        if wire is None or len(wire.path) < 2:
            return
        pads = (
            board_pads_for_side(self._session.board, "top")
            | board_pads_for_side(self._session.board, "bottom")
        )
        enriched = insert_pad_nodes(list(wire.path), pads)
        if enriched == list(wire.path):
            return  # already has every pad vertex
        cmd = replace_wire_path_cmd(
            self._session.layout, wire, enriched, list(wire.path),
        )
        self._execute(cmd)

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

    def _wire_edit_on_key(self, inp: KeyInput) -> bool:
        """Op-table for the selected fragment — one entry per operation.

        Dispatches by *action id* resolved through the KeyMap (not by hard-coded
        keys), so rebinding reroute/autoroute/delete is honoured.  Cancel stays
        on raw Esc / Backspace too, since those are not global shortcuts.
        Returns True when the key was consumed.
        """
        key = inp.key
        if self._key_matches(inp, _ACT_REROUTE):
            self._begin_reroute()
        elif self._key_matches(inp, _ACT_AUTOROUTE):
            self._autoroute_fragment()
        elif self._key_matches(inp, _ACT_FLIP_FRAGMENT):
            self._flip_wire_fragment()
        elif self._key_matches(inp, _ACT_DELETE) or key == Qt.Key.Key_Backspace:
            self._delete_fragment()
        elif self._key_matches(inp, _ACT_CANCEL) or key == Qt.Key.Key_Escape:
            self._wire_edit_clear()
        else:
            return False
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
        self._clear_reroute_live()
        anchor = self._reroute_anchor()
        if anchor is None:
            return
        snapped = self._snap(scene_pt, frozenset())
        self.preview.show_reroute_live(anchor, snapped)

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
        return self._session.cached_wire_net_map()

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
            self.preview.show_wire_edit_wire(
                list(wire.path), marked.get(wid, set())
            )

    def _draw_reroute_preview(self, waypoints: list[Point]) -> None:
        """Dashed orange polyline from the fragment start through the waypoints."""
        self._clear_reroute_overlay()
        anchor = None
        state = self._wire_edit
        if state is not None:
            wire = self._wire_by_id(state.start_wire_id)
            if wire is not None:
                anchor = wire.path[state.start_idx]
        if anchor is None:
            return
        self.preview.show_reroute_path([anchor, *waypoints])

    def _clear_wire_edit_overlay(self) -> None:
        self.preview.clear_wire_edit()

    def _clear_reroute_overlay(self) -> None:
        self.preview.clear_reroute()

    def _clear_reroute_live(self) -> None:
        self.preview.clear_reroute_live()

    # -- keys ---------------------------------------------------------------

    @traced()
    def on_key(self, inp: KeyInput) -> bool:
        key = inp.key
        ctrl = inp.mods.ctrl
        shift = inp.mods.shift
        alt = inp.mods.alt

        # An armed sub-tool (float-rotate / keyboard-move) owns its keys first:
        # one uniform dispatch, no branching on which mode it is.  Enter / Esc
        # travel as intents (accept / cancel calls — the sub-tool never sees a
        # keycode); step / mode keys go through on_key.  Drop the sub-tool once
        # it reports finished.
        if self._armed is not None:
            intent = self._key_to_intent(key)
            if intent is Intent.CONFIRM:
                self._armed.accept()
                consumed = True
            elif intent is Intent.CANCEL:
                self._armed.cancel()
                consumed = True
            else:
                consumed = self._armed.on_key(inp)
            if consumed:
                if self._armed.finished:
                    self._disarm()
                return True

        # Pivot mode: Esc clears it first.
        if (self._pivot_capture_pending or self._rotate_pivot is not None):
            if key == Qt.Key.Key_Escape:
                self._clear_pivot()
                self._status("Pivot cleared")
                return True

        # Translate the key into a semantic intent and offer it to the active
        # mouse interaction (a floating drag, or a pick via PointPickController:
        # CONFIRM ends it, CANCEL aborts, UNDO drops the last pick).  One path —
        # no controller-type branching; the controller decides.  A wire-vertex
        # or reroute-waypoint pick is live here, so its Enter/Esc/Backspace
        # route to the pick before the op-keys get a turn.
        intent = self._key_to_intent(key)
        if intent is not None and self._dispatch_intent(intent):
            return True

        # Fragment selected and no pick in flight → the op-keys act on it
        # ([W] reroute · [A] autoroute · [Del] delete · [Esc] cancel).  This is
        # the "operations are actions on the selected fragment" branch.
        if self._wire_edit is not None and self._mouse is None:
            if self._wire_edit_on_key(inp):
                return True

        # Relation-axis cycling: backtick forward, Shift+backtick back.  Match
        # via the KeyMap (rebindable), with a raw Key_QuoteLeft fallback so it
        # works headless where the KeyMap may be absent.
        if self._key_matches(inp, _ACT_RELATION_CYCLE_BACK) or (
            key == Qt.Key.Key_QuoteLeft and shift
        ):
            self._cycle_relation(forward=False)
            return True
        if self._key_matches(inp, _ACT_RELATION_CYCLE) or (
            key == Qt.Key.Key_QuoteLeft and not shift
        ):
            self._cycle_relation(forward=True)
            return True

        if (
            self._key_matches(inp, _ACT_DELETE)
            or (key == Qt.Key.Key_Backspace and not ctrl)
        ):
            return self._delete_selection(force=shift)

        if self._key_matches(inp, _ACT_EDIT):
            self._begin_edit_selected()  # edit the selected object
            return True

        if key == Qt.Key.Key_R and ctrl:
            self.begin_redrag()  # Ctrl+R — re-drag the selected lead (legacy parity)
            return True

        if self._key_matches(inp, _ACT_MOVE) and not self._selection.is_empty():
            self.arm_move()  # [G] — keyboard float-move (arrows / WSAD)
            return True

        # Alt+R flips (immediate 2-pin flip, kept as a one-shot until flip joins
        # the armed transform mode); the bare rotate action arms float-rotate.
        if self._key_matches(inp, _ACT_FLIP) or (key == Qt.Key.Key_R and alt):
            self._flip_selected()
            return True

        if self._key_matches(inp, _ACT_ROTATE) and not ctrl:
            self.arm_rotate()
            return True

        if key == Qt.Key.Key_Escape:
            if len(self._group_stack) > 1:
                self.exit_group()
            else:
                self._selection.clear()
                self._context_net_id = None  # deselect drops the net context
                self._clear_free_pad_ring()
                self._refresh_highlight()
            return True
        return False

    def on_key_release(self, inp: KeyInput) -> None:
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

    def key_claims(self) -> tuple[tuple[str, Callable[[], bool]], ...]:
        """Contextual key claims (see :meth:`_Tool.wants_key` for the veto).

        - An armed transform (float-rotate / keyboard-move) claims its step +
          action keys — so a global QAction (Route on the step-up/WASD key,
          Select on 'S', …) never shadows the live mode.
        - A selected wire-edit fragment claims its operation keys.
        - Delete acts on the current selection — claimed so the global delete
          action doesn't shadow the tool's handler.

        Pick-confirm / cancel / undo (Enter / Esc / Backspace) are raw,
        non-action keys that aren't global shortcuts, so they reach the tool
        anyway and need no claim here.
        """
        armed = self._is_armed
        wire_edit = self._has_wire_edit
        return (
            *((a, armed) for a in _ARMED_ACTION_IDS),
            (_ACT_REROUTE, wire_edit),
            (_ACT_AUTOROUTE, wire_edit),
            (_ACT_FLIP_FRAGMENT, wire_edit),
            (_ACT_DELETE, wire_edit),
            (_ACT_DELETE, self._has_selection),
        )

    def _is_armed(self) -> bool:
        return self._armed is not None

    def _has_wire_edit(self) -> bool:
        return self._wire_edit is not None

    def _has_selection(self) -> bool:
        return not self._selection.is_empty()

    def mouse_actions(self) -> dict[str, str | None]:
        """Mouse chords this tool layers over the view gestures.

        ``wheel_rotate`` shadows the zoom while an armed transform is live
        (both directions — the accumulator inside FloatTransform decides the
        step sign from raw deltas); ``context_menu`` shadows the generic
        canvas menu with the unified item menu when the cursor hits something.
        """
        return {
            _ACT_WHEEL_ROTATE: "WheelUp|WheelDown",
            _ACT_CONTEXT_MENU: "RMB",
        }

    def mouse_dispatch(self) -> tuple[
        tuple[str, Callable[[PointerInput | WheelInput], bool]],
        ...,
    ]:
        return (
            (_ACT_WHEEL_ROTATE, self._mouse_wheel_rotate),
            (_ACT_CONTEXT_MENU, self._mouse_context_menu),
        )

    def _mouse_wheel_rotate(self, inp: PointerInput | WheelInput) -> bool:
        if not isinstance(inp, WheelInput) or not self.wants_wheel(inp):
            return False  # not armed → the chord falls through to zoom
        self.on_wheel(inp)
        return True

    def _mouse_context_menu(self, inp: PointerInput | WheelInput) -> bool:
        if not isinstance(inp, PointerInput):
            return False
        # False on empty canvas → falls through to the generic canvas menu.
        return self.on_right_press(inp.scene_pos, inp.view_pos)

    def wants_wheel(self, inp: WheelInput) -> bool:
        """Claim the wheel only when the armed sub-tool consumes it (rotate)."""
        return self._armed is not None and self._armed.wants_wheel(inp)

    def on_wheel(self, inp: WheelInput) -> None:
        """Route a wheel tick to the armed sub-tool (it steps / declines)."""
        if self._armed is None:
            return
        if self._armed.on_wheel(inp) and self._armed.finished:
            self._disarm()

    # -- rotate / flip ------------------------------------------------------

    @traced()
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

    @traced()
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

    @traced()
    def arm_rotate(self) -> None:
        """Arm float-rotate on the selection — the ``[R]`` entry point.

        The selection stays live: wheel / ``[↑]`` / ``[↓]`` / ``[R]`` step ±90°
        (previewed off the undo stack), clicking a pad sets a shared pivot,
        ``[Enter]`` accepts (commits ONE rotation **only if** the result is
        committable, else parks armed in the conflict) and ``[Esc]`` cancels.
        A legacy ``begin_rotate_around_point`` pivot capture seeds the pivot.
        """
        from typing import cast

        from protoboard.gui.selection.drag_controller import DragContext
        from protoboard.gui.selection.float_transform import FloatTransform

        if isinstance(self._armed, FloatTransform):
            self._armed.switch_mode("rotate")  # already floating — just re-aim
            self._status(self._armed.status_hint(), 0)
            return
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
        self._armed = FloatTransform(
            cast("DragContext", self._session),
            comp_ids=comp_ids,
            group_id=group_id,
            snap=self._snap_free,
            execute=self._execute_command,
            matches=self._key_matches,
            mode="rotate",
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
        return self._view.snap(raw)

    @traced()
    def arm_move(self) -> None:
        """Arm keyboard float-move on the selection — the ``[G]`` entry point.

        Arms the shared :class:`FloatTransform` in its *move* sub-mode (arrows /
        WASD nudge one cell, ``[Enter]`` drops when committable, ``[Esc]``
        cancels).  When a transform is already armed — e.g. mid-rotate — this
        only switches the sub-mode, so the float pose carries over and the user
        can slide a rotated part clear of a conflict without committing first.
        """
        from typing import cast

        from protoboard.gui.selection.drag_controller import DragContext
        from protoboard.gui.selection.float_transform import FloatTransform

        if isinstance(self._armed, FloatTransform):
            self._armed.switch_mode("move")  # already floating — just re-aim
            self._status(self._armed.status_hint(), 0)
            return
        if self._armed is not None:
            return
        if self._selection.is_empty():
            self._status("Move: select component(s) or a group first")
            return
        comp_ids, group_id = self._rotatable_affected()
        if not comp_ids:
            self._status("Move: nothing movable in the selection")
            return
        self._cancel_gesture()
        self._armed = FloatTransform(
            cast("DragContext", self._session),
            comp_ids=comp_ids,
            group_id=group_id,
            snap=self._snap_free,
            execute=self._execute_command,
            matches=self._key_matches,
            mode="move",
            preview=self._rotate_preview,
            status=lambda msg: self._status(msg, 0),
        )
        self._status(self._armed.status_hint(), 0)

    def _selected_comp_ids(self) -> frozenset[uuid.UUID]:
        import uuid as _uuid

        out: set[_uuid.UUID] = set()
        for sel in self._selection.selected():
            hit = sel.to_hit()
            if hit.kind == "component" and isinstance(hit.id, _uuid.UUID):
                out.add(hit.id)
        return frozenset(out)

    @traced()
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
        self._notify_selection()  # refresh Properties panel rotation, etc.

    @traced()
    def _flip_selected(self) -> None:
        from protoboard.domain.commands.base import MacroCommand
        from protoboard.domain.commands.component_cmds import MoveComponentCommand
        from protoboard.domain.components.package import resolved_footprint
        from protoboard.domain.layout.placement import flip_component

        families = getattr(self._session, "package_families", None)
        cmds: list[object] = []
        for cid in self._selected_comp_ids():
            pl = self._session.layout.placements.get(cid)
            inst = self._session.instances.get(cid)
            if pl is None or inst is None:
                continue
            defn = self._session.components.get(inst.definition_ref)
            if defn is None:
                continue
            fp = resolved_footprint(defn, inst, families)
            if fp.pin_count() != 2:
                continue
            cmds.append(
                MoveComponentCommand(
                    component_id=cid,
                    # flip pivots around the pin span → variant-sensitive.
                    new_placement=flip_component(fp, pl),
                    old_placement=pl.copy(),
                )
            )
        if not cmds:
            return
        self._execute(MacroCommand(cmds, description=f"Flip {len(cmds)} components"))  # type: ignore[arg-type]
        self._notify_selection()  # refresh Properties panel after flip

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
        self._notify_selection()  # refresh Properties panel after multi-rotate

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
        from protoboard.domain.components.package import resolved_footprint
        from protoboard.domain.layout.placement import flip_component

        # A selected group flips as a rigid body — the flip analogue of the
        # RotateGroupCommand dispatch in _rotate_multi.  Default axis: 'v'
        # (mirror left↔right) about the group's pin centroid (pivot=None);
        # the armed FlipMode with a [v]/[h] pick is the designed follow-up
        # (see docs/todo.md, group scene-graph item 3).
        group_id = self._selected_group_id
        if group_id is not None:
            from protoboard.domain.commands.group_cmds import FlipGroupCommand
            from protoboard.domain.layout.group import all_component_ids

            affected = all_component_ids(self._session.layout.groups, group_id)
            if affected:
                self._apply_rotate(
                    FlipGroupCommand(group_id=group_id, axis="v"),
                    set(affected),
                )
            return

        families = getattr(self._session, "package_families", None)
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
            if defn is None:
                continue
            fp = resolved_footprint(defn, inst, families)
            if fp.pin_count() != 2:
                continue
            cmds.append(
                MoveComponentCommand(
                    component_id=cid,
                    new_placement=flip_component(fp, pl),
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

    def _hit_test_wire(self, scene_pt: Point) -> uuid.UUID | None:
        """Nearest wire id within ``WIRE_HIT_DIST`` (SimplifyTool's hook)."""
        return self._hit_tester()._hit_wire(scene_pt.x, scene_pt.y)

    # -- right-click context menu ------------------------------------------
    #
    # The v2 swap left BoardView's generic canvas menu (tool actions + center /
    # escape) as the only RMB menu — every item-aware operation the legacy tool
    # exposed (assign/disconnect pin, lock, export, select-by-net, wire
    # bare/insulated/side/flip/simplify/optimize/unweave/merge) was lost.  This
    # rebuilds the unified menu on v2's services and returns True so BoardView
    # does not fall through to the generic menu; an *empty* canvas returns False
    # so the generic menu (with the same tool-action section) still appears.
    # The tool-action + view section is added here too, so the contextual menu
    # *augments* the generic one rather than replacing it.

    def on_right_press(self, scene_pt: Point, view_pos: Point) -> bool:
        """Publish the item-aware interaction context under the cursor.

        No menu is built here (qt-ports §2.4, variant 1): the context is a
        pure data snapshot; the consumer (MainWindow) renders it and executes
        the chosen option through ``perform_context_action``.  Returns True
        when a context was published (item present), False on empty canvas so
        the chord falls through to the generic canvas context.
        """
        ctx = self.build_context(scene_pt, view_pos)
        if ctx is None:
            return False
        self._view.notify_context(ctx)
        return True

    @traced()
    def secondary_action(self, point: Point, mods: Mods) -> bool:
        """RMB verb (docs MVC step 1) — publish the item context at *point*.

        The verb carries only ``(point, mods)``; the View reconstructs the
        menu anchor from the point (scene == board coords).  Delegates to the
        existing :meth:`on_right_press`.
        """
        return self.on_right_press(point, point)

    @property
    def state(self) -> str:
        """Current interaction state (docs MVC step 1) — a Controller fact.

        ``select.idle`` unless an armed float-transform is live, in which case
        the sub-mode is exposed (``move.float.rotate`` / ``move.float.move``).
        Asserted directly by headless tests, including the negative case
        (arming with no selection leaves the state ``select.idle``).
        """
        from protoboard.gui.selection.float_transform import FloatTransform
        if isinstance(self._armed, FloatTransform):
            return f"move.float.{self._armed.mode}"
        return "select.idle"

    def build_context(
        self, scene_pt: Point, view_pos: Point
    ) -> InteractionContext | None:
        """The unified item context as pure data, or None on empty canvas.

        Composes the generic tool-action options (shared with the canvas
        context) with the item-aware sections; each parameterised option
        carries its payload in ``ActionOption.data`` and executes through
        :meth:`perform_context_action`.  Testable with no menu and no fake.
        """
        comp_id = self._menu_comp_at(scene_pt)
        wire_id = self._menu_wire_at(scene_pt)
        if comp_id is None and wire_id is None:
            return None
        options: list[ActionOption] = []
        options += self._view.tools.get_context_at(scene_pt, view_pos).actions
        options += self._ctx_component_options(comp_id, scene_pt)
        options += self._ctx_select_options(comp_id, wire_id)
        options += self._ctx_highlight_options()
        options += self._ctx_wire_options(wire_id)
        hit: tuple[str, uuid.UUID]
        if comp_id is not None:
            hit = ("component", comp_id)
        else:
            assert wire_id is not None
            hit = ("wire", wire_id)
        return InteractionContext(
            scene_pos=scene_pt,
            view_pos=view_pos,
            hit=hit,
            selection=tuple(self._selected_ids),
            actions=tuple(options),
            kind="selection" if comp_id is not None else "wire",
        )

    def _menu_comp_at(self, scene_pt: Point) -> uuid.UUID | None:
        """Component under the cursor (selected one wins, else hit-test)."""
        sel_comp = self._selected_id
        if sel_comp is not None:
            return sel_comp
        hit = self._hit_tester().hit(scene_pt.x, scene_pt.y, self._filters())
        if hit is not None and hit.to_hit().kind == "component":
            cid = hit.to_hit().id
            return cid if isinstance(cid, uuid.UUID) else None
        return None

    def _menu_wire_at(self, scene_pt: Point) -> uuid.UUID | None:
        """Wire under the cursor (selected primary wins, else hit-test)."""
        sel_wires = self._selected_wire_ids
        if sel_wires:
            return next(iter(sel_wires))
        return self._hit_test_wire(scene_pt)

    def _hint_free_pin_under(self, sel: Selectable | None, scene_pt: Point) -> None:
        """Feedback when the press lands on a *free* (netless) pin-pad.

        A free pin carries no net context, so it gets neither selection-net
        dimming nor a neon disc — the click would look like a no-op.  Two
        signals restore it: a status-bar hint and a light ring on the pad
        (drawn by BoardView).  Both clear on any non-free-pin press.  Only a
        component hit whose *clicked pin* is unassigned qualifies.
        """
        from protoboard.domain.schematic.net import PinRef

        free_pos: Point | None = None
        hit = sel.to_hit() if sel is not None else None
        if hit is not None and hit.kind == "component" and isinstance(hit.id, uuid.UUID):
            pin = hit.pin_number  # the on-pad pin (None for a body click)
            if pin is not None and self._session.schematic.net_for_pin(
                PinRef(component_id=hit.id, pin_number=pin)
            ) is None:
                free_pos = self._session.get_pin_position(hit.id, pin)
                inst = self._session.instances.get(hit.id)
                defn = (
                    self._session.components.get(inst.definition_ref)
                    if inst is not None else None
                )
                name = (
                    defn.pin_names.get(pin, str(pin))
                    if defn is not None else str(pin)
                )
                self._status(
                    f"Pin {pin} ({name}) — no net (right-click → Assign to net)",
                    4000,
                )
        # Drive the ring: position when a free pad was clicked, else clear it.
        setter = getattr(self._view, "set_clicked_free_pad", None)
        if setter is not None:
            setter(free_pos)

    def _menu_clicked_pin(
        self, scene_pt: Point, comp_id: uuid.UUID
    ) -> int | None:
        """Pin number of *comp_id* nearest the click (within a pad pitch), else
        None.  Lets the menu offer pin-aware entries (assign/disconnect) for the
        exact pin under the cursor."""
        best: int | None = None
        best_d = 0.6 * 0.6  # half-pitch², generous (the menu is forgiving)
        for pin_num, pos in self._session.iter_pin_positions(comp_id):
            dx = pos.x - scene_pt.x
            dy = pos.y - scene_pt.y
            d = dx * dx + dy * dy
            if d <= best_d:
                best_d = d
                best = pin_num
        return best

    def _ctx_component_options(
        self, comp_id: uuid.UUID | None, scene_pt: Point
    ) -> list[ActionOption]:
        """Component options — clicked-pin assign/disconnect, lock/unlock,
        move-to-side, export pins (inside a non-root group), autoroute group.
        Mirrors the legacy component section on v2's services."""
        if comp_id is None:
            return []
        inst = self._session.instances.get(comp_id)
        defn = (
            self._session.components.get(inst.definition_ref)
            if inst is not None else None
        )
        if inst is None or defn is None:
            return []

        from protoboard.domain.schematic.net import PinRef as _PinRef

        out: list[ActionOption] = []
        clicked_pin = self._menu_clicked_pin(scene_pt, comp_id)

        # ── Clicked-pin actions (assign always; disconnect when netted) ───────
        if clicked_pin is not None:
            pin_name = defn.pin_names.get(clicked_pin, str(clicked_pin))
            cp_net = self._session.schematic.net_for_pin(
                _PinRef(component_id=comp_id, pin_number=clicked_pin)
            )
            out.append(ActionOption(
                action_id="select-tool.ctx.assign_pin",
                label=f"Assign pin {clicked_pin} ({pin_name}) to net…",
                data={"component_id": comp_id, "pin": clicked_pin},
            ))
            if cp_net is not None:
                out.append(ActionOption(
                    action_id="select-tool.ctx.disconnect_pin",
                    label=(
                        f"Disconnect pin {clicked_pin} ({pin_name})"
                        f"  ←  {cp_net.name}"
                    ),
                    data={"component_id": comp_id, "pin": clicked_pin},
                ))

        # ── Lock / Unlock (whole current selection, falling back to clicked) ──
        lock_targets: set[uuid.UUID] = {comp_id} | set(self._selected_ids)
        placements = self._session.layout.placements
        present = [t for t in lock_targets if t in placements]
        if present:
            any_locked = any(placements[t].locked for t in present)
            all_locked = all(placements[t].locked for t in present)
            if len(present) == 1:
                locked = placements[present[0]].locked
                out.append(ActionOption(
                    action_id="select-tool.ctx.lock",
                    label="\U0001f513 Unlock" if locked else "\U0001f512 Lock",
                    data={"component_ids": frozenset(present),
                          "locked": not locked},
                ))
            else:
                out.append(ActionOption(
                    action_id="select-tool.ctx.lock",
                    label=f"\U0001f512 Lock all ({len(present)})",
                    enabled=not all_locked,
                    data={"component_ids": frozenset(present), "locked": True},
                ))
                out.append(ActionOption(
                    action_id="select-tool.ctx.lock",
                    label=f"\U0001f513 Unlock all ({len(present)})",
                    enabled=any_locked,
                    data={"component_ids": frozenset(present), "locked": False},
                ))

        # ── Move to the other board side (top ↔ bottom) ──────────────────────
        cur_pl = placements.get(comp_id)
        if cur_pl is not None and not cur_pl.locked:
            other = "bottom" if cur_pl.side == "top" else "top"
            out.append(ActionOption(
                action_id="select-tool.ctx.component_side",
                label=f"Move to {other} side",
                data={"component_id": comp_id},
            ))

        # ── Export pins + autoroute, inside a non-root active group ───────────
        out += self._ctx_export_options(comp_id, defn, clicked_pin)
        return out

    def _ctx_export_options(
        self,
        comp_id: uuid.UUID,
        defn: object,
        clicked_pin: int | None,
    ) -> list[ActionOption]:
        from protoboard.domain.layout.layout import ROOT_GROUP_ID

        active_gid = self._current_group_id
        if active_gid == ROOT_GROUP_ID:
            return []
        active_grp = self._session.layout.groups.get(active_gid)
        if active_grp is None:
            return []

        out: list[ActionOption] = []
        is_direct = comp_id in active_grp.component_ids
        reexport_children = [
            (cid, self._session.layout.groups[cid])
            for cid in active_grp.child_group_ids
            if cid in self._session.layout.groups
            and self._session.layout.groups[cid].exported_pins
        ]
        if is_direct or reexport_children:
            inst = self._session.instances.get(comp_id)
            refdes = inst.refdes if inst is not None else "?"
            submenu = f"{refdes} — export/unexport pins…"
            out.append(ActionOption(
                action_id="select-tool.ctx.export_all_pins",
                label="Export all pins", submenu=submenu,
                data={"group_id": active_gid},
            ))
            out.append(ActionOption(
                action_id="select-tool.ctx.export_auto_pins",
                label="Export auto (pins on external nets)", submenu=submenu,
                data={"group_id": active_gid},
            ))
            out.append(ActionOption(
                action_id="select-tool.ctx.clear_all_pins",
                label="Unexport all pins", submenu=submenu,
                data={"group_id": active_gid},
            ))
            if is_direct:
                out += self._ctx_pin_export_options(
                    submenu, comp_id, defn, active_gid, active_grp, clicked_pin
                )

        out.append(ActionOption(
            action_id="select-tool.ctx.autoroute_group",
            label="Autoroute group (internal connections)",
            data={"group_id": active_gid},
        ))
        return out

    def _ctx_pin_export_options(
        self,
        submenu: str,
        comp_id: uuid.UUID,
        defn: object,
        active_gid: uuid.UUID,
        active_grp: ComponentGroup,
        clicked_pin: int | None,
    ) -> list[ActionOption]:
        """Per-pin export/unexport options for a directly-grouped component."""
        from protoboard.domain.layout.pad_conflict import (
            resolved_footprint_or_default,
        )
        from protoboard.domain.schematic.net import PinRef as _PinRef

        inst = self._session.instances.get(comp_id)
        fp = resolved_footprint_or_default(defn, inst)  # type: ignore[arg-type]
        pin_count = fp.pin_count()
        pin_names = defn.pin_names  # type: ignore[attr-defined]
        out: list[ActionOption] = []
        for pin_num in range(1, pin_count + 1):
            ep = _PinRef(component_id=comp_id, pin_number=pin_num)
            pin_name = pin_names.get(pin_num, str(pin_num))
            already = ep in active_grp.exported_pins
            marker = " ◀" if pin_num == clicked_pin else ""
            label = (
                f"✓ Unexport pin {pin_num} ({pin_name}){marker}"
                if already
                else f"  Export pin {pin_num} ({pin_name}){marker}"
            )
            out.append(ActionOption(
                action_id="select-tool.ctx.export_pin",
                label=label, submenu=submenu,
                data={"group_id": active_gid, "pin_ref": ep,
                      "exported": already},
            ))
        return out

    def _ctx_select_options(
        self,
        comp_id: uuid.UUID | None,
        wire_id: uuid.UUID | None,
    ) -> list[ActionOption]:
        """Select-by-net options — promote the click to its whole net(s).

        Built on v2's :class:`SessionConnQuery` (click → net ids) +
        :class:`SessionNetSource` (net → member comp/wire ids).  Simpler than
        the legacy section (no name-based global-net exclusion, no physical-vs-
        logical split) but covers the common "select the rest of this net"."""
        from protoboard.gui.selection.queries import SessionConnQuery

        conn = SessionConnQuery(self._session)
        net_ids: list[uuid.UUID] = []
        if comp_id is not None:
            net_ids = list(conn.nets_for_component(comp_id))
        elif wire_id is not None:
            nid = conn.net_for_wire(wire_id)
            if nid is not None:
                net_ids = [nid]
        if not net_ids:
            return []

        nets = self._session.schematic.nets

        def _net_name(nid: uuid.UUID) -> str:
            n = nets.get(nid)
            return n.name if n is not None else str(nid)[:8]

        group = "Select"
        out: list[ActionOption] = []
        if len(net_ids) == 1:
            nid = net_ids[0]
            out.append(ActionOption(
                action_id="select-tool.ctx.select_net_components",
                label=f"Components in net '{_net_name(nid)}'", group=group,
                data={"net_ids": (nid,)},
            ))
            out.append(ActionOption(
                action_id="select-tool.ctx.select_net_wires",
                label=f"Wires of net '{_net_name(nid)}'", group=group,
                data={"net_ids": (nid,)},
            ))
            out.append(ActionOption(
                action_id="select-tool.ctx.select_net_all",
                label=f"Wires + components of net '{_net_name(nid)}'",
                group=group, data={"net_ids": (nid,)},
            ))
        else:
            ids_t = tuple(net_ids)
            out.append(ActionOption(
                action_id="select-tool.ctx.select_net_components",
                label="Components in same nets", group=group,
                data={"net_ids": ids_t},
            ))
            out.append(ActionOption(
                action_id="select-tool.ctx.select_net_wires",
                label="Wires of same nets", group=group,
                data={"net_ids": ids_t},
            ))
            out.append(ActionOption(
                action_id="select-tool.ctx.select_net_all",
                label="Wires + components of all nets", group=group,
                data={"net_ids": ids_t},
            ))
            for nid in net_ids:
                out.append(ActionOption(
                    action_id="select-tool.ctx.select_net_all",
                    label=_net_name(nid), group=group, submenu="Net…",
                    data={"net_ids": (nid,)},
                ))
        return out

    def _ctx_highlight_options(self) -> list[ActionOption]:
        """Persistent-highlight options — push the selection at strong dimming,
        or clear an active persistent highlight."""
        group = "Highlight"
        return [
            ActionOption(
                action_id="select-tool.ctx.highlight_selection",
                label="Highlight selection (Dimmed #2)", group=group,
                data={"kind": "push"},
            ),
            ActionOption(
                action_id="select-tool.ctx.clear_highlight",
                label="Clear highlight", group=group,
                enabled=self._session.highlight_stack.has_persistent(),
                data={"kind": "clear"},
            ),
        ]

    def _ctx_wire_options(
        self, wire_id: uuid.UUID | None
    ) -> list[ActionOption]:
        """Wire mutation options — bare/insulated, side, flip, simplify,
        optimize, unweave, merge.  Mirrors the legacy wire section; the
        underlying helpers already exist on v2."""
        if wire_id is None:
            return []
        wire = self._wire_by_id(wire_id)
        if wire is None:
            return []

        # Operate on the multi-selection when the clicked wire is part of it.
        effective = set(self._selected_wire_ids)
        target_ids = (
            effective if wire.id in effective and len(effective) > 1 else {wire.id}
        )
        n = len(target_ids)
        ins_label = "insulated" if wire.insulated else "bare"
        group = (
            f"Wire{'s (' + str(n) + ')' if n > 1 else ''}"
            f" — {ins_label}, {wire.side}"
        )
        wires = frozenset(target_ids)
        out: list[ActionOption] = []

        if wire.insulated:
            out.append(ActionOption(
                action_id="select-tool.ctx.wire_insulated",
                label="Set bare (no insulation)", group=group,
                data={"wire_ids": wires, "insulated": False},
            ))
        else:
            out.append(ActionOption(
                action_id="select-tool.ctx.wire_insulated",
                label="Set insulated (add insulation jacket)", group=group,
                data={"wire_ids": wires, "insulated": True},
            ))

        other_side = "bottom" if wire.side == "top" else "top"
        out.append(ActionOption(
            action_id="select-tool.ctx.wire_side",
            label=f"Move to {other_side} layer", group=group,
            data={"wire_ids": wires, "side": other_side},
        ))

        if (
            wire.id in target_ids and len(target_ids) == 1
            and self._is_editing_wire()
        ):
            out.append(ActionOption(
                action_id="select-tool.ctx.flip_fragment",
                label=f"Flip fragment to {other_side} layer  [F]", group=group,
                data={"side": other_side},
            ))

        out.append(ActionOption(
            action_id="select-tool.ctx.simplify_wire",
            label=f"Simplify wire{' paths' if n > 1 else ' path'}", group=group,
            data={"wire_ids": wires, "mode": None},
        ))
        out.append(ActionOption(
            action_id="select-tool.ctx.simplify_wire",
            label="Chebyshev (shortcut via diag)", group=group,
            submenu="Simplify with mode",
            data={"wire_ids": wires, "mode": "chebyshev"},
        ))
        out.append(ActionOption(
            action_id="select-tool.ctx.simplify_wire",
            label="Bare-parity (respect anti-short rule)", group=group,
            submenu="Simplify with mode",
            data={"wire_ids": wires, "mode": "bare_parity"},
        ))

        # Optimize-with: one entry per registered plugin (minus built-in simplify).
        import protoboard.services.routing.builtin_optimizers  # noqa: F401
        from protoboard.services.routing.optimizer import registry as _opt_registry

        opt_names = [o for o in _opt_registry.list_available() if o != "simplify"]
        for opt_name in opt_names:
            opt = _opt_registry.get(opt_name)
            caps = opt.capabilities()
            out.append(ActionOption(
                action_id="select-tool.ctx.optimize_with",
                label=getattr(opt, "display_name", opt_name), group=group,
                submenu="Optimize with…", tooltip=caps.notes or "",
                data={"wire_ids": wires, "optimizer": opt_name},
            ))

        if n >= 2:
            out.append(ActionOption(
                action_id="select-tool.ctx.unweave_wires",
                label=f"Unweave {n} wires (topological)", group=group,
                data={"wire_ids": wires},
            ))

        mergeable = [
            w for w in self._session.layout.wires
            if w.id in target_ids and len(w.fragments) >= 2
        ]
        if mergeable:
            label = (
                f"Merge same-side fragments"
                f"{' (' + str(len(mergeable)) + ' wires)' if len(mergeable) > 1 else ''}"
            )
            out.append(ActionOption(
                action_id="select-tool.ctx.merge_wire_fragments",
                label=label, group=group,
                data={"wire_ids": wires},
            ))
        return out

    def perform_context_action(
        self, action_id: str, data: Mapping[str, object]
    ) -> None:
        """Run a parameterised option this tool published in its context.

        The dispatch counterpart of :meth:`build_context` — same ladder the
        old QMenu data-tuples drove, keyed by the option's ``action_id`` with
        the structural payload in *data* (qt-ports §2.4, variant 1).
        """
        from typing import cast

        action = action_id.removeprefix("select-tool.ctx.")
        if action == "lock":
            self._menu_lock(
                set(cast("frozenset[uuid.UUID]", data["component_ids"])),
                cast(bool, data["locked"]),
            )
        elif action == "assign_pin":
            self._menu_assign_pin(
                cast("uuid.UUID", data["component_id"]), cast(int, data["pin"])
            )
        elif action == "disconnect_pin":
            self._menu_disconnect_pin(
                cast("uuid.UUID", data["component_id"]), cast(int, data["pin"])
            )
        elif action in (
            "export_pin", "export_all_pins", "export_auto_pins", "clear_all_pins"
        ):
            self._menu_export(action, data)
        elif action == "autoroute_group":
            self._menu_autoroute_group()
        elif action in (
            "select_net_components", "select_net_wires", "select_net_all"
        ):
            self._menu_select_net(
                action, cast("tuple[uuid.UUID, ...]", data["net_ids"])
            )
        elif action == "highlight_selection":
            self._menu_highlight_selection()
        elif action == "clear_highlight":
            self._session.highlight_stack.clear()
        elif action == "wire_insulated":
            self._menu_set_wire_insulated(
                set(cast("frozenset[uuid.UUID]", data["wire_ids"])),
                cast(bool, data["insulated"]),
            )
        elif action == "wire_side":
            self._menu_set_wire_side(
                set(cast("frozenset[uuid.UUID]", data["wire_ids"])),
                cast(str, data["side"]),
            )
        elif action == "component_side":
            self._menu_flip_component_side(
                cast("uuid.UUID", data["component_id"])
            )
        elif action == "flip_fragment":
            self._flip_wire_fragment(cast(str, data["side"]))
        elif action == "simplify_wire":
            self._simplify_wires(
                set(cast("frozenset[uuid.UUID]", data["wire_ids"])),
                mode_override=cast("str | None", data["mode"]),
            )
        elif action == "optimize_with":
            self._optimize_selected_wires(
                set(cast("frozenset[uuid.UUID]", data["wire_ids"])),
                cast(str, data["optimizer"]),
            )
        elif action == "unweave_wires":
            self._unweave_wires(
                set(cast("frozenset[uuid.UUID]", data["wire_ids"]))
            )
        elif action == "merge_wire_fragments":
            self._merge_wires_and_fragments(
                frozenset(cast("frozenset[uuid.UUID]", data["wire_ids"]))
            )

    def _menu_set_wire_insulated(
        self, wire_ids: set[uuid.UUID], insulated: bool
    ) -> None:
        from typing import cast

        from protoboard.domain.commands.base import Command, ComponentState, MacroCommand
        from protoboard.domain.commands.component_cmds import (
            SetWireInsulatedCommand,
        )

        raw = [
            cast("Command[ComponentState]",
                 SetWireInsulatedCommand(wire_id=wid, insulated=insulated))
            for wid in wire_ids
        ]
        if not raw:
            return
        cmd = raw[0] if len(raw) == 1 else MacroCommand(
            raw, description=f"Set {len(raw)} wires"
        )
        self._execute_command(cmd)
        self._session.layout_changed.emit()

    def _menu_set_wire_side(self, wire_ids: set[uuid.UUID], side: str) -> None:
        from typing import cast

        from protoboard.domain.commands.base import Command, ComponentState, MacroCommand
        from protoboard.domain.commands.component_cmds import SetWireSideCommand
        from protoboard.types import Side

        raw = [
            cast("Command[ComponentState]",
                 SetWireSideCommand(wire_id=wid, side=cast(Side, side)))
            for wid in wire_ids
        ]
        if not raw:
            return
        cmd = raw[0] if len(raw) == 1 else MacroCommand(
            raw, description=f"Move {len(raw)} wires"
        )
        self._execute_command(cmd)
        self._session.layout_changed.emit()

    def _menu_flip_component_side(self, comp_id: uuid.UUID) -> None:
        """Move a component to the other board side (top ↔ bottom).

        A physical flip: :func:`flip_component_side` toggles ``side`` and shifts
        ``position`` so the body lands back on the same pads (see
        docs/geometry.md §5). Committed through :class:`MoveComponentCommand`,
        which runs the same conflict check as a move — a hard conflict (body
        overlap / different-net pad) leaves the component where it is.
        """
        from protoboard.domain.commands.component_cmds import MoveComponentCommand
        from protoboard.domain.components.package import resolved_footprint
        from protoboard.domain.layout.placement import flip_component_side

        pl = self._session.layout.placements.get(comp_id)
        inst = self._session.instances.get(comp_id)
        if pl is None or inst is None:
            return
        defn = self._session.components.get(inst.definition_ref)
        if defn is None:
            return
        families = getattr(self._session, "package_families", None)
        fp = resolved_footprint(defn, inst, families)
        self._execute(
            MoveComponentCommand(
                component_id=comp_id,
                new_placement=flip_component_side(fp, pl),
                old_placement=pl.copy(),
            )
        )
        self._notify_selection()  # refresh Properties panel (side changed)

    def _menu_lock(self, comp_ids: set[uuid.UUID], locked: bool) -> None:
        from protoboard.domain.commands.base import MacroCommand
        from protoboard.domain.commands.component_cmds import (
            LockComponentCommand,
        )

        cmds = [
            LockComponentCommand(component_id=t, locked=locked)
            for t in comp_ids if t in self._session.layout.placements
        ]
        if not cmds:
            return
        cmd = cmds[0] if len(cmds) == 1 else MacroCommand(
            cmds,
            description=f"{'Lock' if locked else 'Unlock'} {len(cmds)} component(s)",
        )
        self._execute_command(cmd)
        self._session.layout_changed.emit()

    def _menu_assign_pin(self, comp_id: uuid.UUID, pin_num: int) -> None:
        """Delegate to MainWindow's parameterised assign dialog (single source
        with the Nets panel UX)."""
        mw = self._view.window() if self._view is not None else None
        slot = getattr(mw, "_pin_assign_net_for", None)
        if callable(slot):
            slot(comp_id, pin_num)

    def _menu_disconnect_pin(self, comp_id: uuid.UUID, pin_num: int) -> None:
        from protoboard.domain.commands.component_cmds import (
            DisconnectPinCommand,
        )

        self._execute_command(
            DisconnectPinCommand(component_id=comp_id, pin_number=pin_num)
        )
        self._session.layout_changed.emit()

    def _menu_export(self, action: str, data: Mapping[str, object]) -> None:
        from typing import cast

        from protoboard.domain.commands.group_cmds import SetExportedPinsCommand
        from protoboard.domain.schematic.net import PinRef

        group_id = cast("uuid.UUID", data["group_id"])
        grp = self._session.layout.groups.get(group_id)
        if grp is None:
            return
        new_exported: frozenset[PinRef]
        if action == "export_pin":
            ep = cast(PinRef, data["pin_ref"])
            already = cast(bool, data["exported"])
            new_exported = (
                grp.exported_pins - {ep} if already
                else grp.exported_pins | {ep}
            )
        elif action == "export_all_pins":
            new_exported = self._session.group_tree().exposable_surface(
                group_id, self._session.instances, self._session.components,
            )
        elif action == "export_auto_pins":
            new_exported = self._session.group_tree().external_pins_for_group(
                group_id, self._session.schematic,
            )
        else:  # clear_all_pins
            new_exported = frozenset()
        if new_exported != grp.exported_pins:
            self._execute_command(
                SetExportedPinsCommand(
                    group_id=group_id, new_exported=new_exported
                )
            )
            self._session.layout_changed.emit()

    def _menu_autoroute_group(self) -> None:
        """Delegate to MainWindow's group-autoroute slot (worker plumbing)."""
        mw = self._view.window() if self._view is not None else None
        slot = getattr(mw, "_group_autoroute", None)
        if callable(slot):
            slot()

    def _menu_select_net(
        self, action: object, net_ids: tuple[uuid.UUID, ...]
    ) -> None:
        """Replace the selection with the components and/or wires of *net_ids*."""
        from protoboard.gui.selection.queries import SessionNetSource

        src = SessionNetSource(self._session)
        comp_ids: set[uuid.UUID] = set()
        wire_ids: set[uuid.UUID] = set()
        want_comps = action in ("select_net_components", "select_net_all")
        want_wires = action in ("select_net_wires", "select_net_all")
        for nid in net_ids:
            if want_comps:
                comp_ids |= set(src.net_component_ids(nid))
            if want_wires:
                wire_ids |= set(src.wire_ids_on_net(nid))
        self._set_selection_from_ids(comp_ids, wire_ids, set())
        self._notify_selection()

    def _menu_highlight_selection(self) -> None:
        """Push the current selection onto the highlight stack at strong dimming
        (persistent #2) — the "spotlight this" action."""
        from protoboard.gui.highlight_state import (
            PROFILE_DIMMED_2,
            HighlightEntry,
        )

        comp_ids = frozenset(self._selected_ids)
        wire_ids = frozenset(self._selected_wire_ids)
        if not comp_ids and not wire_ids:
            return
        self._session.highlight_stack.push(
            HighlightEntry(
                comp_ids=comp_ids,
                wire_ids=wire_ids,
                profile=PROFILE_DIMMED_2,
                label="User",
            )
        )

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
            RemoveFromGroupCommand,
            SetExportedPinsCommand,
        )
        from protoboard.gui.tools.select_prompts import GroupTargetRequest

        groups = self._session.layout.groups
        active_gid = self._current_group_id
        active = groups.get(active_gid)
        child_groups = [
            groups[gid]
            for gid in (active.child_group_ids if active else frozenset())
            if gid in groups and gid != ROOT_GROUP_ID
        ]

        default_name = f"Group{len(groups) + 1}"
        assert self._group_prompt is not None  # set by activate() / injection
        choice = self._group_prompt(
            GroupTargetRequest(
                existing_groups=tuple(child_groups),
                default_name=default_name,
            )
        )
        if choice is None:
            return None

        if choice.mode == "new":
            new_group, promoted_free, promoted_groups = (
                self._prepare_new_group_promoted(
                    active_gid,
                    ids,
                    default_name,
                    choice.auto_export_external,
                    choice.new_name,
                )
            )
            return self._execute_group_promotion(
                active_gid, new_group, promoted_free, promoted_groups
            )

        # Add to an existing group.
        target_id = choice.target_group_id
        if target_id is None:
            return None
        self._exec_command(
            AddToGroupCommand(group_id=target_id, component_ids=ids)
        )
        if target_id != active_gid:
            self._exec_command(
                RemoveFromGroupCommand(group_id=active_gid, component_ids=ids)
            )
        if choice.auto_export_external:
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

    def _prepare_new_group_promoted(
        self,
        active_gid: uuid.UUID,
        ids: frozenset[uuid.UUID],
        default_name: str,
        auto_export_external: bool,
        new_name: str | None,
    ) -> tuple[ComponentGroup, frozenset[uuid.UUID], frozenset[uuid.UUID]]:
        """Promote the selection and build the (still uncommitted) new group.

        Splits *ids* into free components + top-level groups under *active_gid*,
        mints the new group id, computes its auto-exported pins (against a
        transient tree that already contains the prospective group) when
        requested, and returns ``(new_group, promoted_free, promoted_groups)``.
        """
        from protoboard.domain.layout.group import ComponentGroup
        from protoboard.domain.layout.group_tree import GroupTree
        from protoboard.domain.schematic.net import PinRef

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
        if auto_export_external:
            transient = ComponentGroup(
                id=new_group_id,
                name=new_name or default_name,
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
            name=new_name or default_name,
            component_ids=promoted_free,
            child_group_ids=promoted_groups,
            exported_pins=exported,
        )
        return new_group, promoted_free, promoted_groups

    def _execute_group_promotion(
        self,
        active_gid: uuid.UUID,
        new_group: ComponentGroup,
        promoted_free: frozenset[uuid.UUID],
        promoted_groups: frozenset[uuid.UUID],
    ) -> uuid.UUID:
        """Run the create + reparent command sequence for a new promoted group.

        Order is undo-sensitive: create the group, detach each promoted child
        from its old parent, attach the new group under *active_gid*, then detach
        the promoted free components from *active_gid*.  Selects the new group
        and returns its id.
        """
        from protoboard.domain.commands.group_cmds import (
            AddToGroupCommand,
            CreateGroupCommand,
            RemoveFromGroupCommand,
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
        from typing import cast

        from protoboard.gui.ports import BlockingJob
        from protoboard.gui.selection.wire_ops import build_simplify_inputs
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

        worker_out = cast(
            "list[tuple[uuid.UUID, list[Point], list[Point]]] | None",
            self._view.executor.run_blocking(
                BlockingJob(
                    title="Simplify",
                    label=f"Simplifying {len(wid_list)} wire(s)…",
                    fn=_simplify_in_worker,
                    with_progress=True,
                )
            ),
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
        from typing import cast

        from protoboard.domain.commands.component_cmds import (
            AddWireCommand,
            RemoveWireCommand,
        )
        from protoboard.gui.ports import BlockingJob
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

        out = cast(
            "list[Wire] | None",
            self._view.executor.run_blocking(
                BlockingJob(
                    title="Unweave",
                    label=f"Unweaving {len(targets)} wire(s)…",
                    fn=_unweave_in_worker,
                )
            ),
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

    def _compute_fragment_collapse_commands(
        self, post_fuse_ids: set[uuid.UUID]
    ) -> tuple[list[object], int, int]:
        """Phase-2: build fragment-collapse commands against the post-fuse layout.

        Must be called while the fuse is applied; examines each surviving wire's
        fragments and emits a :class:`ReplaceWireFragmentsCommand` per wire whose
        same-side fragments coalesce.  Returns ``(commands, n_fragment_wires,
        n_fragments_removed)``.
        """
        from protoboard.domain.commands.component_cmds import (
            ReplaceWireFragmentsCommand,
        )
        from protoboard.domain.layout.wire import Fragment
        from protoboard.domain.layout.wire_utils import coalesce_same_side_fragments

        session = self._session
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
        return frag_cmds, n_fragment_wires, n_fragments_removed

    def _merge_wires_and_fragments(
        self, wire_ids: frozenset[uuid.UUID] | None
    ) -> None:
        """Two-phase wire merge — fuse touching wires + coalesce same-side fragments."""
        from typing import cast

        from protoboard.domain.commands.base import ComponentState
        from protoboard.domain.commands.component_cmds import (
            FuseTouchingWiresCommand,
        )

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

        frag_cmds, n_fragment_wires, n_fragments_removed = (
            self._compute_fragment_collapse_commands(post_fuse_ids)
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

    @traced()
    def _execute(self, cmd: object) -> None:
        self._exec_command(cmd)
        self._session.layout_changed.emit()
        self._refresh_highlight()

    # -- group navigation ---------------------------------------------------

    @traced()
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

    @traced()
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
