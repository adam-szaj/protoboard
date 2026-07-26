# SPDX-License-Identifier: MIT
"""``SelectTool`` — the selection tool (rewritten as a service).

A thin ``ControllerTool`` that wires the headless-tested pieces together:

* :class:`~protoboard.controller.selection.hit_test.HitTester` — point → Selectable,
* :class:`~protoboard.controller.selection.service.SelectionService` — selection state
  and the active-pick routing,
* :class:`~protoboard.controller.highlight_controller.HighlightController` — dimming,
* :class:`~protoboard.controller.selection.drag_controller.DragController` and
  :class:`~protoboard.controller.selection.rubber_band_controller.RubberBandController`
  — concrete interactions behind one
  :class:`~protoboard.controller.selection.mouse_input.MouseInputController` seam.

Covers: hit-test → selection (SET/ADD/REMOVE via the modifier matrix),
rubber-band area-select, drag / floating of the selection, rotate / flip /
rotate-around-point, ESC-pop, group enter/exit, and routing clicks into an
active pick.  Wire-edit and net-relation selection arrive in later phases.

The tool keeps almost no logic of its own — on press it arms a mouse-interaction
controller or mutates the selection; pointer events and translated intents
(CONFIRM / CANCEL / UNDO) forward to the active controller; the behaviour lives
in tested, Qt-free controllers.

Qt-free (controller/tools/): raw-key dispatch (``on_key`` / ``_key_to_intent``)
and the default Qt preview / group-target prompt construction live on the
GUI-composed subclass in :mod:`protoboard.gui.tools.gui_tools`; this module
keeps the command methods those key branches call.
"""

from __future__ import annotations

import uuid
from typing import TYPE_CHECKING, Protocol

import structlog

from protoboard.domain.layout.layout import ROOT_GROUP_ID
from protoboard.geometry import Point
from protoboard.controller.tools.base import ControllerTool, _ViewProto
from protoboard.controller.highlight_controller import HighlightController
from protoboard.controller.ports import (
    LEFT_BUTTON,
    ActionOption,
    HoverEvent,
    InteractionContext,
    PointerInput,
    StatusEvent,
    WheelInput,
)
from protoboard.controller.selection.drag_controller import DragController, FloatingPhase
from protoboard.controller.selection.hit_test import HitTester, SelectionFilters
from protoboard.controller.selection.mouse_input import Intent, Mods
from protoboard.controller.selection.rubber_band_controller import RubberBandController
from protoboard.controller.selection.service import SelectionService
from protoboard.controller.selection_fsm import BasisScope, SetOp
from protoboard.services.debug_trace import traced
from protoboard.controller.editing.item_handle import ItemId, ItemTag

_log = structlog.get_logger(__name__)

if TYPE_CHECKING:
    from collections.abc import Callable

    from collections.abc import Mapping

    from protoboard.domain.components.definition import ComponentDefinition
    from protoboard.domain.components.instance import ComponentInstance
    from protoboard.domain.components.package import RedragResult
    from protoboard.domain.layout.group import ComponentGroup
    from protoboard.domain.layout.group_tree import GroupTree
    from protoboard.domain.layout.placement import ComponentPlacement
    from protoboard.domain.layout.wire import Wire
    from protoboard.controller.editing.drag_set import DragSet
    from protoboard.controller.selection.adapters import WireVertexSelectable
    from protoboard.controller.tools.armed_transform_tool import ArmedTransformTool
    from protoboard.controller.tools.wire_edit_tool import WireEditTool
    from protoboard.controller.selection.mouse_input import MouseInputController
    from protoboard.controller.selection.selectable import Selectable
    from protoboard.controller.selection.wire_edit import WireEditState
    from protoboard.controller.editing import EditState
    from protoboard.controller.ports.executor import ExecutorPort
    from protoboard.controller.tools.drag_commit_decision import DragCommitDecision
    from protoboard.controller.tools.select_prompts import GroupTargetPrompt
    from protoboard.controller.tools.transform_overlays import TransformOverlaysPort
    from protoboard.controller.tools.select_preview import SelectPreview

# Below this travel (raster units) a press+release is a click, not a drag —
# used to synthesise the on_click event for click-driven controllers (picks).
_CLICK_THRESHOLD = 0.3

# Namespaced key actions this tool declares.  None → inherit the generic
# ``global.action.<name>`` default (so Delete/Edit/Cancel follow whatever the
# user bound globally); a literal overrides for this tool.  The shortcut veto
# and key dispatch resolve keys against these *by id*, never by hard-coded raw
# key literals — so a user rebinding any of them is honoured automatically.
_ACT_EDIT = "select-tool.action.edit"
_ACT_DELETE = "select-tool.action.delete"
_ACT_CANCEL = "select-tool.action.cancel"
# Transform entry keys: [R] arms float-rotate, [G] arms keyboard float-move
# (both hand off to the ArmedTransformTool, which owns the step/wheel keys on
# its own layer); [Alt+R] is an immediate flip. Dispatched by id, never raw key.
_ACT_ROTATE = "select-tool.action.rotate"
_ACT_MOVE = "select-tool.action.move"
_ACT_FLIP = "select-tool.action.flip"

# Mouse-chord action: the unified RMB context menu, layered over the canvas menu.
_ACT_CONTEXT_MENU = "select-tool.action.context_menu"
# Global relation-cycle actions (backtick / Shift+backtick) — the ids live in
# keybindings.py with their own defaults; the tool dispatches them in on_key.
_ACT_RELATION_CYCLE = "highlight_cycle"
_ACT_RELATION_CYCLE_BACK = "relation_cycle_back"

