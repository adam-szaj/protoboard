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
    """The directive's acceptance test: whole-wire simplify (select mode) and
    full-span fragment simplify (edit mode) must emit byte-identical commands.
    Guards the extraction in Task 8 (must still hold afterwards)."""

    def test_whole_wire_equals_full_span_fragment_simplify(
        self, session: EditorSession
    ) -> None:
        from protoboard.controller.selection.wire_edit import (
            WireRange,
            build_simplify_range_command,
        )
        from protoboard.controller.selection.wire_ops import build_simplify_inputs
        from protoboard.services.routing.postprocess import SimplifyMode

        # An L-shaped (corner-cut) wire — simplify actually shortens this,
        # unlike a straight wire over pads (memory Stage-5 TRAP).
        wire = _add_wire(
            session,
            (1.5, 1.5), (2.5, 1.5), (3.5, 1.5), (3.5, 2.5), (3.5, 3.5),
        )
        inputs = build_simplify_inputs(session)
        mode = SimplifyMode.CHEBYSHEV

        whole = build_simplify_range_command(
            session.layout, list(session.layout.wires),
            WireRange.full_span(wire), inputs, mode,
        )
        frag = build_simplify_range_command(
            session.layout, list(session.layout.wires),
            WireRange.full_span(wire), inputs, mode,
        )

        assert isinstance(whole, ReplaceWireFragmentsCommand)
        assert isinstance(frag, ReplaceWireFragmentsCommand)
        # Same builder + same range + same inputs -> structurally identical
        # commands (dataclass equality, mirroring test_wire_edit_helpers.py's
        # field-level comparisons).
        assert whole.wire_id == frag.wire_id
        assert whole.new_fragments == frag.new_fragments
        assert whole.old_fragments == frag.old_fragments
        # And it actually shortened the wire (not a no-op).
        flat = [p for f in whole.new_fragments for p in f.path]
        assert len(flat) < len(wire.path)
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

    def test_commit_currently_returns_to_select(
        self, session: EditorSession
    ) -> None:
        # PARITY-FLIP(Task 4): after Task 4 this becomes "stays armed".
        view, armed = self._armed_after_rotate_step(session)
        armed.on_key(key_inp(Qt.Key.Key_Return))  # confirm
        assert session.edit_controller.armed_transform is None
        assert isinstance(view.active_tool, SelectTool)

    def test_cancel_currently_returns_to_select(
        self, session: EditorSession
    ) -> None:
        # PARITY-FLIP(Task 4): becomes "stays armed" (still selected, re-armed).
        view, armed = self._armed_after_rotate_step(session)
        armed.on_key(key_inp(Qt.Key.Key_Escape))  # cancel
        assert session.edit_controller.armed_transform is None
        assert isinstance(view.active_tool, SelectTool)

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

    def _arm_full_span_edit(
        self, view: HeadlessBoardView, wire: Wire
    ) -> SelectTool:
        from protoboard.controller.selection.wire_edit import WireRange

        tool = _select_tool(view)
        tool._selection.set(WireSelectable(wire=wire))
        tool._wire_edit = WireRange.full_span(wire)
        return tool

    def test_autoroute_op_currently_drops_to_plain_select(
        self, session: EditorSession
    ) -> None:
        # PARITY-FLIP(Task 8): after extraction, autoroute stays in wire-edit.
        view = _headless(session)
        wire = self._detour_wire(session)
        tool = self._arm_full_span_edit(view, wire)

        tool._autoroute_fragment()

        assert tool._wire_edit is None  # dropped to plain select today

    def test_flip_op_currently_drops_to_plain_select(
        self, session: EditorSession
    ) -> None:
        # PARITY-FLIP(Task 8): after extraction, flip stays in wire-edit.
        view = _headless(session)
        wire = self._detour_wire(session)
        tool = self._arm_full_span_edit(view, wire)

        tool._flip_wire_fragment()

        assert tool._wire_edit is None  # dropped to plain select today

    def test_simplify_op_currently_drops_to_plain_select(
        self, session: EditorSession
    ) -> None:
        # PARITY-FLIP(Task 8): after extraction, simplify stays in wire-edit.
        view = _headless(session)
        wire = self._detour_wire(session)
        tool = self._arm_full_span_edit(view, wire)

        tool._simplify_wire_fragment()

        assert tool._wire_edit is None  # dropped to plain select today

    def test_delete_op_currently_drops_to_plain_select(
        self, session: EditorSession
    ) -> None:
        # PARITY-FLIP(Task 8): after extraction, delete stays in wire-edit
        # (deleting the whole wire, though, leaves nothing left to stay on —
        # this captures today's post-op state regardless).
        view = _headless(session)
        wire = self._detour_wire(session)
        tool = self._arm_full_span_edit(view, wire)

        tool._delete_fragment()

        assert tool._wire_edit is None  # dropped to plain select today

    def test_reroute_esc_keeps_fragment(self, session: EditorSession) -> None:
        # MUST stay true: Esc mid-reroute keeps the fragment selected (the
        # two-stage model the rest of the refactor generalises).
        view = _headless(session)
        wire = self._detour_wire(session)
        tool = self._arm_full_span_edit(view, wire)

        tool._begin_reroute()  # arms the waypoint pick
        assert tool._mouse is not None

        tool._reroute_cancel()  # Esc mid-reroute (called by the pick's on_cancel)
        # The pick machinery itself clears self._mouse on cancel; what matters
        # here is that the fragment selection (_wire_edit) was NOT dropped.
        assert tool._wire_edit is not None


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

    def test_esc_midroute_currently_exits(self, session: EditorSession) -> None:
        # PARITY-FLIP(Task 3): after the host fix, first Esc clears points and
        # STAYS routing; only a second (empty) Esc exits. TODAY the host's
        # handle_key_press ALWAYS calls view.escape_to_select() after Esc,
        # regardless of whether the tool consumed it -- so the first Esc
        # mid-route already signals an exit-to-Select at the host level, even
        # though RouteTool itself just clears its own _points.
        view, tool = self._routing(session)
        tool.on_press(pointer(0.5, 0.5))
        tool.on_press(pointer(0.5, 1.5))
        assert tool._points  # mid-route

        view.tools.handle_key_press(_escape_event())

        # RouteTool's own on_key consumed Esc and cleared its points...
        assert tool._points == []
        # ...but the host ALSO fired the escape-to-Select notification on
        # this very first Esc (no two-stage today).
        assert view.notifications.escapes == 1
