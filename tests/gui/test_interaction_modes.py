# SPDX-License-Identifier: MIT
"""GUI tests for the interaction mode (highlight + selection) system."""

from __future__ import annotations

import uuid

import pytest
from PySide6.QtCore import QPointF, Qt
from PySide6.QtGui import QMouseEvent
from PySide6.QtWidgets import QGraphicsItemGroup

from protoboard.domain.layout.wire import WireSegment
from protoboard.geometry import Point
from protoboard.gui.board_view import BoardView
from protoboard.gui.interaction_mode import (
    INTERACTION_MODES,
    HighlightScope,
    SelectionTarget,
    get_mode,
)
from protoboard.gui.session import EditorSession
from protoboard.gui.tools.place_tool import PlaceTool
from protoboard.gui.tools.select_tool import SelectTool


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

def _place(board_view: BoardView, session: EditorSession, x: float, y: float) -> object:
    board_view.set_tool(PlaceTool("R"))
    tool = board_view._active_tool
    assert isinstance(tool, PlaceTool)
    ev = _ev(x, y, Qt.MouseButton.LeftButton)
    tool.on_press(ev, QPointF(x, y))
    while tool._phase == "drag_pin":
        tool.on_press(ev, QPointF(x, y))
    return list(session.instances.values())[-1]


def _ev(x: float, y: float, btn: Qt.MouseButton) -> QMouseEvent:
    return QMouseEvent(
        QMouseEvent.Type.MouseButtonPress,
        QPointF(x, y), QPointF(x, y),
        btn, btn, Qt.KeyboardModifier.NoModifier,
    )


def _press(tool: SelectTool, x: float, y: float) -> None:
    tool.on_press(_ev(x, y, Qt.MouseButton.LeftButton), QPointF(x, y))


def _add_wire(session: EditorSession, p1: Point, p2: Point) -> WireSegment:
    wire = WireSegment(
        id=uuid.uuid4(),
        path=[p1, p2],
        insulated=True,
        side="top",
    )
    session.layout.wires.append(wire)
    session.invalidate_graph()
    return wire


# ---------------------------------------------------------------------------
# Tests: interaction mode state
# ---------------------------------------------------------------------------

class TestInteractionModeState:
    def test_default_mode_on_init(self, board_view: BoardView) -> None:
        board_view.set_tool(SelectTool())
        tool = board_view._active_tool
        assert isinstance(tool, SelectTool)
        assert tool._interaction_mode.id == "default"

    def test_set_interaction_mode_updates_state(self, board_view: BoardView) -> None:
        board_view.set_tool(SelectTool())
        tool = board_view._active_tool
        assert isinstance(tool, SelectTool)
        mode = get_mode("net_logical_all")
        tool.set_interaction_mode(mode)
        assert tool._interaction_mode.id == "net_logical_all"

    def test_backtick_cycles_modes(self, board_view: BoardView) -> None:
        from PySide6.QtGui import QKeyEvent
        board_view.set_tool(SelectTool())
        tool = board_view._active_tool
        assert isinstance(tool, SelectTool)

        ids = list(INTERACTION_MODES.keys())
        initial_idx = ids.index(tool._interaction_mode.id)

        key_event = QKeyEvent(
            QKeyEvent.Type.KeyPress,
            Qt.Key.Key_QuoteLeft,
            Qt.KeyboardModifier.NoModifier,
        )
        tool.on_key(key_event)
        expected_id = ids[(initial_idx + 1) % len(ids)]
        assert tool._interaction_mode.id == expected_id

    def test_set_mode_emits_signal(
        self, board_view: BoardView, session: EditorSession
    ) -> None:
        board_view.set_tool(SelectTool())
        tool = board_view._active_tool
        assert isinstance(tool, SelectTool)
        received: list[str] = []
        session.highlight_mode_changed.connect(received.append)
        tool.set_interaction_mode(get_mode("net_physical"))
        assert received == ["Net (Physical)"]


# ---------------------------------------------------------------------------
# Tests: default mode — no highlight, normal selection
# ---------------------------------------------------------------------------

