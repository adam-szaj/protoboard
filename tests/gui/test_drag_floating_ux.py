# SPDX-License-Identifier: MIT
"""GUI tests for the drag-and-drop floating-overlay UX.

Covers the user-confirmed behaviour:
* skewed wires are silently dropped on commit
* pin-on-foreign-wire enters SOFT floating; [Enter] commits with wire removed
* body-on-body lands in HARD floating; [Enter] is blocked; [Esc] reverts
* multi-component is all-or-nothing under HARD severity
"""

from __future__ import annotations

import uuid

from PySide6.QtCore import QPointF, Qt
from PySide6.QtGui import QKeyEvent, QMouseEvent

from protoboard.domain.layout.wire import WireSegment
from protoboard.domain.schematic.net import Net
from protoboard.domain.schematic.schematic import PinRef
from protoboard.geometry import Point
from protoboard.gui.board_view import BoardView
from protoboard.gui.session import EditorSession
from protoboard.gui.tools.drag_commit_decision import DragSeverity
from protoboard.gui.tools.place_tool import PlaceTool
from protoboard.gui.tools.select_tool import SelectTool


# ---------------------------------------------------------------------------
# Helpers (mirrors tests/gui/test_select_tool.py)
# ---------------------------------------------------------------------------

def _place(board_view: BoardView, session: EditorSession, x: float, y: float):
    board_view.set_tool(PlaceTool("R"))
    tool = board_view._active_tool
    assert isinstance(tool, PlaceTool)
    ev = QMouseEvent(
        QMouseEvent.Type.MouseButtonPress,
        QPointF(x, y), QPointF(x, y),
        Qt.MouseButton.LeftButton, Qt.MouseButton.LeftButton,
        Qt.KeyboardModifier.NoModifier,
    )
    tool.on_press(ev, QPointF(x, y))
    while tool._phase == "drag_pin":
        tool.on_press(ev, QPointF(x, y))
    return list(session.instances.values())[-1]


def _press(tool: SelectTool, x: float, y: float) -> None:
    tool.on_press(
        QMouseEvent(
            QMouseEvent.Type.MouseButtonPress,
            QPointF(x, y), QPointF(x, y),
            Qt.MouseButton.LeftButton, Qt.MouseButton.LeftButton,
            Qt.KeyboardModifier.NoModifier,
        ),
        QPointF(x, y),
    )


def _move(tool: SelectTool, x: float, y: float) -> None:
    tool.on_move(
        QMouseEvent(
            QMouseEvent.Type.MouseMove,
            QPointF(x, y), QPointF(x, y),
            Qt.MouseButton.NoButton, Qt.MouseButton.LeftButton,
            Qt.KeyboardModifier.NoModifier,
        ),
        QPointF(x, y),
    )


def _release(tool: SelectTool, x: float, y: float) -> None:
    tool.on_release(
        QMouseEvent(
            QMouseEvent.Type.MouseButtonRelease,
            QPointF(x, y), QPointF(x, y),
            Qt.MouseButton.LeftButton, Qt.MouseButton.NoButton,
            Qt.KeyboardModifier.NoModifier,
        ),
        QPointF(x, y),
    )


def _key(tool: SelectTool, key: Qt.Key) -> None:
    tool.on_key(QKeyEvent(QKeyEvent.Type.KeyPress, key, Qt.KeyboardModifier.NoModifier))


# ---------------------------------------------------------------------------
# Tests
# ---------------------------------------------------------------------------

