# SPDX-License-Identifier: MIT
"""``WireEditTool`` — wire-fragment edit mode as its own registered tool.

Entered from :class:`~protoboard.gui.tools.gui_tools.SelectTool` on ``[E]``
(which resolves the selected wire to a :class:`WireRange` and swaps to this
tool).  Unlike :class:`~protoboard.controller.tools.armed_transform_tool.
ArmedTransformTool` — which re-enters a *controller transform session* — a
wire-edit op mints a domain :class:`Command` and executes it **locally**
(``_execute_command`` + ``_refresh_after_wire_op``), delegating only the
command-*building* half to Task-7's :class:`~protoboard.controller.editing.
wire_edit_arm.WireEditArm`.

STAY-ARMED semantics (see ``docs/superpowers/specs`` armed-subtool design):

* ``flip`` / ``simplify`` / ``reroute`` / ``autoroute`` preserve the wire id, so
  after committing the tool RE-ARMS on the **full span** of the now-mutated wire
  (the old fragment indices may be stale after a path change) and stays active.
* ``delete`` removes the wire — nothing to re-arm — so it EXITS via the injected
  ``return_to`` (drop back to plain select).  The only op that exits on commit.
* ``has_pending_op()`` is True ONLY while a reroute waypoint sub-pick is live
  (``self._mouse is not None``); a bare armed fragment with no live pick is
  exitable (first ``[Esc]`` exits).  ``can_exit() = not has_pending_op()``.
* ``commit_op()`` confirms the live reroute pick (Enter); ``cancel_op()`` drops
  its waypoints but keeps the fragment armed.

The reroute waypoint pick reuses the same pick machinery SelectTool has: a
per-session :class:`~protoboard.controller.selection.service.SelectionService`
mints a :class:`~protoboard.controller.selection.point_pick_controller.
PointPickController`, parked as ``self._mouse`` so board clicks / intents route
through it exactly as they do in SelectTool.
"""

from __future__ import annotations

import uuid
from collections.abc import Callable
from typing import TYPE_CHECKING, cast

from protoboard.controller.tools.base import ControllerTool
from protoboard.instrumentation.debug_trace import traced

if TYPE_CHECKING:
    from protoboard.controller.editing import EditingContext
    from protoboard.controller.editing.wire_edit_arm import WireEditArm
    from protoboard.controller.ports import HoverEvent, PointerInput
    from protoboard.controller.selection.adapters import WireVertexSelectable
    from protoboard.controller.selection.hit_test import WireEditCycle
    from protoboard.controller.selection.mouse_input import (
        Intent,
        Mods,
        MouseInputController,
    )
    from protoboard.controller.selection.selectable import Selectable
    from protoboard.controller.selection.service import SelectionService
    from protoboard.controller.selection.wire_edit import WireRange, WireRangeEdit
    from protoboard.controller.tools.base import _ViewProto
    from protoboard.controller.tools.select_preview import SelectPreview
    from protoboard.domain.commands.base import Command, ComponentState
    from protoboard.domain.layout.wire import Wire
    from protoboard.geometry import Point


# Own action namespace — the wire-edit op keys.  Copied verbatim from
# select_tool.py (lines 98-119) so the SAME ids drive both tools while SelectTool
# still owns wire-edit (8a); 8b repoints the call site.
_ACT_REROUTE = "select-tool.action.reroute"
_ACT_AUTOROUTE = "select-tool.action.autoroute"
_ACT_DELETE = "select-tool.action.delete"
_ACT_CANCEL = "select-tool.action.cancel"
_ACT_FLIP_FRAGMENT = "select-tool.action.flip_fragment"
_ACT_SIMPLIFY_FRAGMENT = "select-tool.action.simplify_fragment"
_ACT_SET_INSULATED_FRAGMENT = "select-tool.action.set_insulated_fragment"


