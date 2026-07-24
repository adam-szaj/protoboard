# SPDX-License-Identifier: MIT
"""Parity net for the ArmedSubTool two-level-lifecycle refactor.

Captures the CURRENT (pre-refactor) commit/cancel/exit behaviour of
ArmedTransformTool, wire-edit (SelectTool sub-mode) and RouteTool, plus the
directive acceptance test. Tasks 4/5/8 intentionally flip the assertions
marked ``PARITY-FLIP`` from "exits after op" to "stays armed after op".
"""
from __future__ import annotations

import uuid

from PySide6.QtCore import QEvent, Qt
from PySide6.QtGui import QKeyEvent

from protoboard.controller.selection.adapters import WireSelectable
from protoboard.controller.tools.drag_commit_decision import (
    DragCommitDecision,
    DragSeverity,
)
from protoboard.domain.commands.component_cmds import ReplaceWireFragmentsCommand
from protoboard.domain.components.instance import ComponentInstance
from protoboard.domain.layout.wire import Wire
from protoboard.geometry import Point
from protoboard.gui.session import EditorSession
from protoboard.gui.tools.gui_tools import (
    ArmedTransformTool,
    PlaceTool,
    RouteTool,
    SelectTool,
    WireEditTool,
)
from tests.fakes.board_view import HeadlessBoardView
from tests.gui.input_helpers import key_inp, pointer

# --------------------------------------------------------------------------- #
# Helpers — mirror test_headless_tool_flows.py's idioms.
# --------------------------------------------------------------------------- #


def _headless(session: EditorSession) -> HeadlessBoardView:
    return HeadlessBoardView(session)


def _place(
    view: HeadlessBoardView, x: float, y: float, kind: str = "R"
) -> ComponentInstance:
    view.tools.set_tool(PlaceTool(kind))
    tool = view.active_tool
    assert isinstance(tool, PlaceTool)
    tool.on_press(pointer(x, y))
    while tool._phase == "drag_pin":
        tool.on_press(pointer(x, y))
    return list(view.session.instances.values())[-1]


def _select_tool(view: HeadlessBoardView) -> SelectTool:
    tool = SelectTool()
    view.tools.set_tool(tool)
    return tool


def _armed(view: HeadlessBoardView) -> ArmedTransformTool:
    tool = view.active_tool
    assert isinstance(tool, ArmedTransformTool)
    return tool


def _add_wire(
    session: EditorSession, *pts: tuple[float, float], insulated: bool = True
) -> Wire:
    w = Wire(
        id=uuid.uuid4(),
        path=[Point(x, y) for x, y in pts],
        insulated=insulated,
        side="top",
    )
    session.layout.wires.append(w)
    session.invalidate_graph()
    session.layout_changed.emit()
    return w


def _escape_event() -> QKeyEvent:
    ev = QKeyEvent(
        QEvent.Type.KeyPress,
        Qt.Key.Key_Escape,
        Qt.KeyboardModifier.NoModifier,
    )
    ev.ignore()
    return ev


# --------------------------------------------------------------------------- #


