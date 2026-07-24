# SPDX-License-Identifier: MIT
"""Tests for the view notification channel (docs/qt-ports.md §2.3).

The seam publishes ``notify_status`` / ``notify_cursor_moved`` /
``escape_to_select``; the concrete BoardView re-emits them as Qt signals the
consumer (MainWindow, or a recording test double) subscribes to.  Tests
assert *structural* payload fields, not formatted strings.
"""

from __future__ import annotations

import uuid

from protoboard.geometry import Point
from protoboard.gui.board_view import BoardView
from protoboard.controller.ports import StatusEvent
from protoboard.gui.tools.gui_tools import SelectTool, WireEditTool


class TestStatusEvent:
    def test_text_and_timeout_read_from_data(self) -> None:
        evt = StatusEvent("select.status", {"text": "hi", "timeout_ms": 500})
        assert evt.kind == "select.status"
        assert evt.text == "hi"
        assert evt.timeout_ms == 500

    def test_defaults_are_empty(self) -> None:
        evt = StatusEvent("place.floating.cleared")
        assert evt.text == ""
        assert evt.timeout_ms == 0


class TestNotificationChannel:
    def test_notify_status_reemits_status_changed(
        self, board_view: BoardView
    ) -> None:
        events: list[StatusEvent] = []
        board_view.status_changed.connect(events.append)
        evt = StatusEvent("route.locked_pin", {"text": "x", "timeout_ms": 3000})
        board_view.notify_status(evt)
        assert events == [evt]

    def test_notify_cursor_moved_reemits_cursor_moved(
        self, board_view: BoardView
    ) -> None:
        seen: list[tuple[float, float]] = []
        board_view.cursor_moved.connect(lambda x, y: seen.append((x, y)))
        board_view.notify_cursor_moved(Point(1.5, 2.5))
        assert seen == [(1.5, 2.5)]

    def test_escape_to_select_reemits_escape_tool(
        self, board_view: BoardView
    ) -> None:
        fired: list[bool] = []
        board_view.escape_tool.connect(lambda: fired.append(True))
        board_view.escape_to_select()
        assert fired == [True]


class TestToolStatusPublication:
    def test_select_tool_status_publishes_structural_event(
        self, board_view: BoardView
    ) -> None:
        events: list[StatusEvent] = []
        board_view.status_changed.connect(events.append)
        tool = SelectTool()
        board_view.set_tool(tool)
        tool._status("Entered group — [Esc] to leave", 4000)
        assert len(events) == 1
        assert events[0].kind == "select.status"
        assert events[0].data["text"] == "Entered group — [Esc] to leave"
        assert events[0].data["timeout_ms"] == 4000


class TestWheelNotchAccumulator:
    def test_sub_detent_ticks_fold_into_one_notch(self) -> None:
        from protoboard.controller.ports.inputs_details import WheelNotchAccumulator

        acc = WheelNotchAccumulator()
        assert acc.add(40) == 0
        assert acc.add(40) == 0
        assert acc.add(40) == 1  # 3 × 40 = one detent

    def test_hard_spin_yields_multiple_notches(self) -> None:
        from protoboard.controller.ports.inputs_details import WheelNotchAccumulator

        acc = WheelNotchAccumulator()
        assert acc.add(240) == 2

    def test_direction_flip_drops_residue(self) -> None:
        from protoboard.controller.ports.inputs_details import WheelNotchAccumulator

        acc = WheelNotchAccumulator()
        assert acc.add(100) == 0    # residue +100
        assert acc.add(-120) == -1  # flip: residue dropped, clean notch down


class TestInteractionContext:
    @staticmethod
    def _arm_wire_edit(board_view: BoardView) -> WireEditTool:
        """Arm a WireEditTool on a wire so its actions (reroute / …) surface in
        the context query — reroute moved to WireEditTool in the Task-8 swap."""
        from protoboard.controller.selection.wire_edit import WireRange
        from protoboard.domain.layout.wire import Wire

        session = board_view.session
        wire = Wire(
            id=uuid.uuid4(),
            path=[Point(1.5, 1.5), Point(2.5, 1.5), Point(3.5, 1.5)],
            insulated=True, side="top",
        )
        session.layout.wires.append(wire)
        session.invalidate_graph()
        board_view.set_tool(SelectTool())
        tool = WireEditTool(
            WireRange.full_span(wire), wire.id,
            return_to=lambda: None,
        )
        board_view.set_tool(tool)
        return tool

    def test_get_context_at_lists_tool_actions(
        self, board_view: BoardView
    ) -> None:
        # A pure query — no menu, no fake, no exec (qt-ports §2.4).  reroute is
        # a WireEditTool action after the Task-8 swap.
        self._arm_wire_edit(board_view)
        ctx = board_view.tools.get_context_at(Point(2.5, 1.5), Point(10.0, 20.0))
        assert ctx.kind == "canvas"
        assert ctx.scene_pos == Point(2.5, 1.5)
        ids = [opt.action_id for opt in ctx.actions]
        assert "select-tool.action.reroute" in ids
        reroute = next(
            o for o in ctx.actions
            if o.action_id == "select-tool.action.reroute"
        )
        assert reroute.shortcut_hint == "W"

    def test_notify_context_reemits_context_published(
        self, board_view: BoardView
    ) -> None:
        from protoboard.controller.ports import InteractionContext

        seen: list[InteractionContext] = []
        board_view.context_published.connect(seen.append)
        ctx = board_view.tools.get_context_at(Point(0.5, 0.5), Point(1.0, 1.0))
        board_view.notify_context(ctx)
        assert seen == [ctx]

    def test_context_reflects_rebind(self, board_view: BoardView) -> None:
        board_view.keymap.set_override("select-tool.action.reroute", "Q")
        self._arm_wire_edit(board_view)
        ctx = board_view.tools.get_context_at(Point(0.5, 0.5), Point(0.0, 0.0))
        reroute = next(
            o for o in ctx.actions
            if o.action_id == "select-tool.action.reroute"
        )
        assert reroute.shortcut_hint == "Q"