class WireEditTool(ControllerTool):
    """The wire-fragment edit mode, driven as a registered tool."""

    def __init__(
        self,
        wire_range: WireRange,
        wire_id: uuid.UUID,
        *,
        return_to: Callable[[], None],
    ) -> None:
        from protoboard.controller.selection.hit_test import WireEditCycle

        super().__init__()  # sets _session / _view to None up-front
        self._return_to = return_to
        self._arm: WireEditArm | None = None
        self._preview: SelectPreview | None = None
        # The selection service driving the reroute pick + its live controller.
        self._selection: SelectionService = None  # type: ignore[assignment]
        self._mouse: MouseInputController | None = None
        # The armed fragment (WireRange) + cluster-cycle + anchor wire.  The
        # cross-wire ``_wire_edit_start`` first-vertex is only used by the
        # net-restricted vertex hit-test (a bare full-span arm has none).
        self._wire_edit: WireRange | None = wire_range
        self._wire_edit_wire_id: uuid.UUID | None = wire_id
        self._wire_edit_cycle: WireEditCycle = WireEditCycle()
        self._wire_edit_start: WireVertexSelectable | None = None
        # True while, after a sub-range cut, the tool is disarmed and waiting
        # for the user to pick a fresh 2-vertex range (or [Esc] to leave).
        self._awaiting_repick: bool = False
        # Guards the transient board-dim highlight: pushed once on activate(),
        # popped once on _exit() -- see _push_dim/_pop_dim.
        self._dim_active = False

    # -- lifecycle ----------------------------------------------------------

    @traced()
    def activate(self, session: EditingContext, view: object) -> None:
        from protoboard.controller.editing.wire_edit_arm import WireEditArm
        from protoboard.controller.selection.service import SelectionService

        self._session = session
        # Narrow the object-typed seam param to the controller view surface.
        view = cast("_ViewProto", view)
        self._view = view
        # Reuse the per-session SelectionService (SelectTool parks one) so a
        # reroute pick shares the same pick registry; create + park if absent.
        svc = getattr(session, "selection_service", None)
        if not isinstance(svc, SelectionService):
            svc = SelectionService()
            session.selection_service = svc
        self._selection = svc
        self._arm = WireEditArm(
            cast(
                "Callable[[Wire], Command[ComponentState]]",
                self._net_aware_add_factory(),
            ),
            find_paths=session.edit_controller.find_paths,
            simplify_wire=session.edit_controller.simplify_wire,
        )
        # Fragment ops (flip / simplify / sub-range delete) issue a
        # ``replace_wire_fragments`` edit, which requires both endpoint knots.
        # A wire loaded from a saved project / built directly into
        # ``layout.wires`` (never through ``add_wire``) has none, so backfill
        # them once on arm -- matching the established pattern in
        # ``simplify_tool`` and ``replace_wire_path_cmd`` (reroute).  Idempotent:
        # a no-op when both knots are already set.
        state = self._wire_edit
        if state is not None:
            for wid in {state.start_wire_id, state.end_wire_id}:
                wire = self._wire_by_id(wid)
                if wire is not None:
                    session.layout.ensure_wire_knots(wire)
        self._push_dim()
        self._draw_wire_edit_overlay()
        self._status(
            "Fragment selected  ·  [W] Reroute  ·  [A] Autoroute  ·  "
            "[F] Flip layer  ·  [S] Simplify  ·  [I] Insulate/bare  ·  "
            "[Del] Delete  ·  [Esc] Cancel",
            0,
        )

    @traced()
    def _push_dim(self) -> None:
        """Dim the whole board except the edited wire (transient highlight)."""
        if self._dim_active:
            return
        wid = self._wire_edit_wire_id
        if wid is None or self._session is None:
            return
        from protoboard.controller.highlight_entry import (
            PROFILE_DIMMED_2,
            HighlightEntry,
        )

        self._session.highlight_stack.push_transient(
            HighlightEntry(
                wire_ids=frozenset({wid}),
                profile=PROFILE_DIMMED_2,
                transient=True,
                label="wire-edit",
            )
        )
        self._dim_active = True

    def _pop_dim(self) -> None:
        if not self._dim_active:
            return
        if self._session is not None:
            self._session.highlight_stack.pop_transient()
        self._dim_active = False

    def deactivate(self) -> None:
        self._pop_dim()  # ToolHost.set_tool bypasses _exit(); restore brightness.
        self._wire_edit_clear()
        self._session = None  # type: ignore[assignment]
        self._view = None
        self._arm = None

    def on_scene_cleared(self) -> None:
        if self._preview is not None:
            self._preview.on_scene_cleared()

    @property
    def preview(self) -> SelectPreview:
        assert self._preview is not None  # set by activate() / injection
        return self._preview

    @property
    def state(self) -> str:
        return "wire_edit" if self._wire_edit is not None else "select.idle"

    # -- commands -----------------------------------------------------------

    def command_table(self) -> dict[str, Callable[[], object]]:
        """``action_id -> command`` — the wire-edit op-table the View resolves
        keys against (mirrors ``_wire_edit_on_key`` in SelectTool)."""
        return {
            _ACT_REROUTE: self._begin_reroute,
            _ACT_AUTOROUTE: self._autoroute_fragment,
            _ACT_FLIP_FRAGMENT: self._flip_wire_fragment,
            _ACT_SIMPLIFY_FRAGMENT: self._simplify_wire_fragment,
            _ACT_SET_INSULATED_FRAGMENT: self._set_insulated_fragment,
            _ACT_DELETE: self._delete_fragment,
            _ACT_CANCEL: self.cancel_op,
        }

    def displayable_action_ids(self) -> list[str]:
        return [
            _ACT_REROUTE,
            _ACT_AUTOROUTE,
            _ACT_FLIP_FRAGMENT,
            _ACT_SIMPLIFY_FRAGMENT,
            _ACT_SET_INSULATED_FRAGMENT,
            _ACT_DELETE,
        ]

    # -- ArmedSubTool surface ----------------------------------------------

    def has_pending_op(self) -> bool:
        # Pending == a live reroute waypoint pick (a REAL in-flight op), NOT
        # mere armedness — a bare armed fragment is exitable on the first Esc.
        mouse = self._mouse
        return mouse is not None and not mouse.finished

    def can_commit(self) -> bool:
        # A wire-edit op has no HARD-conflict park concept (the net-restricted
        # hit-test keeps a fragment within one net) — a commit always lands.
        return True

    def can_exit(self) -> bool:
        return not self.has_pending_op()

    def commit_op(self) -> bool:
        """Confirm ([Enter]) the live reroute pick, if one is in flight."""
        return self._dispatch_intent_confirm()

    def cancel_op(self) -> bool:
        """Esc: drop a live reroute pick's waypoints (keep the fragment); with
        no pick in flight there is nothing to cancel."""
        if self.has_pending_op():
            self._dispatch_intent_cancel()
            return True
        return False

    # -- reroute waypoint pick ---------------------------------------------

    def _begin_reroute(self) -> None:
        """Arm the waypoint pick — ``[W]`` reroute (a chained ``point`` pick).

        Collects snapped board waypoints; ``on_progress`` previews the dashed
        orange polyline, ``on_preview`` draws the live cursor segment, Enter
        commits (``_commit_reroute``), Esc keeps the fragment.
        """
        from protoboard.controller.selection.selectable import Stop

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
        from protoboard.controller.selection.adapters import PointSelectable

        pts = [p.point for p in picked if isinstance(p, PointSelectable)]
        self._draw_reroute_preview(pts)

    def _reroute_preview(self, scene_pt: Point, _mods: Mods) -> None:
        """Live rubber-band from the last anchor to the cursor (snapped)."""
        self._clear_reroute_live()
        anchor = self._reroute_anchor()
        if anchor is None:
            return
        snapped = self._snap(scene_pt)
        self.preview.show_reroute_live(anchor, snapped)

    def _reroute_anchor(self) -> Point | None:
        """The point a new reroute segment starts from (last waypoint or start).

        Single source of truth — the live ``PickSession``'s collected waypoints
        (``state.waypoints`` is only populated at commit time, so reading it
        here would make every live segment start from the fragment start).
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
        """The last confirmed waypoint from the active pick, or None."""
        from protoboard.controller.selection.adapters import PointSelectable

        mouse = self._mouse
        session = getattr(mouse, "session", None)
        if session is None:
            return None
        pts = [p.point for p in session.collected if isinstance(p, PointSelectable)]
        return pts[-1] if pts else None

    def _commit_reroute(self, picked: list[Selectable]) -> None:
        """Commit the reroute: build the path command, execute, RE-ARM."""
        from protoboard.controller.selection.adapters import PointSelectable

        state = self._wire_edit
        arm = self._arm
        if state is None or arm is None:
            self._reroute_cancel()
            return
        pts = [p.point for p in picked if isinstance(p, PointSelectable)]
        cmd = arm.reroute(
            self._session.layout,
            list(self._session.layout.wires),
            self._wire_net_map(),
            state.with_waypoints(pts),
        )
        self._commit_and_rearm(cmd)

    def _reroute_cancel(self) -> None:
        """Esc during reroute → drop the waypoints, keep the fragment armed."""
        self._clear_reroute_overlay()
        if self._wire_edit is not None:
            self._status(
                "Fragment selected  ·  [W] Reroute  ·  [A] Autoroute  ·  "
                "[F] Flip layer  ·  [S] Simplify  ·  [I] Insulate/bare  ·  "
                "[Del] Delete  ·  [Esc] Cancel",
                0,
            )

    # -- ops (build via arm, execute locally, re-arm or exit) --------------

    def _autoroute_fragment(self) -> None:
        """A* re-route the fragment — ``[A]`` (stays armed)."""
        state = self._wire_edit
        arm = self._arm
        if state is None or arm is None:
            return
        source, target, side = self._fragment_endpoints(state)
        if source is None or target is None:
            self._exit()
            return
        cmd = arm.autoroute(
            self._session.layout,
            list(self._session.layout.wires),
            self._wire_net_map(),
            state,
            self._session.connectivity_graph(),
            source,
            target,
            side,
        )
        if cmd is None:
            self._status("Autoroute: no path found")
            self._refresh_after_wire_op()
            return
        self._commit_and_rearm(cmd)

    @traced()
    def _flip_wire_fragment(self, target_side: str | None = None) -> None:
        """Flip the selected fragment to the opposite layer — ``[F]`` (stays armed)."""
        state = self._wire_edit
        arm = self._arm
        if state is None or arm is None:
            return
        cmd = arm.flip(list(self._session.layout.wires), state, target_side)
        if cmd is None:
            self._status("Flip: pick a fragment on a single wire (two vertices apart).")
            return
        # Flip preserves the wire's vertex count, so the picked sub-range stays
        # valid: keep it armed (highlight + a repeated [F] target the SAME
        # fragment) instead of growing to the whole wire.
        self._commit_and_rearm(cmd, preserve_range=True)

    def _simplify_wire_fragment(self) -> None:
        """Simplify the selected fragment — ``[S]`` (a range op; stays armed)."""
        from protoboard.controller.selection.wire_ops import build_simplify_inputs
        from protoboard.domain.routing.simplify_mode import SimplifyMode

        state = self._wire_edit
        arm = self._arm
        if state is None or arm is None or state.is_cross:
            self._status(
                "Simplify: pick a fragment on a single wire (two vertices apart)."
            )
            return
        mode = getattr(
            self._session.routing_settings, "simplify_mode", SimplifyMode.BARE_PARITY
        )
        cmd = arm.simplify(
            self._session.layout,
            list(self._session.layout.wires),
            state,
            build_simplify_inputs(self._session),
            mode,
        )
        if cmd is None:
            self._status("Simplify: nothing to shorten on this fragment.")
            self._refresh_after_wire_op()
            return
        self._commit_and_rearm(cmd)

    def _delete_fragment(self) -> None:
        """Delete the selected fragment — ``[Del]``.

        Full-span → whole wire gone → exit.  Sub-range → the two halves
        survive → wait for a fresh range pick (unified with set-insulated)."""
        state = self._wire_edit
        arm = self._arm
        if state is None or arm is None:
            return
        edit = arm.delete_range(
            list(self._session.layout.wires), self._wire_net_map(), state
        )
        self._apply_range_edit(edit, "Delete: nothing to remove.")

    def _set_insulated_fragment(self) -> None:
        """Toggle the selected range's insulation — ``[I]``.

        Reads the anchored wire's current ``insulated`` and targets the
        opposite, so a single key flips bare<->insulated for the slice.
        Full-span → id-preserving toggle (stays armed).  Sub-range → split
        into bare/insulated/bare and wait for a re-pick."""
        state = self._wire_edit
        arm = self._arm
        if state is None or arm is None:
            return
        wire = self._wire_by_id(state.start_wire_id)
        if wire is None:
            return
        edit = arm.set_insulated(
            list(self._session.layout.wires),
            self._wire_net_map(),
            state,
            insulated=not wire.insulated,
        )
        self._apply_range_edit(
            edit, "Insulate: pick a range within one wire (two vertices apart)."
        )

    # -- commit / re-arm / exit --------------------------------------------

    def _apply_range_edit(self, edit: WireRangeEdit | None, empty_msg: str) -> None:
        """Execute a range edit and choose the post-op behavior.

        No edit (refused / no-op) → status hint, stay as-is.  Not a cut
        (full-span, id-preserving) → re-arm on the same wire.  A cut with
        survivors → wait for a re-pick.  A cut that removed the whole wire
        (no survivors) → exit.
        """
        if edit is None:
            self._status(empty_msg)
            self._refresh_after_wire_op()
            return
        self._execute_command(edit.command)
        self._refresh_after_wire_op()
        if not edit.was_cut:
            self._rearm_full_span()
        elif edit.survivor_wire_ids:
            self._arm_repick(edit.survivor_wire_ids)
        else:
            self._exit()

    def _arm_repick(self, survivor_wire_ids: tuple[uuid.UUID, ...]) -> None:
        """Disarm the fragment and wait for a fresh 2-vertex range pick."""
        from protoboard.controller.selection.hit_test import WireEditCycle
        from protoboard.controller.selection.selectable import Stop

        # Reassign the anchor to a surviving wire that still exists (the one
        # field whose set-once-in-__init__ assumption we deliberately relax).
        anchor = next(
            (wid for wid in survivor_wire_ids if self._wire_by_id(wid) is not None),
            None,
        )
        if anchor is None:
            self._exit()
            return
        self._clear_wire_edit_overlay()
        self._wire_edit = None
        self._wire_edit_wire_id = anchor
        self._wire_edit_cycle = WireEditCycle()
        self._wire_edit_start = None
        self._awaiting_repick = True
        self._status(
            "Wire cut  ·  pick two vertices to select a new range  ·  [Esc] Done",
            0,
        )
        self._arm_pick(
            "wire-vertex",
            Stop.count(2),
            on_done=self._range_repicked,
        )

    def _range_repicked(self, picked: list[Selectable]) -> None:
        """Resolve two picked vertices into a new armed range (port of
        SelectTool._range_picked): same-wire swap to start<=end, zero-length
        abort; cross-wire kept as-is.

        A failed resolution (<2 vertices, or a zero-length same-vertex pick)
        leaves the tool ``_awaiting_repick`` with no live pick (the ``Stop.
        count(2)`` pick that got us here has already finished) — re-arm a
        fresh pick so the wait loop stays live instead of stalling until Esc.
        """
        from protoboard.controller.selection.adapters import WireVertexSelectable
        from protoboard.controller.selection.wire_edit import WireRange

        verts = [p for p in picked if isinstance(p, WireVertexSelectable)]
        if len(verts) < 2:
            self._rearm_repick_if_waiting()
            return  # keep waiting; the pick above stays live
        v0, v1 = verts[0], verts[1]
        start_wire, start_idx = v0.wire_id, v0.index
        end_wire, end_idx = v1.wire_id, v1.index
        if start_wire == end_wire:
            if start_idx == end_idx:
                self._rearm_repick_if_waiting()
                return  # zero-length, ignore
            if end_idx < start_idx:
                start_idx, end_idx = end_idx, start_idx
        self._awaiting_repick = False
        self._wire_edit = WireRange(start_wire, start_idx, end_wire, end_idx)
        self._wire_edit_wire_id = start_wire
        self._draw_wire_edit_overlay()
        self._status(
            "Fragment selected  ·  [W] Reroute  ·  [A] Autoroute  ·  "
            "[F] Flip layer  ·  [S] Simplify  ·  [I] Insulate/bare  ·  "
            "[Del] Delete  ·  [Esc] Cancel",
            0,
        )

    def _rearm_repick_if_waiting(self) -> None:
        """Re-arm the 2-vertex re-pick if a failed resolution left the tool
        ``_awaiting_repick`` with no live pick (see ``_range_repicked``)."""
        from protoboard.controller.selection.selectable import Stop

        if self._awaiting_repick:
            self._arm_pick(
                "wire-vertex",
                Stop.count(2),
                on_done=self._range_repicked,
            )

    def _commit_and_rearm(
        self, cmd: Command[ComponentState] | None, *, preserve_range: bool = False
    ) -> None:
        """Execute *cmd* (if any), refresh, then re-arm so the tool stays active.

        ``preserve_range=True`` keeps the picked sub-range armed instead of
        growing it to the wire's full span.  Only safe for **geometry-preserving**
        ops (flip): they keep the wire's vertex count, so the picked
        ``[start_idx, end_idx]`` stays valid and the user's fragment selection
        (and its highlight) survives the op.  Geometry-*changing* ops
        (reroute / autoroute / simplify) must re-arm the full span, since their
        path edits invalidate the old indices.
        """
        if cmd is not None:
            self._execute_command(cmd)
        self._refresh_after_wire_op()
        if preserve_range:
            self._rearm_same_range()
        else:
            self._rearm_full_span()

    def _rearm_same_range(self) -> None:
        """Re-arm on the CURRENTLY-picked sub-range (geometry-preserving ops).

        The wire's vertex count is unchanged, so ``self._wire_edit`` stays valid;
        just refresh the overlay + status so the same fragment stays highlighted
        and armed.  If the wire vanished (shouldn't for id-preserving ops) or no
        range is armed, fall back to the full-span re-arm.
        """
        wire_id = self._wire_edit_wire_id
        wire = self._wire_by_id(wire_id) if wire_id is not None else None
        if wire is None or self._wire_edit is None:
            self._rearm_full_span()
            return
        self._draw_wire_edit_overlay()
        self._status(
            "Fragment selected  ·  [W] Reroute  ·  [A] Autoroute  ·  "
            "[F] Flip layer  ·  [S] Simplify  ·  [I] Insulate/bare  ·  "
            "[Del] Delete  ·  [Esc] Cancel",
            0,
        )

    def _rearm_full_span(self) -> None:
        """Re-arm on the FULL SPAN of the (now-mutated) wire.

        The old fragment indices may be stale after a path change; the full span
        of the surviving wire is the safe, always-selectable re-arm.  If the wire
        vanished (shouldn't happen for id-preserving ops), exit instead.
        """
        from protoboard.controller.selection.hit_test import WireEditCycle
        from protoboard.controller.selection.wire_edit import WireRange

        wire_id = self._wire_edit_wire_id
        wire = self._wire_by_id(wire_id) if wire_id is not None else None
        if wire is None:
            self._exit()
            return
        self._wire_edit = WireRange.full_span(wire)
        self._wire_edit_cycle = WireEditCycle()
        self._wire_edit_start = None
        self._draw_wire_edit_overlay()
        self._status(
            "Fragment selected  ·  [W] Reroute  ·  [A] Autoroute  ·  "
            "[F] Flip layer  ·  [S] Simplify  ·  [I] Insulate/bare  ·  "
            "[Del] Delete  ·  [Esc] Cancel",
            0,
        )

    def _exit(self) -> None:
        """Drop wire-edit and hand control back to the SelectTool that armed us."""
        self._pop_dim()
        self._wire_edit_clear()
        self._return_to()

    def _wire_edit_clear(self) -> None:
        """Leave wire-edit: drop the fragment, cycle, overlays."""
        from protoboard.controller.selection.hit_test import WireEditCycle

        self._clear_wire_edit_overlay()
        self._clear_reroute_overlay()
        self._wire_edit = None
        self._wire_edit_wire_id = None
        self._wire_edit_start = None
        self._wire_edit_cycle = WireEditCycle()
        self._awaiting_repick = False

    # -- pick machinery (replicated from SelectTool — reroute needs it) -----

    def on_press(self, inp: PointerInput) -> bool:
        from protoboard.controller.ports import LEFT_BUTTON

        if inp.button != LEFT_BUTTON:
            return False
        mouse = self._mouse
        if mouse is not None and not mouse.finished:
            mouse.on_press(inp.scene_pos, inp.mods)
            return True
        return False

    def on_move(self, inp: PointerInput) -> None:
        if self._mouse is not None:
            self._mouse.on_move(inp.scene_pos, inp.mods)

    def empty_action(self, point: Point, mods: Mods) -> HoverEvent:
        from protoboard.controller.ports import HoverEvent

        if self._mouse is not None:
            self._mouse.on_move(point, mods)
        return HoverEvent(point=point)

    def on_release(self, inp: PointerInput) -> None:
        mouse = self._mouse
        if mouse is None:
            return
        mouse.on_click(inp.scene_pos, inp.mods)
        # on_click may run an on_done callback that re-arms a NEW pick
        # (the re-pick wait fold-in). Only clear if THIS finished pick is
        # still the current one — else we'd null the freshly-armed pick.
        if mouse.finished and self._mouse is mouse:
            self._mouse = None

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
        """Start a ``point`` pick and park it as the active mouse interaction.

        Builds a ``PickSession`` via the service, wraps it in a
        ``PointPickController``, and routes clicks / intents through
        ``self._mouse`` (the reroute waypoint pick).  The resolver is this tool's
        :meth:`_pick_at`.  Mirrors ``SelectTool._arm_pick`` (only the surface
        wire-edit needs — no custom ``resolve``).
        """
        from collections.abc import Callable as _Callable

        from protoboard.controller.selection.point_pick_controller import (
            PointPickController,
        )
        from protoboard.controller.selection.selectable import PickKind, Stop

        if self._mouse is not None and not self._mouse.finished:
            self._dispatch_intent_cancel()
        kind = cast("PickKind", what)
        session = self._selection.request_pick(
            kind,
            cast("Stop", stop),
            on_done=cast("_Callable[[list[Selectable]], None]", on_done),
            on_cancel=cast("_Callable[[], None] | None", on_cancel),
            on_progress=cast("_Callable[[list[Selectable]], None] | None", on_progress),
        )

        def _resolve(pt: Point, _mods: Mods) -> Selectable | None:
            return self._pick_at(kind, pt)

        self._mouse = PointPickController(
            session,
            _resolve,
            on_preview=cast("_Callable[[Point, Mods], None] | None", on_preview),
        )

    def _pick_at(self, what: object, scene_pt: Point) -> Selectable | None:
        """Resolve a click into the Selectable the active pick wants.

        A ``point`` pick resolves to the snapped board coordinate; a
        ``wire-vertex`` pick runs the net-restricted cluster-cycling vertex
        hit-test (threading ``self._wire_edit_cycle`` so repeated clicks at a
        shared knot rotate through siblings).  Mirrors the reroute-relevant
        branches of ``SelectTool._pick_at``.
        """
        if what in ("pad", "point"):
            from protoboard.controller.selection.adapters import PointSelectable

            return PointSelectable(point=self._snap(scene_pt))
        if what == "wire-vertex":
            from protoboard.controller.selection.hit_test import HitTester
            from protoboard.types import Side

            camera_side = getattr(self._view, "camera_side", "top")
            hitter = HitTester(self._session, camera_side=cast("Side", camera_side))
            sel, cycle = hitter.hit_wire_vertex(
                scene_pt.x,
                scene_pt.y,
                start_wire_id=self._wire_edit_active_start_wire(),
                wire_net_map=self._session.cached_wire_net_map(),
                cycle=self._wire_edit_cycle,
            )
            self._wire_edit_cycle = cycle
            return sel
        return None

    def _wire_edit_active_start_wire(self) -> uuid.UUID | None:
        """The wire the wire-vertex pick is anchored on for tie-breaking."""
        start = self._wire_edit_start
        if start is not None:
            return start.wire_id
        return self._wire_edit_wire_id

    def _dispatch_intent_confirm(self) -> bool:
        from protoboard.controller.selection.mouse_input import Intent

        return self._dispatch_intent(Intent.CONFIRM)

    def _dispatch_intent_cancel(self) -> bool:
        from protoboard.controller.selection.mouse_input import Intent

        return self._dispatch_intent(Intent.CANCEL)

    def _dispatch_intent(self, intent: Intent) -> bool:
        """Route a semantic intent to the active reroute pick; return consumed."""
        mouse = self._mouse
        if mouse is None:
            return False
        if not mouse.on_event(intent):
            return False
        if mouse.finished:
            self._mouse = None
        return True

    # -- helpers ------------------------------------------------------------

    def _net_aware_add_factory(self) -> object:
        """A wire-range ``add_factory`` that reclassifies nets on cut/reroute.

        The Qt-free range builders mint fresh wires when a cut re-adds a
        surviving / replacement piece.  This factory wraps each fresh wire in an
        ``AddWireWithNetsCommand`` and runs ``resolve_wire_nets`` against the
        live session, so a piece whose new endpoint lands on a pin re-extends /
        re-merges the schematic instead of keeping the stale net.  (Copied from
        ``SelectTool._net_aware_add_factory``.)
        """
        from protoboard.controller.editing.wire_nets import resolve_wire_nets
        from protoboard.domain.commands.component_cmds import AddWireWithNetsCommand
        from protoboard.domain.layout.wire import Wire as _Wire

        def _factory(wire: _Wire) -> Command[ComponentState]:
            cmd = AddWireWithNetsCommand(wire=wire)
            resolve_wire_nets(
                self._session,
                wire,
                cmd,
                merge_prompt=lambda net_a, _net_b: net_a.name,
            )
            return cast("Command[ComponentState]", cmd)

        return _factory

    def _fragment_endpoints(
        self, state: WireRange
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
        return self._session.layout.wires.get(wire_id)

    def _wire_net_map(self) -> dict[uuid.UUID, uuid.UUID]:
        return self._session.cached_wire_net_map()

    def _refresh_after_wire_op(self) -> None:
        if self._view is not None:
            self._view.refresh()

    def _execute_command(self, cmd: object) -> bool:
        return self._session.edit_controller.execute_command(cmd)

    def _snap(self, raw: Point) -> Point:
        assert self._view is not None
        return self._view.snap(raw, exclude_ids=frozenset())

    def _status(self, msg: str, timeout_ms: int = 3000) -> None:
        from protoboard.controller.ports import StatusEvent

        if self._view is None:
            return
        self._view.notify_status(
            StatusEvent("wire-edit.status", {"text": msg, "timeout_ms": timeout_ms})
        )

    # -- wire-edit overlays (tool-drawn) ------------------------------------

    @traced()
    def _draw_wire_edit_overlay(self) -> None:
        """Cyan segments for the armed wire + red endpoint dots + a bright
        polyline over the picked span (fragment or whole wire)."""
        if self._preview is None:
            return
        self._clear_wire_edit_overlay()
        marked: dict[uuid.UUID, set[int]] = {}
        state = self._wire_edit
        if state is not None:
            marked.setdefault(state.start_wire_id, set()).add(state.start_idx)
            marked.setdefault(state.end_wire_id, set()).add(state.end_idx)
        # Span points: only for a same-wire range (cross-wire has no single-wire
        # sub-path -> empty span, falls back to cyan + dots).
        span_wid: uuid.UUID | None = None
        span_pts: list[Point] = []
        if state is not None and not state.is_cross:
            span_wire = self._wire_by_id(state.start_wire_id)
            if span_wire is not None:
                span_pts = list(span_wire.path[state.start_idx : state.end_idx + 1])
                span_wid = state.start_wire_id
        wire_ids = set(marked)
        if self._wire_edit_wire_id is not None:
            wire_ids.add(self._wire_edit_wire_id)
        for wid in wire_ids:
            wire = self._wire_by_id(wid)
            if wire is None:
                continue
            self.preview.show_wire_edit_wire(
                list(wire.path),
                marked.get(wid, set()),
                span_pts if wid == span_wid else [],
            )

    def _draw_reroute_preview(self, waypoints: list[Point]) -> None:
        """Dashed orange polyline from the fragment start through the waypoints."""
        if self._preview is None:
            return
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
        if self._preview is not None:
            self._preview.clear_wire_edit()

    def _clear_reroute_overlay(self) -> None:
        if self._preview is not None:
            self._preview.clear_reroute()

    def _clear_reroute_live(self) -> None:
        if self._preview is not None:
            self._preview.clear_reroute_live()