class TestWholeWireVsFragmentSimplifyParity:
    """The directive's acceptance test: a whole-wire (full-span) simplify and a
    full-span *fragment* simplify must emit the same command.

    Strengthened for the Task-8 swap: instead of calling
    ``build_simplify_range_command`` twice with identical args (which only
    proved the builder is deterministic), this now drives the REAL EDIT-MODE
    dispatch path — ``[E]`` on a selected wire hands off to
    :class:`WireEditTool`, then ``[S]`` (``_simplify_wire_fragment``) executes
    the full-span fragment simplify.  It asserts the command that actually
    landed on the stack equals the reference ``build_simplify_range_command(
    WireRange.full_span(...))`` — i.e. the edit-mode entry emits exactly the
    whole-wire full-span builder's command, not merely a re-run of the builder.

    Scope note: the select-mode context-menu "Simplify wire" entry
    (``SelectTool._simplify_wires``) is a SEPARATE, worker-backed path that
    uses ``generic_simplify_wire`` + ``replace_wire_path_cmd`` — a different
    builder (cross-wire aware, multi-wire) that does NOT emit a
    ``ReplaceWireFragmentsCommand``.  A byte-identical comparison against it is
    therefore not meaningful; the genuine "whole-wire == full-span fragment"
    equivalence lives in the ``build_simplify_range_command`` family, which is
    what the edit-mode entry drives and what this test pins."""

    def test_edit_mode_full_span_equals_builder_command(
        self, session: EditorSession
    ) -> None:
        from protoboard.controller.selection.adapters import (
            WireSelectable,
            WireVertexSelectable,
        )
        from protoboard.controller.selection.wire_edit import (
            WireRange,
            build_simplify_range_command,
        )
        from protoboard.controller.selection.wire_ops import build_simplify_inputs
        from protoboard.gui.tools.gui_tools import WireEditTool
        from protoboard.services.routing.postprocess import SimplifyMode

        # An L-shaped (corner-cut) wire — simplify actually shortens this,
        # unlike a straight wire over pads (memory Stage-5 TRAP).  Routed
        # through the controller so it carries real endpoint knots.
        view = _headless(session)
        wire_id = session.edit_controller.add_wire(
            [Point(1.5, 1.5), Point(2.5, 1.5), Point(3.5, 1.5),
             Point(3.5, 2.5), Point(3.5, 3.5)],
            insulated=True, side="top",
        )
        assert wire_id is not None
        wire = session.layout.wires.get(wire_id)
        assert wire is not None

        # Reference command: whole-wire = full-span range through the builder.
        mode = getattr(
            session.routing_settings, "simplify_mode", SimplifyMode.BARE_PARITY
        )
        reference = build_simplify_range_command(
            session.layout, list(session.layout.wires),
            WireRange.full_span(wire),
            build_simplify_inputs(session), mode,
        )
        assert isinstance(reference, ReplaceWireFragmentsCommand)

        # Edit-mode path: [E] → hand off to WireEditTool → [S].
        select = _select_tool(view)
        select._selection.set(WireSelectable(wire=wire))
        select.arm_wire_edit()
        enriched = session.layout.wires.get(wire_id)
        assert enriched is not None
        last = len(enriched.path) - 1
        select._range_picked(
            [WireVertexSelectable(wire_id, 0),
             WireVertexSelectable(wire_id, last)]
        )
        tool = view.active_tool
        assert isinstance(tool, WireEditTool)
        n_points_before = len(enriched.path)  # pre-op vertex count

        depth_before = len(session.stack._undo)  # type: ignore[attr-defined]
        tool._simplify_wire_fragment()
        assert len(session.stack._undo) == depth_before + 1  # type: ignore[attr-defined]
        emitted = session.stack._undo[-1]  # type: ignore[attr-defined]

        # The command the edit-mode entry actually emitted matches the whole-
        # wire full-span builder command field-for-field.
        assert isinstance(emitted, ReplaceWireFragmentsCommand)
        assert emitted.wire_id == reference.wire_id
        assert emitted.new_fragments == reference.new_fragments
        assert emitted.old_fragments == reference.old_fragments
        # And it actually shortened the wire (not a no-op).
        flat = [p for f in emitted.new_fragments for p in f.path]
        assert len(flat) < n_points_before
        assert flat[0] == Point(1.5, 1.5) and flat[-1] == Point(3.5, 3.5)


class TestArmedTransformExitParity:
    def _armed_after_rotate_step(
        self, session: EditorSession
    ) -> tuple[HeadlessBoardView, ArmedTransformTool]:
        view = _headless(session)
        _place(view, 0.5, 0.5)
        tool = _select_tool(view)
        tool.on_press(pointer(0.5, 0.5))
        tool.on_key(key_inp(Qt.Key.Key_R))  # arm
        armed = _armed(view)
        armed.on_key(key_inp(Qt.Key.Key_R))  # step +90
        return view, armed

    def test_commit_stays_armed(
        self, session: EditorSession
    ) -> None:
        # Task 4: confirm commits and RE-ARMS on the (possibly moved)
        # selection instead of returning to SelectTool.
        view, armed = self._armed_after_rotate_step(session)
        armed.on_key(key_inp(Qt.Key.Key_Return))  # confirm
        assert view.active_tool is armed
        assert armed._armed is not None and not armed._armed.finished

    def test_cancel_stays_armed(
        self, session: EditorSession
    ) -> None:
        # Task 4: cancel reverts the pending op and RE-ARMS — it does not
        # exit to SelectTool (only host [Esc]-when-clean does that).
        view, armed = self._armed_after_rotate_step(session)
        armed.on_key(key_inp(Qt.Key.Key_Escape))  # cancel
        assert view.active_tool is armed
        assert armed._armed is not None and not armed._armed.finished

    def test_esc_trace_second_press_reaches_clean_exit(
        self, session: EditorSession
    ) -> None:
        # Two-stage [Esc] trace (host's ToolHost.handle_key_press evaluates
        # can_exit() BEFORE dispatching the key): the first Esc, with a real
        # pending delta (the +90 step), reports can_exit()=False — the cancel
        # lands and the tool stays armed (re-armed). The re-arm is untouched
        # (no delta yet), so has_pending_op() is False and can_exit() is now
        # True — a second Esc (host path) would exit. This proves Esc never
        # traps the user: it always reaches a clean, exitable state.
        view, armed = self._armed_after_rotate_step(session)
        assert armed.has_pending_op() is True  # the +90 step is a real delta
        assert armed.can_exit() is False  # first Esc must cancel, not exit

        armed.on_key(key_inp(Qt.Key.Key_Escape))  # first Esc: cancel + re-arm

        assert view.active_tool is armed  # stayed armed (re-armed)
        assert armed.has_pending_op() is False  # fresh re-arm, no delta
        assert armed.can_exit() is True  # second Esc (host) would now exit

    def test_hard_conflict_parks_armed(self, session: EditorSession) -> None:
        # MUST stay true across the whole refactor: a HARD-conflict confirm
        # does NOT exit — it parks.
        view, armed = self._armed_after_rotate_step(session)
        transform = session.edit_controller.armed_transform
        assert transform is not None
        transform._classify = lambda: DragCommitDecision(  # type: ignore[method-assign]
            severity=DragSeverity.HARD
        )
        assert transform.can_commit() is False

        armed.on_key(key_inp(Qt.Key.Key_Return))  # confirm attempt

        assert session.edit_controller.armed_transform is transform  # parked
        assert view.active_tool is armed
        assert transform.finished is False