class TestDefaultMode:
    def test_component_click_selects_component(
        self, board_view: BoardView, session: EditorSession
    ) -> None:
        inst = _place(board_view, session, 0.5, 0.5)
        board_view.set_tool(SelectTool())
        tool = board_view._active_tool
        assert isinstance(tool, SelectTool)
        tool.set_interaction_mode(get_mode("default"))
        board_view.refresh()
        _press(tool, 0.5, 0.5)
        assert tool._selected_id == inst.id or inst.id in tool._selected_ids

    def test_default_mode_no_highlight_items(
        self, board_view: BoardView, session: EditorSession
    ) -> None:
        _place(board_view, session, 0.5, 0.5)
        board_view.set_tool(SelectTool())
        tool = board_view._active_tool
        assert isinstance(tool, SelectTool)
        tool.set_interaction_mode(get_mode("default"))
        board_view.refresh()
        _press(tool, 0.5, 0.5)
        assert tool._highlight_items == []

    def test_default_mode_no_net_dimming(
        self, board_view: BoardView, session: EditorSession
    ) -> None:
        _place(board_view, session, 0.5, 0.5)
        board_view.set_tool(SelectTool())
        tool = board_view._active_tool
        assert isinstance(tool, SelectTool)
        tool.set_interaction_mode(get_mode("default"))
        board_view.refresh()
        _press(tool, 0.5, 0.5)
        assert board_view._highlighted_net_id is None


# ---------------------------------------------------------------------------
# Tests: wire click → select_connected_comps mode
# ---------------------------------------------------------------------------

class TestSelectConnectedCompsMode:
    def test_wire_click_selects_correlated_comps_not_wire(
        self, board_view: BoardView, session: EditorSession
    ) -> None:
        i1 = _place(board_view, session, 0.5, 0.5)
        i2 = _place(board_view, session, 3.5, 0.5)
        from protoboard.domain.schematic.net import Net, PinRef
        net = Net(id=uuid.uuid4(), name="N1", pins={
            PinRef(i1.id, 1), PinRef(i2.id, 1),
        })
        session.schematic.nets[net.id] = net

        wire = _add_wire(session, Point(0.5, 0.5), Point(3.5, 0.5))
        board_view.refresh()
        board_view.set_tool(SelectTool())
        tool = board_view._active_tool
        assert isinstance(tool, SelectTool)
        tool.set_interaction_mode(get_mode("select_connected_comps"))

        # Simulate wire click: pass wire_id directly to _apply_interaction.
        tool._apply_interaction(wire_id=wire.id)

        # Wire should NOT be in selected_wire_ids.
        assert wire.id not in tool._selected_wire_ids
        assert tool._selected_wire_id != wire.id
        # But correlated comps should be selected.
        all_selected = tool._selected_ids | (
            {tool._selected_id} if tool._selected_id else set()
        )
        assert i1.id in all_selected or i2.id in all_selected


# ---------------------------------------------------------------------------
# Tests: inspect_only mode — no selection change
# ---------------------------------------------------------------------------

class TestInspectOnlyMode:
    def test_inspect_mode_does_not_change_selection(
        self, board_view: BoardView, session: EditorSession
    ) -> None:
        i1 = _place(board_view, session, 0.5, 0.5)
        i2 = _place(board_view, session, 3.5, 0.5)
        board_view.refresh()
        board_view.set_tool(SelectTool())
        tool = board_view._active_tool
        assert isinstance(tool, SelectTool)
        tool.set_interaction_mode(get_mode("inspect_only"))

        # Pre-select i1 manually.
        tool._selected_id = i1.id
        # Now apply interaction on i2.
        tool._apply_interaction(comp_id=i2.id)

        # Selection should be unchanged (NOTHING target).
        assert tool._selected_id == i1.id


# ---------------------------------------------------------------------------
# Tests: clear highlight on empty click
# ---------------------------------------------------------------------------

class TestClearHighlight:
    def test_empty_click_clears_highlight_items(
        self, board_view: BoardView, session: EditorSession
    ) -> None:
        _place(board_view, session, 0.5, 0.5)
        board_view.set_tool(SelectTool())
        tool = board_view._active_tool
        assert isinstance(tool, SelectTool)
        tool.set_interaction_mode(get_mode("net_logical_all"))
        board_view.refresh()

        # Click component — may draw highlight items.
        _press(tool, 0.5, 0.5)
        # Click empty area to clear.
        _press(tool, 10.0, 10.0)

        assert tool._highlight_items == []
        assert board_view._highlighted_net_id is None

    def test_set_mode_redispatches_if_source_active(
        self, board_view: BoardView, session: EditorSession
    ) -> None:
        """Changing mode re-runs highlight on last click source."""
        _place(board_view, session, 0.5, 0.5)
        board_view.set_tool(SelectTool())
        tool = board_view._active_tool
        assert isinstance(tool, SelectTool)
        board_view.refresh()

        # Set a source manually.
        tool._hl_source_comp = list(session.instances.keys())[0]

        # Switch to a mode with NONE scope — should clear dimming.
        tool.set_interaction_mode(get_mode("default"))
        assert board_view._highlighted_net_id is None