class SelectToolView(_ViewProto, Protocol):
    """The view surface SelectTool depends on (qt-ports §4 pattern).

    SelectTool needs exactly the shared controller→view contract, so this is
    just ``ControllerTool``'s :class:`_ViewProto` under a tool-local name (kept
    for readable signatures). Any SelectTool-only view member would be added
    here on top of the inherited base.
    """


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


class SelectTool(ControllerTool):
    """Selection tool built on the SelectionService / HitTester / Highlight stack."""

    def __init__(
        self,
        preview: SelectPreview | None = None,
        group_prompt: GroupTargetPrompt | None = None,
        armed_transform_factory: Callable[..., ArmedTransformTool] | None = None,
        wire_edit_factory: Callable[..., WireEditTool] | None = None,
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
        # ArmedTransformTool constructor — injected callable; the GUI-composed
        # subclass's activate() defaults it to the Qt-composed
        # ``gui.tools.gui_tools.ArmedTransformTool`` (preview + overlays +
        # on_key).  Kept Qt-free here: this tool only needs it to be an
        # ``ArmedTransformTool`` (isinstance check in ``_invoke_armed_atomic``).
        self._armed_transform_factory: Callable[..., ArmedTransformTool] | None = (
            armed_transform_factory
        )
        self._armed_transform_factory_injected = armed_transform_factory is not None
        # WireEditTool constructor — injected callable; the GUI-composed
        # subclass's activate() defaults it to the Qt-composed
        # ``gui.tools.gui_tools.WireEditTool`` (preview + on_key).  ``[E]`` on a
        # selected wire resolves the fragment range then hands off to this tool
        # (mirrors ``_armed_transform_factory``).
        self._wire_edit_factory: Callable[..., WireEditTool] | None = (
            wire_edit_factory
        )
        self._wire_edit_factory_injected = wire_edit_factory is not None
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
        # Live-drag conflict corner label + selection-outline override — the
        # SOFT-amber/HARD-red feedback shared with ArmedTransformTool (Qt-side
        # in :class:`~protoboard.gui.tools.transform_overlays.TransformOverlays`,
        # injected by the GUI-composed subclass's ``activate()``; ``None``
        # headless).  Cleared the moment the drag returns to CLEAR so the
        # frame never lingers after the cursor leaves the conflict zone (the
        # legacy "sticky yellow frame" complaint, 2026-06-19).
        self._overlays: TransformOverlaysPort | None = None
        # Rotate-around-point pivot capture (legacy menu path; seeds the armed
        # rotate's shared pivot).
        self._pivot_capture_pending: bool = False
        self._rotate_pivot: object | None = None  # geometry.Point | None
        # Re-drag (bendable lead reposition) — a request_pick client; the
        # overlay lives in the preview, the pick state lives in the service.
        self._redrag_comp_id: uuid.UUID | None = None
        self._redrag_pin: int | None = None
        # Wire-edit ENTRY state (tool-local, like _rotate_pivot): the in-flight
        # two-vertex range pick that resolves a wire fragment.  Once both
        # vertices are picked, `_range_picked` hands off to the WireEditTool
        # (which owns the fragment ops); SelectTool never itself runs an op.
        # `_wire_edit` therefore stays None in practice — it is the transient
        # in-progress range only the draw-overlay reads.
        from protoboard.controller.selection.hit_test import WireEditCycle

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
        from protoboard.controller.selection_axes import RelationClass, SelectionFilter

        self._relation: RelationClass = RelationClass.SAME_LOGICAL_NET
        # Selection filter (which object kinds may be selected).  Shim for
        # Phase 6a: state is held + mirrored to the status-bar widget, but
        # `_filters()` does NOT yet enforce it (all kinds stay hittable).
        # Enforcement in the hit-test is a deferred follow-up.
        self._filter: SelectionFilter = SelectionFilter.ALL

    # -- ControllerTool lifecycle --------------------------------------------

    @property
    def preview(self) -> SelectPreview:
        assert self._preview is not None  # set by activate() / injection
        return self._preview

    @property
    def _view(self) -> SelectToolView:  # type: ignore[override]
        """The bound view, typed as SelectTool's own view surface.

        The base stores ``_view`` as ``_ViewProto | None`` (None until
        activate). SelectTool binds a wider :class:`SelectToolView`; this
        accessor re-types the getter to it so the post-activate ``self._view.*``
        event-handler call sites don't each need a None-guard.

        It deliberately does **not** assert non-None: several methods (e.g.
        :meth:`_status`) legitimately run before/without a bound view and guard
        with ``if self._view is None`` — an assert here would turn those guards
        into crashes.  The stored value is returned as-is (may be None at
        runtime); the return *type* stays narrow for the common guarded paths.
        """
        return self.__view  # type: ignore[return-value]  # may be None pre-activate; guarded call sites check

    @_view.setter
    def _view(self, value: SelectToolView | None) -> None:
        self.__view = value

    @traced()
    def activate(self, session: EditState, view: SelectToolView) -> None:
        self._session = session
        self._view = view  # pyright: ignore[reportIncompatibleVariableOverride]
        # SelectionService / HighlightController are per-session — reuse one
        # parked on the session if present, else create and park it.
        svc = getattr(session, "selection_service", None)
        if not isinstance(svc, SelectionService):
            svc = SelectionService()
            session.selection_service = svc
        self._selection = svc
        self._highlight = HighlightController(
            session.highlight_stack, source_id="select_tool"
        )

    @traced()
    def deactivate(self) -> None:
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
        if self._overlays is not None:
            self._overlays.clear()

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
        from protoboard.controller.selection_axes import SelectionFilter

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
            from protoboard.controller.selection.adapters import PointSelectable

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
        from protoboard.controller.selection.queries import SessionConnQuery

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
        tag = ItemTag.parse(kind)
        out: set[uuid.UUID] = set()
        if self._selection is None:  # pre-activate
            return out
        for sel in self._selection.selected():
            iid = sel.to_hit().item_id()
            if iid is not None and iid.tag is tag:
                out.add(iid.uuid)
        return out

    def _set_selection_from_ids(
        self,
        comp_ids: set[uuid.UUID],
        wire_ids: set[uuid.UUID],
        group_ids: set[uuid.UUID],
    ) -> None:
        """Rebuild the service selection from raw id sets (adapter-wrapped)."""
        from protoboard.controller.selection.adapters import (
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
    def _multi_highlights(self) -> list[object]:
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
        from protoboard.controller.selection.queries import SessionConnQuery

        nets = primary.net_ids(SessionConnQuery(self._session))
        return next(iter(nets), None)

    def _delete_selection(self, *, force: bool = False) -> bool:
        """Delete the selected wires and/or components.  Returns True if it ran.

        Resolves the selection to ids and hands the actual delete (group-lock
        filtering + atomic macro) to :meth:`EditController.delete`, passing this
        tool's active-group context explicitly.  Selection bookkeeping (wire-
        edit clear, multi-selection clear, re-broadcast) stays here — it is
        interaction state the stateless controller deliberately does not know.

        ``_wire_edit_clear`` runs *before* the controller call so the wire-edit
        overlay is already torn down when the delete's ``layout_changed`` fires
        (matching the legacy order); it is idempotent, so clearing on the
        no-command path is harmless.
        """
        # Delete acts on components + wires only; a selected group is left for
        # the separate Ungroup action (Delete does not disband today).
        item_ids = [
            ItemId.component(cid) for cid in self._selected_by_kind("component")
        ] + [
            ItemId.wire(wid) for wid in self._selected_by_kind("wire")
        ]
        self._wire_edit_clear()
        ran = self._session.edit_controller.delete(
            item_ids,
            active_group_id=self._current_group_id,
            force=force,
        )
        if not ran:
            return False
        self._clear_multi_selection()
        self._notify_selection()
        return True

    def _wire_is_locked(self, wire_id: uuid.UUID) -> bool:
        """True iff *wire_id* is structurally owned by a group viewed from outside.

        Mirrors :meth:`_is_locked_in_group` for components: selecting a locked
        wire (for highlight) stays allowed, but mutating it (delete/edit) gates
        here and skips it.  Delegates to the shared pure guard in
        ``controller.editing.group_lock``.
        """
        from protoboard.controller.editing.group_lock import wire_locked_in_group

        return wire_locked_in_group(
            self._session, wire_id, active_group_id=self._current_group_id
        )

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
        from protoboard.controller.selection_axes import RELATION_LABELS, RelationClass

        if isinstance(relation, RelationClass):
            self._relation = relation
            self._refresh_highlight()
            if self._session is not None:
                # GUI-only Qt signal: only EditorSession carries it, not the
                # Qt-free EditState protocol / HeadlessSession. Routing this
                # through an EditState notify verb is deferred (docs/todo.md).
                self._session.highlight_mode_changed.emit(  # type: ignore[attr-defined]  # pyright: ignore[reportAttributeAccessIssue]
                    RELATION_LABELS[relation]
                )

    def _cycle_relation(self, *, forward: bool = True) -> None:
        """Step the relation axis to the next/previous class — backtick / Shift+`.

        The keyboard counterpart to the status-bar relation widget; lost in the
        v2 rewrite (the action ids existed in the KeyMap but nothing dispatched
        them), restored by the parity test."""
        from protoboard.controller.selection_axes import RelationClass

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
        from protoboard.controller.selection_axes import SelectionFilter

        if isinstance(f, SelectionFilter):
            self._filter = f

    def _relation_source(self, sel: Selectable):  # type: ignore[no-untyped-def]
        """Map a hit Selectable to a :class:`RelationSource` for the queries."""
        from protoboard.controller.selection.queries import RelationSource

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
        from protoboard.controller.selection.adapters import (
            ComponentSelectable,
            WireSelectable,
        )
        from protoboard.controller.selection.queries import (
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
        from protoboard.controller.selection_axes import SelectionFilter

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
    def empty_action(self, point: Point, mods: Mods) -> HoverEvent:
        """Hover verb (docs MVC step 1) — report what is under the cursor.

        While an interaction (drag / band / pick) is live the move belongs to
        it, so we forward it and report nothing hit.  Otherwise it is a plain
        hover: hit-test eagerly and reply with the domain id under the cursor
        (empty over bare canvas).  Pure query — it never changes the selection
        or the interaction state.
        """
        if self._mouse is not None:
            self._mouse.on_move(point, mods)
            return HoverEvent(point=point)
        sel = self._hit_tester().hit(point.x, point.y, self._filters())
        objects: tuple[uuid.UUID, ...] = ()
        if sel is not None:
            hid = sel.to_hit().id
            if isinstance(hid, uuid.UUID):
                objects = (hid,)
        return HoverEvent(point=point, objects=objects)

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
        from protoboard.controller.selection.adapters import WireSelectable

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
        from protoboard.controller.selection.adapters import GroupSelectable

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

        from protoboard.controller.selection.drag_controller import DragContext

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
        # GUI-only Qt signal (see the highlight_mode_changed note above) —
        # absent from the Qt-free EditState protocol; deferred (docs/todo.md).
        self._session.drag_moved.emit()  # type: ignore[attr-defined]  # pyright: ignore[reportAttributeAccessIssue]

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

        Shared by drag and armed-rotate previews (SOFT amber / HARD red while
        conflicting, hidden the instant the state is CLEAR or ``None`` so the
        frame never lingers) — delegates to the Qt-side
        :class:`~protoboard.gui.tools.transform_overlays.TransformOverlays`
        the GUI-composed subclass injects on ``activate()``; a no-op headless.
        """
        if self._overlays is not None:
            self._overlays.show_conflict(decision)

    def _clear_drag_corner(self) -> None:
        """Hide the corner label, reset the selection outline, un-ghost wires.

        Every drag-end path routes through here so the frame/outline can't
        stick after the cursor leaves the conflict zone or the drag ends.
        """
        if self._overlays is not None:
            self._overlays.clear()
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
        from protoboard.controller.editing.drag_set import DragSet

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

        Delegates to the pure guard in ``controller.editing.group_lock`` (shared
        with :meth:`EditController.delete`), binding the active group from this
        tool's interaction context.
        """
        from protoboard.controller.editing.group_lock import (
            component_locked_in_group,
        )

        return component_locked_in_group(
            self._session, comp_id, active_group_id=self._current_group_id
        )

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
        self._session.notify_changed()
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

        from protoboard.controller.selection.adapters import ComponentSelectable
        from protoboard.controller.selection.mouse_input import Mods
        from protoboard.controller.selection.rubber_band_controller import BandHit

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
    #
    # ``_key_to_intent`` (Qt key → semantic Intent) lives on the GUI-composed
    # subclass's ``on_key`` in :mod:`protoboard.gui.tools.gui_tools` — this is
    # the command method it dispatches into.

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
        from protoboard.controller.selection import Stop

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
        from protoboard.controller.selection import Stop

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
        from protoboard.controller.selection.adapters import ComponentSelectable

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
        from protoboard.controller.selection.adapters import ComponentSelectable

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

        from protoboard.controller.selection.point_pick_controller import (
            PointPickController,
        )
        from protoboard.controller.selection.selectable import PickKind, Stop

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
        from protoboard.controller.selection.adapters import PointSelectable

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
        per-kind action is routed through the handle's ``.call`` visitor — the
        seam that expresses "one op name, a different action per kind" without an
        ``if``-ladder.  Group takes priority over wire when both are selected
        (entering the group is the coarser, containing edit).
        """
        gid = self._selected_group_id
        wid = self._selected_wire_id
        if gid is not None:
            target = ItemId.group(gid)
        elif wid is not None:
            target = ItemId.wire(wid)
        else:
            self._status("Nothing to edit — select a group or wire first")
            return
        handle = self._session.edit_controller.get_item(
            target, active_group_id=self._current_group_id
        )
        handle.call(
            group_call=lambda _g: self.enter_group(),
            wire_call=lambda _w: self.arm_wire_edit(),
        )

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
        from protoboard.controller.selection.hit_test import WireEditCycle
        from protoboard.controller.selection.selectable import Stop

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
        from protoboard.controller.selection.wire_edit import replace_wire_path_cmd

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
        from protoboard.controller.selection.adapters import WireVertexSelectable

        if picked and isinstance(picked[-1], WireVertexSelectable):
            self._wire_edit_start = picked[0] if isinstance(
                picked[0], WireVertexSelectable
            ) else None
        self._draw_wire_edit_overlay()

    def _range_picked(self, picked: list[Selectable]) -> None:
        """Resolve the fragment range from the two picked vertices, then hand
        off to the :class:`WireEditTool` (mirrors :meth:`_arm_transform`).

        SelectTool owns the two-vertex ``wire-vertex`` pick (and the pad-vertex
        materialisation done at :meth:`arm_wire_edit` entry); once the range
        resolves, the fragment edit itself lives in its own tool.  The ops (
        reroute / autoroute / flip / simplify / delete) stay armed after each
        commit; ``return_to`` drops back to this SelectTool (delete / [Esc]),
        re-syncing the selection highlight to whatever wire survived.
        """
        from protoboard.controller.selection.adapters import WireVertexSelectable
        from protoboard.controller.selection.wire_edit import WireEditState

        verts = [p for p in picked if isinstance(p, WireVertexSelectable)]
        if len(verts) < 2:
            self._status_msg("Wire edit: pick two different vertices on one wire.")
            self._wire_edit_clear()
            return
        v0, v1 = verts[0], verts[1]
        start_wire, start_idx = v0.wire_id, v0.index
        end_wire, end_idx = v1.wire_id, v1.index
        # Same-wire: normalise so start_idx <= end_idx (legacy swap); a zero-
        # length range is not a fragment.
        if start_wire == end_wire:
            if start_idx == end_idx:
                self._status_msg("Wire edit: pick two different vertices on one wire.")
                self._wire_edit_clear()
                return
            if end_idx < start_idx:
                start_idx, end_idx = end_idx, start_idx
        wire_range = WireEditState(
            start_wire_id=start_wire,
            start_idx=start_idx,
            end_wire_id=end_wire,
            end_idx=end_idx,
        )
        wire_id = self._wire_edit_wire_id
        assert wire_id is not None  # set by arm_wire_edit before the pick
        # Drop SelectTool's own wire-edit overlay/pick state before handing off —
        # the WireEditTool draws its own overlay.
        self._wire_edit_clear()
        assert self._wire_edit_factory is not None  # set by activate() / injection
        self._view.tools.set_tool(
            self._wire_edit_factory(
                wire_range,
                wire_id,
                return_to=lambda: self._return_from_wire_edit(wire_id),
            )
        )

    def _return_from_wire_edit(self, wire_id: uuid.UUID) -> None:
        """Hand control back from :class:`WireEditTool` to this SelectTool.

        Re-arms this tool and re-syncs the selection highlight to the wire that
        survived the edit: reroute/simplify/flip preserve *wire_id* but replace
        the ``Wire`` object (new geometry), and a cross-wire op or delete may
        remove it entirely.  Without this re-sync the selection would keep a
        stale ``Wire`` snapshot / a dead wire id and the highlight would render
        pre-op geometry (memory ``feedback_refresh_highlights_after_mutation``).
        """
        from protoboard.controller.selection.adapters import WireSelectable

        self._view.tools.set_tool(self)
        # Clear first: the per-session SelectionService still holds the wire
        # selection from before [E], but with a STALE ``Wire`` snapshot (the op
        # replaced the object).  A bare ``set`` of the same hit would toggle it
        # OFF; clear-then-set re-seats the current object deterministically.
        self._selection.clear()
        self._context_net_id = None
        wire = self._wire_by_id(wire_id)
        if wire is not None:
            self._selection.set(WireSelectable(wire=wire))
        self._refresh_highlight()

    def _wire_by_id(self, wire_id: uuid.UUID) -> Wire | None:
        return next(
            (w for w in self._session.layout.wires if w.id == wire_id), None
        )

    def _refresh_after_wire_op(self) -> None:
        self._refresh_highlight()
        if self._view is not None:
            self._view.refresh()

    def _wire_edit_clear(self) -> None:
        """Reset the wire-edit *entry* state: the in-progress two-vertex pick,
        the cyan overlay, and the cluster-cycle.

        The fragment edit itself now lives in :class:`WireEditTool`; SelectTool
        only owns the ``[E]`` entry (the whole-wire selection + the two-vertex
        range pick), so this clears the transient pick state — not any op."""
        from protoboard.controller.selection.hit_test import WireEditCycle

        self._clear_wire_edit_overlay()
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

    def _clear_wire_edit_overlay(self) -> None:
        self.preview.clear_wire_edit()

    # -- keys -----------------------------------------------------------------
    #
    # ``on_key`` (raw-key → command dispatch) lives on the GUI-composed
    # subclass in :mod:`protoboard.gui.tools.gui_tools` — it resolves keys
    # (KeyMap action ids, with headless-safe raw-key fallbacks) and calls the
    # command methods kept here: :meth:`_clear_pivot`, :meth:`_dispatch_intent`
    # (via ``_key_to_intent``), :meth:`_cycle_relation`,
    # :meth:`_delete_selection`, :meth:`_begin_edit_selected`, :meth:`begin_redrag`,
    # :meth:`arm_move`, :meth:`_flip_selected`, :meth:`arm_rotate`,
    # :meth:`exit_group`.  ``ControllerTool.on_key`` (do-nothing default from
    # :class:`~protoboard.controller.tools.base.ControllerTool`) covers the
    # headless/no-view case.

    def key_actions(self) -> dict[str, str | None]:
        """The keyboard actions this tool owns (namespaced; see module consts).

        ``None`` inherits the generic global default; a literal overrides for
        this tool.  Registered into the view's KeyMap on activate; the veto +
        on_key dispatch resolve against these by id.  The wire-edit op keys
        (reroute / autoroute / flip / simplify) live on
        :class:`~protoboard.controller.tools.wire_edit_tool.WireEditTool`'s own
        layer now, not here — SelectTool only owns the ``[E]`` entry.
        """
        return {
            _ACT_EDIT: None,        # global.action.edit  (default "E")
            _ACT_DELETE: None,      # global.action.delete (default "Del")
            _ACT_CANCEL: None,      # global.action.cancel (default "Esc")
            _ACT_ROTATE: "R",       # arm float-rotate (hands off to ArmedTransformTool)
            _ACT_MOVE: "G",         # arm keyboard float-move ("grab")
            _ACT_FLIP: None,        # global.action.flip (default "Alt+R") — immediate flip
        }

    def command_table(self) -> dict[str, Callable[[], object]]:
        """``action_id -> command`` — the data ``EditController.dispatch_tool_
        command`` runs (the View resolves a key to one of these ids via the
        KeyMap; a headless driver calls the id directly)."""
        return {
            _ACT_EDIT: self._begin_edit_selected,
            _ACT_DELETE: self._delete_selection,
            _ACT_CANCEL: self._wire_edit_clear,
            _ACT_ROTATE: self.arm_rotate,
            _ACT_MOVE: self.arm_move,
            _ACT_FLIP: self._flip_selected,
            _ACT_RELATION_CYCLE: self._cycle_relation,
            _ACT_RELATION_CYCLE_BACK: self._cycle_relation_back,
        }

    def _cycle_relation_back(self) -> None:
        self._cycle_relation(forward=False)

    def key_claims(self) -> tuple[tuple[str, Callable[[], bool]], ...]:
        """Contextual key claims (see :meth:`_Tool.wants_key` for the veto).

        Delete acts on the current selection — claimed so the global delete
        action doesn't shadow the tool's handler.  Pick-confirm / cancel / undo
        (Enter / Esc / Backspace) are raw, non-action keys that aren't global
        shortcuts, so they reach the tool anyway and need no claim here.  The
        wire-edit op-key claims live on the WireEditTool's own layer.
        """
        return (
            (_ACT_DELETE, self._has_selection),
        )

    def _has_selection(self) -> bool:
        return not self._selection.is_empty()

    def mouse_actions(self) -> dict[str, str | None]:
        """Mouse chords this tool layers over the view gestures.

        ``context_menu`` shadows the generic canvas menu with the unified item
        menu when the cursor hits something. (The armed transform's wheel-rotate
        lives on the ArmedTransformTool's own layer.)
        """
        return {
            _ACT_CONTEXT_MENU: "RMB",
        }

    def mouse_dispatch(self) -> tuple[
        tuple[str, Callable[[PointerInput | WheelInput], bool]],
        ...,
    ]:
        return (
            (_ACT_CONTEXT_MENU, self._mouse_context_menu),
        )

    def _mouse_context_menu(self, inp: PointerInput | WheelInput) -> bool:
        if not isinstance(inp, PointerInput):
            return False
        # The RMB chord resolves to the secondary_action verb (docs MVC step 1);
        # the View reconstructs the menu anchor from the scene point, so the
        # verb carries only (point, mods).  False on empty canvas → falls
        # through to the generic canvas menu.
        return self.secondary_action(inp.scene_pos, inp.mods)

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

    # -- armed transform entry ([R] rotate / [G] move) ----------------------

    @traced()
    def _invoke_armed_atomic(
        self,
        arm: "Callable[[], None]",
        action: "Callable[[ArmedTransformTool], None]",
    ) -> bool:
        """Run one armed action atomically: arm → action → accept.

        The programmatic form of the ``[arm, action, accept]`` shape (for menus
        / macros): *arm* enters the ArmedTransformTool (via ``set_tool``),
        *action* runs one operation on it (e.g. a 90° step, a flip), then we try
        to accept. Per the conditional-disarm contract, accept commits + returns
        to SelectTool **only when committable** — so if *action* left a conflict
        the ArmedTransformTool **stays active**, parked for the user to resolve
        (exactly like an interactive accept), and this returns ``False``.
        Returns ``True`` when it committed and returned to Select.

        No-op (returns ``False``) if *arm* failed to arm anything — e.g. an empty
        selection — so callers needn't pre-check.
        """
        from protoboard.controller.tools.armed_transform_tool import (
            ArmedTransformTool,
        )

        arm()
        armed = self._view.tools.active_tool
        if not isinstance(armed, ArmedTransformTool):
            return False  # arm declined (e.g. empty / non-transformable selection)
        action(armed)
        armed.accept()
        # A committable accept returned control here; a conflict left the
        # ArmedTransformTool active (parked).
        return self._view.tools.active_tool is self

    @traced()
    def arm_rotate(self) -> None:
        """Arm float-rotate on the selection — the ``[R]`` entry point.

        Resolves the selection to the rotate target, then hands off to the
        :class:`~protoboard.gui.tools.gui_tools.ArmedTransformTool`
        (which becomes the active tool and drives the controller's armed API):
        wheel / ``[↑]`` / ``[↓]`` / ``[R]`` step ±90°, a pad click sets a shared
        pivot, ``[Enter]`` accepts (parks in a conflict), ``[Esc]`` cancels. A
        legacy ``begin_rotate_around_point`` pivot capture seeds the pivot.
        """
        seed = self._rotate_pivot if isinstance(self._rotate_pivot, Point) else None
        self._arm_transform(
            "rotate",
            empty_msg="Rotate: select component(s) or a group first",
            none_msg="Rotate: nothing rotatable in the selection",
            pivot=seed,
        )

    def _arm_transform(
        self, mode: str, *, empty_msg: str, none_msg: str,
        pivot: Point | None = None,
    ) -> None:
        """Resolve the selection and switch to the ArmedTransformTool.

        Shared entry for ``[R]`` (rotate) and ``[G]`` (move): empty-selection /
        nothing-transformable early-returns keep parity with the old in-place
        arm; otherwise the resolved ``(comp_ids, group_id)`` snapshot + the
        active group are handed to the new tool, which arms the controller and
        returns control here on accept / cancel.
        """
        assert self._armed_transform_factory is not None  # set by activate() / injection

        if self._selection.is_empty():
            self._status(empty_msg)
            return
        comp_ids, group_id = self._rotatable_affected()
        if not comp_ids:
            self._status(none_msg)
            return
        self._cancel_gesture()  # drop any stray drag/pick first
        self._pivot_capture_pending = False
        self._view.tools.set_tool(
            self._armed_transform_factory(
                comp_ids, group_id,
                active_group_id=self._current_group_id,
                mode=mode,
                pivot=pivot,
                return_to=lambda: self._view.tools.set_tool(self),
            )
        )

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

    @traced()
    def arm_move(self) -> None:
        """Arm keyboard float-move on the selection — the ``[G]`` entry point.

        Resolves the selection and hands off to the ArmedTransformTool in its
        *move* sub-mode (arrows / WASD nudge one cell, ``[Enter]`` drops when
        committable, ``[Esc]`` cancels). Re-aiming ``[R]``/``[G]`` while armed
        is handled by the ArmedTransformTool itself (it is the active tool).
        """
        self._arm_transform(
            "move",
            empty_msg="Move: select component(s) or a group first",
            none_msg="Move: nothing movable in the selection",
        )

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
    # filter + crossing-wire cleanup, all done by ``EditController``);
    # ``_begin_rotate_around_point`` and ``_start_redrag`` are the
    # underscore-named aliases the menu/PPM call.

    def _rotate_multi(self, delta: int = 90) -> None:
        """Rotate the multi/loose selection (or selected group) via the controller.

        Target resolution (group vs loose) and the pivot are interaction state
        the tool owns; it hands them to :meth:`EditController.rotate` as explicit
        args.  The controller runs the unified pivot→group→loose dispatch and the
        crossing-wire cleanup; the tool keeps the presentation tail (status note,
        highlight refresh, panel re-broadcast).
        """
        pivot = self._rotate_pivot if isinstance(self._rotate_pivot, Point) else None
        removed = self._session.edit_controller.rotate(
            delta_deg=delta,
            pivot=pivot,
            active_group_id=self._current_group_id,
        )
        if removed:
            self._status_msg(f"Removed {removed} colliding wire(s) after move.")
        self._refresh_highlight()
        self._notify_selection()  # refresh Properties panel after multi-rotate

    def _flip_multi(self) -> None:
        """Flip the multi/loose selection (or selected group) via the controller.

        A selected group flips as a rigid body (with crossing-wire cleanup +
        highlight refresh, the flip analogue of ``_rotate_multi``); a loose
        selection flips its 2-pin components in place.  The controller builds the
        commands; the tool keeps the per-branch presentation tail that the legacy
        code applied (group: status + refresh + notify; loose: none — the plain
        ``layout_changed`` from the command execution suffices).
        """
        has_group = bool(self._selected_group_ids)
        removed = self._session.edit_controller.flip(
            active_group_id=self._current_group_id,
        )
        if has_group:
            if removed:
                self._status_msg(f"Removed {removed} colliding wire(s) after move.")
            self._refresh_highlight()
            self._notify_selection()

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

        Always ``select.idle``: the armed float-transform now lives on the
        :class:`~protoboard.gui.tools.gui_tools.ArmedTransformTool`,
        which is the active tool (and reports ``move.float.*``) while a transform
        is live — SelectTool is not active then.
        """
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
        from protoboard.controller.selection.queries import SessionConnQuery

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

        # Wire-pin conversion — the endpoint op now lives on the tool (not
        # MainWindow). Offer the relevant direction when it applies.
        if len(target_ids) == 1 and self._wire_has_wire_pin_endpoint(wire):
            out.append(ActionOption(
                action_id="select-tool.ctx.wire_pin_to_endpoint",
                label="Convert wire-pin to endpoint", group=group,
                data={},
            ))
        elif len(target_ids) == 1 and self._wire_endpoint_wire_pin_eligible(wire):
            out.append(ActionOption(
                action_id="select-tool.ctx.endpoint_to_wire_pin",
                label="Convert endpoint to wire-pin", group=group,
                data={},
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
        elif action == "endpoint_to_wire_pin":
            self.convert_endpoint_to_wire_pin()
        elif action == "wire_pin_to_endpoint":
            self.convert_wire_pin_to_endpoint()

    def _menu_set_wire_insulated(
        self, wire_ids: set[uuid.UUID], insulated: bool
    ) -> None:
        if not wire_ids:
            return
        self._session.edit_controller.set_wires_insulated(
            wire_ids, insulated=insulated
        )
        self._session.notify_changed()

    def _menu_set_wire_side(self, wire_ids: set[uuid.UUID], side: str) -> None:
        from typing import cast

        from protoboard.types import Side

        if not wire_ids:
            return
        self._session.edit_controller.set_wires_side(wire_ids, side=cast(Side, side))
        self._session.notify_changed()

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
        if not comp_ids:
            return
        self._session.edit_controller.set_locked(comp_ids, locked=locked)
        self._session.notify_changed()

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
        self._session.notify_changed()

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
            self._session.notify_changed()

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
        from protoboard.controller.selection.queries import SessionNetSource

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
        from protoboard.controller.highlight_entry import (
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

    def _endpoint_eligible_for_wire_pin(self, position: Point) -> bool:
        """True iff *position* sits on a plated-through pad (wire-pin invariant)."""
        for p in self._session.cached_plated_through_pads():
            if abs(p.x - position.x) < 1e-6 and abs(p.y - position.y) < 1e-6:
                return True
        return False

    def convert_endpoint_to_wire_pin(self) -> None:
        """Promote the half-orphan endpoint of the singly-selected wire to a wire-pin.

        The tool owns this wire operation (moved off ``MainWindow`` — the GUI is
        only an interface to the Controller). Endpoint side preference: ``b``
        (last placed) when both endpoints are eligible, ``a`` otherwise.
        """
        from typing import cast

        from protoboard.domain.commands.base import Command, ComponentState
        from protoboard.domain.commands.component_cmds import (
            ConvertEndpointToWirePinCommand,
        )
        from protoboard.domain.layout.knot import JointKnot

        wire_id = self._single_selected_wire_id()
        if wire_id is None:
            self._status_msg(
                "Convert endpoint to wire-pin: select one wire "
                "(right-click its endpoint for context menu)."
            )
            return
        wire = self._session.layout.wires.get(wire_id)
        if wire is None:
            return
        eligible: str | None = None
        for side in ("b", "a"):
            ref = wire.b if side == "b" else wire.a
            if ref is None:
                continue
            knot = self._session.layout.knots.get(ref.id)
            if isinstance(knot, JointKnot) and knot.wire_ids == frozenset({wire.id}):
                if self._endpoint_eligible_for_wire_pin(knot.position):
                    eligible = side
                    break
        if eligible is None:
            self._status_msg(
                "Convert endpoint to wire-pin: no eligible half-orphan endpoint "
                "on a plated-through pad."
            )
            return
        wire_net_id = self._session.cached_wire_net_map().get(wire.id)
        cmd = cast(
            "Command[ComponentState]",
            ConvertEndpointToWirePinCommand(
                wire_id=wire.id, endpoint=eligible, net_id=wire_net_id,
            ),
        )
        self._execute_command(cmd)
        self._refresh_after_wire_op()

    def convert_wire_pin_to_endpoint(self) -> None:
        """Demote every ``WirePinKnot`` owned by the selected wires to a free endpoint."""
        from typing import cast

        from protoboard.domain.commands.base import (
            Command,
            ComponentState,
            MacroCommand,
        )
        from protoboard.domain.commands.component_cmds import (
            ConvertWirePinToEndpointCommand,
        )
        from protoboard.domain.layout.knot import WirePinKnot

        sel = self._selected_by_kind("wire")
        if not sel:
            self._status_msg(
                "Convert wire-pin to endpoint: select one or more wires that "
                "own a wire-pin endpoint."
            )
            return
        targets: list[uuid.UUID] = []
        for wire_id in sel:
            wire = self._session.layout.wires.get(wire_id)
            if wire is None:
                continue
            for ref in (wire.a, wire.b):
                if ref is None:
                    continue
                knot = self._session.layout.knots.get(ref.id)
                if isinstance(knot, WirePinKnot) and knot.id not in targets:
                    targets.append(knot.id)
        if not targets:
            self._status_msg(
                "Convert wire-pin to endpoint: selected wires have no wire-pin "
                "endpoints."
            )
            return
        cmds = [
            cast(
                "Command[ComponentState]",
                ConvertWirePinToEndpointCommand(wire_pin_id=wp_id),
            )
            for wp_id in targets
        ]
        cmd = (
            cmds[0] if len(cmds) == 1
            else cast(
                "Command[ComponentState]",
                MacroCommand(cmds, description="Convert wire-pins to endpoints"),
            )
        )
        self._execute_command(cmd)
        self._refresh_after_wire_op()

    def _single_selected_wire_id(self) -> uuid.UUID | None:
        """The one selected wire id, or ``None`` when zero or many are selected."""
        sel = self._selected_by_kind("wire")
        return next(iter(sel)) if len(sel) == 1 else None

    def _wire_has_wire_pin_endpoint(self, wire: Wire | None) -> bool:
        """True when *wire* owns a ``WirePinKnot`` at either endpoint."""
        from protoboard.domain.layout.knot import WirePinKnot

        if wire is None:
            return False
        for ref in (wire.a, wire.b):
            if ref is None:
                continue
            if isinstance(self._session.layout.knots.get(ref.id), WirePinKnot):
                return True
        return False

    def _wire_endpoint_wire_pin_eligible(self, wire: Wire | None) -> bool:
        """True when *wire* has a half-orphan endpoint on a plated-through pad."""
        from protoboard.domain.layout.knot import JointKnot

        if wire is None:
            return False
        for ref in (wire.b, wire.a):
            if ref is None:
                continue
            knot = self._session.layout.knots.get(ref.id)
            if (
                isinstance(knot, JointKnot)
                and knot.wire_ids == frozenset({wire.id})
                and self._endpoint_eligible_for_wire_pin(knot.position)
            ):
                return True
        return False

    def _move_selected_wires_to_side(self, target_side: str) -> None:
        """Flatten all selected whole wires onto *target_side* (explicit Move).

        "Wire: Move to Top/Bottom Layer". The per-fragment toggle flip is
        :meth:`_flip_selected_wires`; the per-fragment [F] op is WireEditTool's.
        """
        from typing import cast

        from protoboard.types import Side

        wire_ids = self._selected_by_kind("wire")
        if not wire_ids:
            return
        self._session.edit_controller.set_wires_side(
            wire_ids, side=cast(Side, target_side)
        )
        self._session.notify_changed()
        self._status_msg(
            f"Moved {len(wire_ids)} wire(s) to {target_side} layer."
        )

    def _flip_selected_wires(self) -> None:
        """Flip all selected whole wires by toggling each fragment's side.

        Mixed wires stay mixed (inverted) — no flatten, no "different layers"
        refusal (the old stale-wire.side guard is gone; see cd01742).
        """
        wire_ids = self._selected_by_kind("wire")
        if not wire_ids:
            return
        self._session.edit_controller.flip_wires(wire_ids)
        self._session.notify_changed()
        self._status_msg(f"Flipped {len(wire_ids)} wire(s).")

    # -- group creation dialog (Ctrl+G) -------------------------------------

    def _select_group(self, group_id: uuid.UUID) -> None:
        """Select a single group as a monolith (post-create / post-merge)."""
        self._set_selection_from_ids(set(), set(), {group_id})

    def _group_for_selection(self) -> uuid.UUID | None:
        """Group the current selection: dialog to create new or add to existing.

        The tool owns the interaction shell — the target-picker dialog
        (``_group_prompt``) and the select-the-result side effect — and hands the
        actual promotion / add-to-existing to :class:`EditController`.
        """
        ids = frozenset(self._selected_ids)
        sel_id = self._selected_id
        if sel_id and sel_id not in ids:
            ids = ids | {sel_id}
        if not ids:
            return None

        from protoboard.controller.tools.select_prompts import GroupTargetRequest

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

        item_ids = [ItemId.component(cid) for cid in ids]
        controller = self._session.edit_controller
        if choice.mode == "new":
            new_id = controller.create_group(
                item_ids,
                active_group_id=active_gid,
                name=choice.new_name,
                auto_export=choice.auto_export_external,
            )
            if new_id is not None:
                self._select_group(new_id)
            return new_id

        # Add to an existing group.
        target_id = choice.target_group_id
        if target_id is None:
            return None
        controller.add_to_group(
            target_id,
            item_ids,
            active_group_id=active_gid,
            auto_export=choice.auto_export_external,
        )
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
        from typing import cast

        from protoboard.controller.ports import BlockingJob
        from protoboard.controller.selection.wire_ops import build_simplify_inputs
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

        from protoboard.controller.selection.wire_edit import replace_wire_path_cmd

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
        from protoboard.controller.ports import BlockingJob
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
        self._session.notify_changed()
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
        self._session.notify_changed()
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