class TestWireEditExitParity:
    """Task 8 SWAP: ``[E]`` now hands off to :class:`WireEditTool`; each
    id-preserving op (autoroute / flip / simplify) STAYS armed in wire-edit
    (re-armed on the mutated wire's full span), mirroring
    ``TestArmedTransformExitParity.test_commit_stays_armed``.  Delete is the
    one op that EXITS (the wire is gone — nothing to re-arm), dropping back to
    plain select."""

    @staticmethod
    def _detour_wire(session: EditorSession) -> Wire:
        # Orthogonal L on the top layer: simplify corner-cuts it. Routed
        # through the controller (not a raw layout.wires.append) so the wire
        # gets real endpoint knots -- required by FlipWireFragmentCommand.
        wire_id = session.edit_controller.add_wire(
            [Point(1.5, 1.5), Point(2.5, 1.5), Point(3.5, 1.5),
             Point(3.5, 2.5), Point(3.5, 3.5)],
            insulated=True, side="top",
        )
        assert wire_id is not None
        wire = session.layout.wires.get(wire_id)
        assert wire is not None
        return wire

    def _arm_via_entry(
        self, view: HeadlessBoardView, wire: Wire
    ) -> WireEditTool:
        """Drive the REAL ``[E]`` entry: select the wire, arm wire-edit, then
        resolve the full-span fragment through ``_range_picked`` — which hands
        off to the WireEditTool (the swap under test).  Returns the now-active
        WireEditTool."""
        from protoboard.controller.selection.adapters import WireVertexSelectable

        tool = _select_tool(view)
        tool._selection.set(WireSelectable(wire=wire))
        tool.arm_wire_edit()  # runs _ensure_wire_ready_for_edit + arms the pick
        # Re-read the wire: _ensure_wire_ready_for_edit may have enriched its
        # path (and thus replaced the object) before the fragment resolves.
        wire = view.session.layout.wires.get(wire.id)
        assert wire is not None
        last = len(wire.path) - 1
        tool._range_picked(
            [WireVertexSelectable(wire.id, 0), WireVertexSelectable(wire.id, last)]
        )
        active = view.active_tool
        assert isinstance(active, WireEditTool)  # handed off to wire-edit
        return active

    def test_autoroute_op_stays_in_wire_edit(
        self, session: EditorSession
    ) -> None:
        view = _headless(session)
        wire = self._detour_wire(session)
        tool = self._arm_via_entry(view, wire)

        tool._autoroute_fragment()

        assert tool._wire_edit is not None  # re-armed on the mutated wire
        assert view.active_tool is tool  # stayed in wire-edit

    def test_flip_op_stays_in_wire_edit(
        self, session: EditorSession
    ) -> None:
        view = _headless(session)
        wire = self._detour_wire(session)
        tool = self._arm_via_entry(view, wire)

        tool._flip_wire_fragment()

        assert tool._wire_edit is not None
        assert view.active_tool is tool

    def test_simplify_op_stays_in_wire_edit(
        self, session: EditorSession
    ) -> None:
        view = _headless(session)
        wire = self._detour_wire(session)
        tool = self._arm_via_entry(view, wire)

        tool._simplify_wire_fragment()

        assert tool._wire_edit is not None
        assert view.active_tool is tool

    def test_delete_op_exits_to_plain_select(
        self, session: EditorSession
    ) -> None:
        # The ONE op that exits: deleting the whole wire leaves nothing to
        # re-arm, so control returns to the plain SelectTool via return_to.
        view = _headless(session)
        wire = self._detour_wire(session)
        tool = self._arm_via_entry(view, wire)
        n_before = len(session.layout.wires)

        tool._delete_fragment()

        assert view.active_tool is not tool
        assert isinstance(view.active_tool, SelectTool)
        assert len(session.layout.wires) == n_before - 1

    def test_two_consecutive_ops_without_rearm(
        self, session: EditorSession
    ) -> None:
        # Proof of the stays-armed model: [S] then [F] both land commands on
        # the stack without a re-entry round-trip through SelectTool.
        view = _headless(session)
        wire = self._detour_wire(session)
        tool = self._arm_via_entry(view, wire)
        n_before = len(session.stack._undo)  # type: ignore[attr-defined]

        tool._simplify_wire_fragment()
        assert view.active_tool is tool  # no round-trip
        tool._flip_wire_fragment()
        assert view.active_tool is tool

        assert len(session.stack._undo) == n_before + 2  # type: ignore[attr-defined]

    def test_reroute_esc_keeps_fragment(self, session: EditorSession) -> None:
        # MUST stay true: Esc mid-reroute keeps the fragment selected (the
        # two-stage model the rest of the refactor generalises).
        view = _headless(session)
        wire = self._detour_wire(session)
        tool = self._arm_via_entry(view, wire)

        tool._begin_reroute()  # arms the waypoint pick
        assert tool._mouse is not None

        tool.cancel_op()  # Esc mid-reroute
        # The fragment (_wire_edit) is NOT dropped; only the waypoint pick was.
        assert tool._wire_edit is not None
        assert view.active_tool is tool

    def test_handback_resyncs_selection_to_mutated_wire(
        self, session: EditorSession
    ) -> None:
        # The dropped _refresh_highlight fix (8a reviewer Important finding): a
        # simplify replaces the Wire OBJECT (fresh geometry, same id).  When
        # control returns to SelectTool, its selection must re-sync to the
        # CURRENT wire — not keep the stale pre-op snapshot — so the highlight
        # renders post-op geometry (memory feedback_refresh_highlights_after_
        # mutation).
        from protoboard.controller.selection.adapters import WireSelectable

        view = _headless(session)
        wire = self._detour_wire(session)
        select = _select_tool(view)
        select._selection.set(WireSelectable(wire=wire))
        select.arm_wire_edit()
        fresh = session.layout.wires.get(wire.id)
        assert fresh is not None
        last = len(fresh.path) - 1
        from protoboard.controller.selection.adapters import WireVertexSelectable
        select._range_picked(
            [WireVertexSelectable(wire.id, 0),
             WireVertexSelectable(wire.id, last)]
        )
        wire_edit = view.active_tool
        assert isinstance(wire_edit, WireEditTool)

        wire_edit._simplify_wire_fragment()  # mutates geometry (id preserved)
        wire_edit._exit()  # empty [Esc] → hand back to SelectTool

        assert view.active_tool is select
        # The re-synced selection points at the CURRENT wire object, and the
        # highlight tracks its (live) id — not a stale snapshot / dead id.
        selected = list(select._selection.selected())
        assert [s.to_hit().id for s in selected] == [wire.id]
        current = session.layout.wires.get(wire.id)
        assert current is not None
        assert any(s.wire is current for s in selected
                   if isinstance(s, WireSelectable))
        entry = session.highlight_stack.active()
        assert entry is not None
        assert wire.id in entry.wire_ids

    def test_handback_after_delete_drops_dead_wire_highlight(
        self, session: EditorSession
    ) -> None:
        # Delete removes the wire entirely: on hand-back the selection is
        # cleared and the highlight must NOT retain the now-dead wire id.
        view = _headless(session)
        wire = self._detour_wire(session)
        tool = self._arm_via_entry(view, wire)

        tool._delete_fragment()  # wire gone -> return_to fires

        select = view.active_tool
        assert isinstance(select, SelectTool)
        assert select._selection.is_empty()
        entry = session.highlight_stack.active()
        # Either no active entry, or one that no longer references the dead id.
        assert entry is None or wire.id not in entry.wire_ids