class TestSkewedWireCleanup:
    def test_skewed_wire_dropped_on_drag_commit(
        self, board_view: BoardView, session: EditorSession
    ) -> None:
        # R1 at (0.5, 0.5), wire from R1.pin1 to a distant pad.
        r1 = _place(board_view, session, 0.5, 0.5)
        r2 = _place(board_view, session, 4.5, 0.5)
        wire = WireSegment(
            id=uuid.uuid4(),
            path=[Point(0.5, 0.5), Point(4.5, 0.5)],
            insulated=True, side="top",
        )
        session.layout.wires.append(wire)

        board_view.set_tool(SelectTool())
        tool = board_view._active_tool
        assert isinstance(tool, SelectTool)
        # Drag R1 vertically — now wire is (0.5, 2.5) → (4.5, 0.5), skewed.
        _press(tool, 0.5, 0.5)
        _move(tool, 0.5, 2.5)
        _release(tool, 0.5, 2.5)

        # If floating, accept (the dragged wire turning skewed → SOFT).
        if tool._is_floating():
            _key(tool, Qt.Key.Key_Return)

        # Skewed wire dropped.
        assert wire.id not in {w.id for w in session.layout.wires}
        # R1 ended up at new position.
        assert session.layout.placements[r1.id].position == Point(0.5, 2.5)
        # R2 untouched.
        assert session.layout.placements[r2.id].position == Point(4.5, 0.5)


class TestHardFloatingBodyOverlap:
    def test_different_net_pad_collision_enters_hard_floating(
        self, board_view: BoardView, session: EditorSession
    ) -> None:
        # R1 at (0.5, 0.5) with pins at (0.5, 0.5) and (1.5, 0.5).  R2 at
        # (2.5, 0.5) with pins at (2.5, 0.5) and (3.5, 0.5).  Each gets its
        # own net.  Drag R1 to (2.5, 0.5) → R1.pin1 lands on R2.pin1 with a
        # different net = HARD.
        r1 = _place(board_view, session, 0.5, 0.5)
        r2 = _place(board_view, session, 2.5, 0.5)
        net_a = uuid.uuid4()
        net_b = uuid.uuid4()
        session.schematic.nets[net_a] = Net(
            id=net_a, name="VCC",
            pins={PinRef(component_id=r1.id, pin_number=1)},
        )
        session.schematic.nets[net_b] = Net(
            id=net_b, name="GND",
            pins={PinRef(component_id=r2.id, pin_number=1)},
        )

        board_view.set_tool(SelectTool())
        tool = board_view._active_tool
        assert isinstance(tool, SelectTool)
        _press(tool, 0.5, 0.5)
        _move(tool, 2.5, 0.5)
        _release(tool, 2.5, 0.5)

        # Should be in HARD floating state.
        assert tool._is_floating()
        assert tool._floating_severity == DragSeverity.HARD.value

        # [Enter] is blocked.
        _key(tool, Qt.Key.Key_Return)
        assert tool._is_floating()  # still floating
        # R1 still at new (illegal) position.
        assert session.layout.placements[r1.id].position == Point(2.5, 0.5)

        # [Esc] reverts.
        _key(tool, Qt.Key.Key_Escape)
        assert not tool._is_floating()
        assert session.layout.placements[r1.id].position == Point(0.5, 0.5)
        assert session.layout.placements[r2.id].position == Point(2.5, 0.5)


class TestPinOnForeignWireSoftFloating:
    def test_pin_on_foreign_wire_soft_commit_removes_wire(
        self, board_view: BoardView, session: EditorSession
    ) -> None:
        # R1 at (0.5, 0.5), R2 at (3.5, 0.5). A wire connects R2.pin1 to a far
        # pad, with an intermediate vertex at (1.5, 0.5).  Net N1 includes R2.pin1
        # so the wire carries N1 — when R1 lands its pin1 at (1.5, 0.5) it's a
        # foreign-wire conflict (R1 has no net).
        r1 = _place(board_view, session, 0.5, 0.5)
        r2 = _place(board_view, session, 3.5, 0.5)
        wire = WireSegment(
            id=uuid.uuid4(),
            path=[Point(3.5, 0.5), Point(1.5, 0.5), Point(0.5, 1.5)],
            insulated=True, side="top",
        )
        session.layout.wires.append(wire)
        net_n = uuid.uuid4()
        session.schematic.nets[net_n] = Net(
            id=net_n, name="N1",
            pins={PinRef(component_id=r2.id, pin_number=1)},
        )

        board_view.set_tool(SelectTool())
        tool = board_view._active_tool
        assert isinstance(tool, SelectTool)
        # Drag R1 so pin 1 lands at (1.5, 0.5) — mid foreign wire.
        _press(tool, 0.5, 0.5)
        _move(tool, 1.5, 0.5)
        _release(tool, 1.5, 0.5)

        assert tool._is_floating()
        assert tool._floating_severity == DragSeverity.SOFT.value

        # Commit with Enter — wire is removed.
        _key(tool, Qt.Key.Key_Return)
        assert not tool._is_floating()
        assert wire.id not in {w.id for w in session.layout.wires}
        assert session.layout.placements[r1.id].position == Point(1.5, 0.5)


class TestMultiComponentAllOrNothing:
    def test_multi_drag_with_one_hard_conflict_revertable_atomically(
        self, board_view: BoardView, session: EditorSession
    ) -> None:
        # R1, R2 in selection, R3 alone.  Assign different nets to R1.pin1
        # and R3.pin1 so dragging R1 to R3's pad triggers HARD.
        r1 = _place(board_view, session, 0.5, 0.5)
        r2 = _place(board_view, session, 0.5, 2.5)
        r3 = _place(board_view, session, 2.5, 0.5)

        net_a = uuid.uuid4()
        net_b = uuid.uuid4()
        session.schematic.nets[net_a] = Net(
            id=net_a, name="VCC",
            pins={PinRef(component_id=r1.id, pin_number=1)},
        )
        session.schematic.nets[net_b] = Net(
            id=net_b, name="GND",
            pins={PinRef(component_id=r3.id, pin_number=1)},
        )

        board_view.set_tool(SelectTool())
        tool = board_view._active_tool
        assert isinstance(tool, SelectTool)
        tool._selected_ids = {r1.id, r2.id}

        orig_r1 = session.layout.placements[r1.id].position
        orig_r2 = session.layout.placements[r2.id].position
        orig_r3 = session.layout.placements[r3.id].position

        _press(tool, 0.5, 0.5)
        # Drag both so R1's pin1 lands at (2.5, 0.5) → DIFFERENT_NETS with R3.
        _move(tool, 2.5, 0.5)
        _release(tool, 2.5, 0.5)

        # HARD floating — both R1 and R2 in the floating set.
        assert tool._is_floating()
        assert tool._floating_severity == DragSeverity.HARD.value
        assert r1.id in tool._floating_ids
        assert r2.id in tool._floating_ids

        # [Esc] reverts BOTH.
        _key(tool, Qt.Key.Key_Escape)
        assert session.layout.placements[r1.id].position == orig_r1
        assert session.layout.placements[r2.id].position == orig_r2
        assert session.layout.placements[r3.id].position == orig_r3


class TestDoubleClickCommitsFloating:
    def test_double_click_commits_soft_floating(
        self, board_view: BoardView, session: EditorSession
    ) -> None:
        # Reuse the pin-on-foreign-wire scenario for SOFT and commit via
        # on_double_press (instead of [Enter]).
        r1 = _place(board_view, session, 0.5, 0.5)
        r2 = _place(board_view, session, 3.5, 0.5)
        wire = WireSegment(
            id=uuid.uuid4(),
            path=[Point(3.5, 0.5), Point(1.5, 0.5), Point(0.5, 1.5)],
            insulated=True, side="top",
        )
        session.layout.wires.append(wire)
        net_n = uuid.uuid4()
        session.schematic.nets[net_n] = Net(
            id=net_n, name="N1",
            pins={PinRef(component_id=r2.id, pin_number=1)},
        )

        board_view.set_tool(SelectTool())
        tool = board_view._active_tool
        assert isinstance(tool, SelectTool)
        _press(tool, 0.5, 0.5)
        _move(tool, 1.5, 0.5)
        _release(tool, 1.5, 0.5)
        assert tool._is_floating()

        # Double-click anywhere commits.
        committed = tool.on_double_press(QPointF(10.0, 10.0))
        assert committed is True
        assert not tool._is_floating()
        assert wire.id not in {w.id for w in session.layout.wires}