class TestRouteToolExitParity:
    def _routing(self, session: EditorSession) -> tuple[HeadlessBoardView, RouteTool]:
        view = _headless(session)
        view.tools.set_tool(RouteTool())
        tool = view.active_tool
        assert isinstance(tool, RouteTool)
        return view, tool

    def test_commit_stays_in_routing(self, session: EditorSession) -> None:
        # MUST stay true: Enter commits a wire and RouteTool stays active with
        # empty _points.
        view, tool = self._routing(session)
        tool.on_press(pointer(0.5, 0.5))
        tool.on_press(pointer(0.5, 1.5))
        n_wires_before = len(session.layout.wires)

        tool.on_key(key_inp(Qt.Key.Key_Return))  # commit

        assert len(session.layout.wires) == n_wires_before + 1
        assert view.active_tool is tool  # RouteTool never exited
        assert tool._points == []

    def test_route_tool_conforms_to_armed_subtool(self) -> None:
        # RouteTool here is the GUI-composed class (gui_tools.RouteTool =
        # _RouteTool + ViewToolMixin) — the one the app actually instantiates.
        # The bare controller.tools.route_tool.RouteTool lacks on_key (it's
        # added by ViewToolMixin), so it alone never satisfies ArmedSubTool;
        # the composed class is the correct conformance target.
        from protoboard.controller.tools.armed_subtool import ArmedSubTool

        assert isinstance(RouteTool(), ArmedSubTool)

    def test_esc_is_two_stage(self, session: EditorSession) -> None:
        # The host fix: first Esc mid-route cancels the pending op (points
        # cleared) and STAYS in RouteTool -- can_exit() is False while points
        # are pending, so the host must skip escape_to_select(). Only a
        # second (empty) Esc, with can_exit() now True, exits to Select.
        view, tool = self._routing(session)
        tool.on_press(pointer(0.5, 0.5))
        tool.on_press(pointer(0.5, 1.5))
        assert tool._points  # mid-route

        view.tools.handle_key_press(_escape_event())

        # RouteTool's own on_key consumed Esc and cleared its points...
        assert tool._points == []
        # ...and stayed active: the host must NOT have fired escape_to_select
        # on this first Esc (can_exit() was False while a point was pending).
        assert view.notifications.escapes == 0
        assert view.tools.active_tool is tool

        # Second Esc: nothing pending -> can_exit() is True -> host exits.
        view.tools.handle_key_press(_escape_event())

        assert view.notifications.escapes == 1


class TestRouteConflictGate:
    """Task 6: a route whose endpoint lands on an alien-net pad refuses to
    commit and stays armed (points intact) instead of silently wiring two
    different nets together."""

    def _routing(self, session: EditorSession) -> tuple[HeadlessBoardView, RouteTool]:
        view = _headless(session)
        view.tools.set_tool(RouteTool())
        tool = view.active_tool
        assert isinstance(tool, RouteTool)
        return view, tool

    def test_route_ending_on_alien_net_pad_refuses(
        self, session: EditorSession
    ) -> None:
        view = _headless(session)
        # comp1: net-A pad pair, wired together (netted).
        _place(view, 0.5, 0.5)
        # comp2: pad pair left UNCONNECTED (no wire) — its pins carry no net.
        _place(view, 2.5, 0.5)

        view.tools.set_tool(RouteTool())
        tool = view.active_tool
        assert isinstance(tool, RouteTool)
        tool.on_press(pointer(0.5, 0.5))
        tool.on_press(pointer(0.5, 1.5))
        tool._commit()  # net A now lives on comp1's two pins

        # Route from comp2's (unconnected) pad to comp1's (netted) pad.
        tool.on_press(pointer(2.5, 0.5))
        tool.on_press(pointer(0.5, 0.5))

        assert tool.can_commit() is False

        added_before = len(session.layout.wires)
        assert tool.commit_op() is False
        assert len(session.layout.wires) == added_before  # not committed
        assert tool.has_pending_op()  # stays armed, points intact
        assert view.active_tool is tool

    def test_clean_route_commits(self, session: EditorSession) -> None:
        view, tool = self._routing(session)
        tool.on_press(pointer(0.5, 0.5))
        tool.on_press(pointer(0.5, 1.5))

        assert tool.can_commit() is True

        added_before = len(session.layout.wires)
        assert tool.commit_op() is True
        assert len(session.layout.wires) == added_before + 1

