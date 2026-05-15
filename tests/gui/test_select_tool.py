# SPDX-License-Identifier: MIT
"""Tests for SelectTool — selection, drag-move, rotate, delete, state management."""

from __future__ import annotations

from PySide6.QtCore import QPointF, Qt
from PySide6.QtGui import QKeyEvent, QMouseEvent

from protoboard.domain.components.instance import ComponentInstance
from protoboard.geometry import Point
from protoboard.gui.board_view import BoardView
from protoboard.gui.session import EditorSession
from protoboard.gui.tools.place_tool import PlaceTool
from protoboard.gui.tools.select_tool import SelectTool


def _place_component(
    board_view: BoardView, session: EditorSession, x: float, y: float
) -> ComponentInstance:
    """Place an R component and return the instance."""
    board_view.set_tool(PlaceTool("R"))
    tool = board_view._active_tool
    assert isinstance(tool, PlaceTool)
    ev = QMouseEvent(
        QMouseEvent.Type.MouseButtonPress,
        QPointF(x, y),
        QPointF(x, y),
        Qt.MouseButton.LeftButton,
        Qt.MouseButton.LeftButton,
        Qt.KeyboardModifier.NoModifier,
    )
    tool.on_press(ev, QPointF(x, y))
    # If the component has bendable pins, confirm each drag pin with a click at
    # the same position (accepts the default offset).
    while tool._phase == "drag_pin":
        tool.on_press(ev, QPointF(x, y))
    return list(session.instances.values())[-1]


def _press(tool: SelectTool, x: float, y: float) -> None:
    tool.on_press(
        QMouseEvent(
            QMouseEvent.Type.MouseButtonPress,
            QPointF(x, y),
            QPointF(x, y),
            Qt.MouseButton.LeftButton,
            Qt.MouseButton.LeftButton,
            Qt.KeyboardModifier.NoModifier,
        ),
        QPointF(x, y),
    )


def _move(tool: SelectTool, x: float, y: float) -> None:
    tool.on_move(
        QMouseEvent(
            QMouseEvent.Type.MouseMove,
            QPointF(x, y),
            QPointF(x, y),
            Qt.MouseButton.NoButton,
            Qt.MouseButton.LeftButton,
            Qt.KeyboardModifier.NoModifier,
        ),
        QPointF(x, y),
    )


def _release(tool: SelectTool, x: float, y: float) -> None:
    tool.on_release(
        QMouseEvent(
            QMouseEvent.Type.MouseButtonRelease,
            QPointF(x, y),
            QPointF(x, y),
            Qt.MouseButton.LeftButton,
            Qt.MouseButton.NoButton,
            Qt.KeyboardModifier.NoModifier,
        ),
        QPointF(x, y),
    )


def _key(tool: SelectTool, key: Qt.Key) -> None:
    tool.on_key(QKeyEvent(QKeyEvent.Type.KeyPress, key, Qt.KeyboardModifier.NoModifier))


class TestSelectToolSelection:
    def test_click_near_component_selects_it(
        self, board_view: BoardView, session: EditorSession
    ) -> None:
        inst = _place_component(board_view, session, 0.5, 0.5)
        board_view.set_tool(SelectTool())
        tool = board_view._active_tool
        assert isinstance(tool, SelectTool)
        _press(tool, 0.5, 0.5)
        assert tool._selected_id == inst.id

    def test_click_empty_area_deselects(
        self, board_view: BoardView, session: EditorSession
    ) -> None:
        _place_component(board_view, session, 0.5, 0.5)
        board_view.set_tool(SelectTool())
        tool = board_view._active_tool
        assert isinstance(tool, SelectTool)
        _press(tool, 0.5, 0.5)
        assert tool._selected_id is not None
        _press(tool, 10.0, 10.0)  # far from any component
        assert tool._selected_id is None

    def test_highlight_added_to_scene_on_select(
        self, board_view: BoardView, session: EditorSession
    ) -> None:
        _place_component(board_view, session, 0.5, 0.5)
        board_view.set_tool(SelectTool())
        tool = board_view._active_tool
        assert isinstance(tool, SelectTool)
        _press(tool, 0.5, 0.5)
        assert tool._highlight is not None


class TestSelectToolDrag:
    def test_drag_moves_component(
        self, board_view: BoardView, session: EditorSession
    ) -> None:
        inst = _place_component(board_view, session, 0.5, 0.5)
        board_view.set_tool(SelectTool())
        tool = board_view._active_tool
        assert isinstance(tool, SelectTool)
        _press(tool, 0.5, 0.5)
        _move(tool, 2.5, 0.5)
        _release(tool, 2.5, 0.5)
        placement = session.layout.placements[inst.id]
        assert placement.position == Point(2.5, 0.5)

    def test_drag_creates_undo_entry(
        self, board_view: BoardView, session: EditorSession
    ) -> None:
        _place_component(board_view, session, 0.5, 0.5)
        board_view.set_tool(SelectTool())
        tool = board_view._active_tool
        assert isinstance(tool, SelectTool)
        _press(tool, 0.5, 0.5)
        _move(tool, 2.5, 0.5)
        _release(tool, 2.5, 0.5)
        assert session.stack.can_undo()

    def test_drag_undo_restores_position(
        self, board_view: BoardView, session: EditorSession
    ) -> None:
        inst = _place_component(board_view, session, 0.5, 0.5)
        board_view.set_tool(SelectTool())
        tool = board_view._active_tool
        assert isinstance(tool, SelectTool)
        _press(tool, 0.5, 0.5)
        _move(tool, 2.5, 0.5)
        _release(tool, 2.5, 0.5)
        session.stack.undo(session)
        placement = session.layout.placements[inst.id]
        assert placement.position == Point(0.5, 0.5)

    def test_highlight_restored_after_drag(
        self, board_view: BoardView, session: EditorSession
    ) -> None:
        """BUG 5 regression: component must remain selected after drag+release."""
        inst = _place_component(board_view, session, 0.5, 0.5)
        board_view.set_tool(SelectTool())
        tool = board_view._active_tool
        assert isinstance(tool, SelectTool)
        _press(tool, 0.5, 0.5)
        _move(tool, 2.5, 0.5)
        _release(tool, 2.5, 0.5)
        assert tool._selected_id == inst.id

    def test_drag_state_reset_on_deactivate(
        self, board_view: BoardView, session: EditorSession
    ) -> None:
        """BUG 3 regression: _dragging must be False after deactivate."""
        _place_component(board_view, session, 0.5, 0.5)
        board_view.set_tool(SelectTool())
        tool = board_view._active_tool
        assert isinstance(tool, SelectTool)
        _press(tool, 0.5, 0.5)
        tool._dragging = True  # simulate mid-drag
        tool.deactivate()
        assert tool._dragging is False
        assert tool._drag_orig_positions == {}


class TestSelectToolRotate:
    def test_rotate_key_changes_rotation(
        self, board_view: BoardView, session: EditorSession
    ) -> None:
        inst = _place_component(board_view, session, 0.5, 0.5)
        board_view.set_tool(SelectTool())
        tool = board_view._active_tool
        assert isinstance(tool, SelectTool)
        _press(tool, 0.5, 0.5)
        _key(tool, Qt.Key.Key_R)
        placement = session.layout.placements[inst.id]
        assert placement.rotation == 90

    def test_rotate_is_undoable(
        self, board_view: BoardView, session: EditorSession
    ) -> None:
        inst = _place_component(board_view, session, 0.5, 0.5)
        board_view.set_tool(SelectTool())
        tool = board_view._active_tool
        assert isinstance(tool, SelectTool)
        _press(tool, 0.5, 0.5)
        _key(tool, Qt.Key.Key_R)
        session.stack.undo(session)
        placement = session.layout.placements[inst.id]
        assert placement.rotation == 0


class TestSelectToolDelete:
    def test_delete_removes_placement(
        self, board_view: BoardView, session: EditorSession
    ) -> None:
        _place_component(board_view, session, 0.5, 0.5)
        board_view.set_tool(SelectTool())
        tool = board_view._active_tool
        assert isinstance(tool, SelectTool)
        _press(tool, 0.5, 0.5)
        _key(tool, Qt.Key.Key_Delete)
        assert len(session.layout.placements) == 0

    def test_delete_clears_selection(
        self, board_view: BoardView, session: EditorSession
    ) -> None:
        _place_component(board_view, session, 0.5, 0.5)
        board_view.set_tool(SelectTool())
        tool = board_view._active_tool
        assert isinstance(tool, SelectTool)
        _press(tool, 0.5, 0.5)
        _key(tool, Qt.Key.Key_Delete)
        assert tool._selected_id is None


class TestSelectToolSceneCleared:
    def test_scene_cleared_drops_highlight_ref(
        self, board_view: BoardView, session: EditorSession
    ) -> None:
        """on_scene_cleared must drop the stale graphics item reference."""
        _place_component(board_view, session, 0.5, 0.5)
        board_view.set_tool(SelectTool())
        tool = board_view._active_tool
        assert isinstance(tool, SelectTool)
        _press(tool, 0.5, 0.5)
        assert tool._highlight is not None
        tool.on_scene_cleared()
        assert tool._highlight is None

    def test_scene_cleared_preserves_selected_id_for_drag(
        self, board_view: BoardView, session: EditorSession
    ) -> None:
        """_selected_id must survive on_scene_cleared so drag can commit."""
        inst = _place_component(board_view, session, 0.5, 0.5)
        board_view.set_tool(SelectTool())
        tool = board_view._active_tool
        assert isinstance(tool, SelectTool)
        _press(tool, 0.5, 0.5)
        assert tool._selected_id == inst.id
        tool.on_scene_cleared()
        assert tool._selected_id == inst.id  # must survive for drag commit


# ---------------------------------------------------------------------------
# Helpers for wire-related tests
# ---------------------------------------------------------------------------

def _ctrl_press(tool: SelectTool, x: float, y: float) -> None:
    tool.on_press(
        QMouseEvent(
            QMouseEvent.Type.MouseButtonPress,
            QPointF(x, y),
            QPointF(x, y),
            Qt.MouseButton.LeftButton,
            Qt.MouseButton.LeftButton,
            Qt.KeyboardModifier.ControlModifier,
        ),
        QPointF(x, y),
    )


def _add_wire(
    session: EditorSession,
    path: list[tuple[float, float]],
) -> None:
    """Add a WireSegment directly to the session layout."""
    import uuid as _uuid

    from protoboard.domain.layout.wire import WireSegment
    from protoboard.geometry import Point as _Point

    session.layout.wires.append(
        WireSegment(
            id=_uuid.uuid4(),
            path=[_Point(x, y) for x, y in path],
            insulated=True,
            side="top",
        )
    )


# ---------------------------------------------------------------------------
# Multi-selection drag (components + wires)
# ---------------------------------------------------------------------------


class TestMultiSelectionDrag:
    """Tests for rubber-band selection + drag of mixed components/wires."""

    def test_rubber_band_left_to_right_selects_components_inside(
        self, board_view: BoardView, session: EditorSession
    ) -> None:
        """LTR rubber-band: only fully-contained components are selected."""
        i1 = _place_component(board_view, session, 1.5, 1.5)
        i2 = _place_component(board_view, session, 4.5, 4.5)
        board_view.set_tool(SelectTool())
        tool = board_view._active_tool
        assert isinstance(tool, SelectTool)
        # LTR drag from (0,0) to (3,3) — i1 bbox inside, i2 bbox outside.
        _press(tool, 0.0, 0.0)
        _move(tool, 3.0, 3.0)
        _release(tool, 3.0, 3.0)
        assert i1.id in tool._selected_ids
        assert i2.id not in tool._selected_ids

    def test_rubber_band_right_to_left_selects_touching_components(
        self, board_view: BoardView, session: EditorSession
    ) -> None:
        """RTL rubber-band: components that touch the rect are selected."""
        i1 = _place_component(board_view, session, 1.5, 1.5)
        board_view.set_tool(SelectTool())
        tool = board_view._active_tool
        assert isinstance(tool, SelectTool)
        # RTL drag: start x > end x.
        _press(tool, 3.0, 3.0)
        _move(tool, 0.0, 0.0)
        _release(tool, 0.0, 0.0)
        assert i1.id in tool._selected_ids

    def test_rubber_band_selects_wires(
        self, board_view: BoardView, session: EditorSession
    ) -> None:
        """Rubber-band includes wires whose points fall inside the rect."""
        _add_wire(session, [(1.0, 1.0), (2.0, 1.0)])
        board_view.refresh()
        board_view.set_tool(SelectTool())
        tool = board_view._active_tool
        assert isinstance(tool, SelectTool)
        wire_id = session.layout.wires[0].id
        # RTL drag to include the wire points.
        _press(tool, 3.0, 3.0)
        _move(tool, 0.0, 0.0)
        _release(tool, 0.0, 0.0)
        assert wire_id in tool._selected_wire_ids

    def test_drag_component_in_multi_selection_moves_all(
        self, board_view: BoardView, session: EditorSession
    ) -> None:
        """After rubber-band, dragging one selected component moves all."""
        i1 = _place_component(board_view, session, 1.5, 1.5)
        i2 = _place_component(board_view, session, 2.5, 1.5)
        board_view.set_tool(SelectTool())
        tool = board_view._active_tool
        assert isinstance(tool, SelectTool)
        # Select both with RTL rubber-band.
        _press(tool, 4.0, 3.0)
        _move(tool, 0.0, 0.0)
        _release(tool, 0.0, 0.0)
        assert i1.id in tool._selected_ids
        assert i2.id in tool._selected_ids
        # Now click on i1 and drag by (+2, 0).
        _press(tool, 1.5, 1.5)
        _move(tool, 3.5, 1.5)
        _release(tool, 3.5, 1.5)
        assert session.layout.placements[i1.id].position == Point(3.5, 1.5)
        assert session.layout.placements[i2.id].position == Point(4.5, 1.5)

    def test_drag_multi_selection_is_undoable(
        self, board_view: BoardView, session: EditorSession
    ) -> None:
        """Moving multi-selection creates a single undo entry."""
        i1 = _place_component(board_view, session, 1.5, 1.5)
        i2 = _place_component(board_view, session, 2.5, 1.5)
        board_view.set_tool(SelectTool())
        tool = board_view._active_tool
        assert isinstance(tool, SelectTool)
        _press(tool, 4.0, 3.0)
        _move(tool, 0.0, 0.0)
        _release(tool, 0.0, 0.0)
        _press(tool, 1.5, 1.5)
        _move(tool, 3.5, 1.5)
        _release(tool, 3.5, 1.5)
        # One undo entry for the whole move.
        stack_depth = len(session.stack._undo)
        session.stack.undo(session)
        assert len(session.stack._undo) == stack_depth - 1
        assert session.layout.placements[i1.id].position == Point(1.5, 1.5)
        assert session.layout.placements[i2.id].position == Point(2.5, 1.5)

    def test_drag_selected_wire_moves_wire(
        self, board_view: BoardView, session: EditorSession
    ) -> None:
        """Clicking a selected wire and dragging moves its path."""
        _add_wire(session, [(1.0, 1.0), (2.0, 1.0)])
        board_view.refresh()
        board_view.set_tool(SelectTool())
        tool = board_view._active_tool
        assert isinstance(tool, SelectTool)
        wire_id = session.layout.wires[0].id
        # RTL rubber-band to select wire.
        _press(tool, 3.0, 3.0)
        _move(tool, 0.0, 0.0)
        _release(tool, 0.0, 0.0)
        assert wire_id in tool._selected_wire_ids
        # Click near wire midpoint and drag by (+1, +1).
        _press(tool, 1.5, 1.0)
        _move(tool, 2.5, 2.0)
        _release(tool, 2.5, 2.0)
        wire = session.layout.wires[0]
        assert wire.path[0] == Point(2.0, 2.0)
        assert wire.path[1] == Point(3.0, 2.0)

    def test_drag_wire_is_undoable(
        self, board_view: BoardView, session: EditorSession
    ) -> None:
        """Wire drag creates undo entry that restores original path."""
        _add_wire(session, [(1.0, 1.0), (2.0, 1.0)])
        board_view.refresh()
        board_view.set_tool(SelectTool())
        tool = board_view._active_tool
        assert isinstance(tool, SelectTool)
        wire_id = session.layout.wires[0].id
        _press(tool, 3.0, 3.0)
        _move(tool, 0.0, 0.0)
        _release(tool, 0.0, 0.0)
        assert wire_id in tool._selected_wire_ids
        _press(tool, 1.5, 1.0)
        _move(tool, 2.5, 2.0)
        _release(tool, 2.5, 2.0)
        session.stack.undo(session)
        wire = session.layout.wires[0]
        assert wire.path[0] == Point(1.0, 1.0)
        assert wire.path[1] == Point(2.0, 1.0)

    def test_mixed_drag_moves_both_component_and_wire(
        self, board_view: BoardView, session: EditorSession
    ) -> None:
        """Selecting component + wire and dragging moves both."""
        inst = _place_component(board_view, session, 1.5, 1.5)
        _add_wire(session, [(1.0, 3.0), (2.0, 3.0)])
        board_view.refresh()
        board_view.set_tool(SelectTool())
        tool = board_view._active_tool
        assert isinstance(tool, SelectTool)
        # RTL rubber-band to select both.
        _press(tool, 4.0, 4.0)
        _move(tool, 0.0, 0.0)
        _release(tool, 0.0, 0.0)
        assert inst.id in tool._selected_ids
        assert session.layout.wires[0].id in tool._selected_wire_ids
        # Drag from component position by (+1, 0).
        _press(tool, 1.5, 1.5)
        _move(tool, 2.5, 1.5)
        _release(tool, 2.5, 1.5)
        assert session.layout.placements[inst.id].position == Point(2.5, 1.5)
        wire = session.layout.wires[0]
        assert wire.path[0] == Point(2.0, 3.0)
        assert wire.path[1] == Point(3.0, 3.0)

    def test_wires_stay_selected_during_drag_from_bbox(
        self, board_view: BoardView, session: EditorSession
    ) -> None:
        """Regression: clicking inside component bbox (not near pin) must not
        deselect wires — wires should remain in selection and move with drag."""
        inst = _place_component(board_view, session, 1.5, 1.5)
        _add_wire(session, [(0.5, 3.0), (2.5, 3.0)])
        board_view.refresh()
        board_view.set_tool(SelectTool())
        tool = board_view._active_tool
        assert isinstance(tool, SelectTool)
        # RTL rubber-band to select both.
        _press(tool, 4.0, 4.0)
        _move(tool, 0.0, 0.0)
        _release(tool, 0.0, 0.0)
        wire_id = session.layout.wires[0].id
        assert inst.id in tool._selected_ids
        assert wire_id in tool._selected_wire_ids
        # Click in the middle of the component bbox — far from pins.
        # SIL(2) rot=90: pins at (1.5, 1.5) and (2.5, 1.5); bbox centre ≈ (2, 1.5).
        # Click at (2.0, 1.5) — inside bbox but not within _HIT_RADIUS of any pin.
        _press(tool, 2.0, 1.5)
        # Wires must still be selected after the press.
        assert wire_id in tool._selected_wire_ids
        # Move and release — wire should move together with component.
        _move(tool, 3.0, 1.5)
        _release(tool, 3.0, 1.5)
        wire = session.layout.wires[0]
        assert wire.path[0] == Point(1.5, 3.0)
        assert wire.path[1] == Point(3.5, 3.0)


# ---------------------------------------------------------------------------
# Group navigation
# ---------------------------------------------------------------------------

class TestSelectToolGroupNavigation:
    """Tests for _group_stack management (enter/exit group)."""

    def test_initial_stack_contains_root_group(
        self, board_view: BoardView, session: EditorSession
    ) -> None:
        from protoboard.domain.layout.layout import ROOT_GROUP_ID
        board_view.set_tool(SelectTool())
        tool = board_view._active_tool
        assert isinstance(tool, SelectTool)
        assert tool._group_stack == [ROOT_GROUP_ID]

    def test_esc_never_pops_root_group(
        self, board_view: BoardView, session: EditorSession
    ) -> None:
        from protoboard.domain.layout.layout import ROOT_GROUP_ID
        board_view.set_tool(SelectTool())
        tool = board_view._active_tool
        assert isinstance(tool, SelectTool)
        _key(tool, Qt.Key.Key_Escape)
        assert tool._group_stack == [ROOT_GROUP_ID]

    def test_enter_group_pushes_to_stack(
        self, board_view: BoardView, session: EditorSession
    ) -> None:
        import uuid as _uuid
        from protoboard.domain.layout.group import ComponentGroup
        from protoboard.domain.layout.layout import ROOT_GROUP_ID
        board_view.set_tool(SelectTool())
        tool = board_view._active_tool
        assert isinstance(tool, SelectTool)

        grp_id = _uuid.uuid4()
        session.layout.groups[grp_id] = ComponentGroup(
            id=grp_id, name="G1",
            component_ids=frozenset(),
            child_group_ids=frozenset(),
            exported_pins=frozenset(),
        )
        # Add group to root.
        root = session.layout.groups[ROOT_GROUP_ID]
        session.layout.groups[ROOT_GROUP_ID] = ComponentGroup(
            id=ROOT_GROUP_ID, name=root.name,
            component_ids=root.component_ids,
            child_group_ids=root.child_group_ids | {grp_id},
            exported_pins=root.exported_pins,
        )
        # Manually trigger group entry (simulates double-click on group).
        tool._group_stack.append(grp_id)
        assert tool._group_stack[-1] == grp_id

        # Esc should pop back to root.
        _key(tool, Qt.Key.Key_Escape)
        assert tool._group_stack == [ROOT_GROUP_ID]

    def test_esc_pops_one_level_at_a_time(
        self, board_view: BoardView, session: EditorSession
    ) -> None:
        import uuid as _uuid
        from protoboard.domain.layout.group import ComponentGroup
        from protoboard.domain.layout.layout import ROOT_GROUP_ID
        board_view.set_tool(SelectTool())
        tool = board_view._active_tool
        assert isinstance(tool, SelectTool)

        g1 = _uuid.uuid4()
        g2 = _uuid.uuid4()
        for gid, name in [(g1, "G1"), (g2, "G2")]:
            session.layout.groups[gid] = ComponentGroup(
                id=gid, name=name,
                component_ids=frozenset(),
                child_group_ids=frozenset(),
                exported_pins=frozenset(),
            )
        tool._group_stack.extend([g1, g2])
        assert len(tool._group_stack) == 3  # root, g1, g2

        _key(tool, Qt.Key.Key_Escape)
        assert tool._group_stack[-1] == g1

        _key(tool, Qt.Key.Key_Escape)
        assert tool._group_stack == [ROOT_GROUP_ID]


# ---------------------------------------------------------------------------
# _hit_test_pin / iter_pin_positions integration
# ---------------------------------------------------------------------------

class TestHitTestPin:
    def test_hit_test_pin_finds_pin_near_cursor(
        self, board_view: BoardView, session: EditorSession
    ) -> None:
        inst = _place_component(board_view, session, 0.5, 0.5)
        board_view.refresh()
        board_view.set_tool(SelectTool())
        tool = board_view._active_tool
        assert isinstance(tool, SelectTool)
        # Pin 1 of SIL(2) at rotation=90 is at (0.5, 0.5).
        result = tool._hit_test_pin(QPointF(0.5, 0.5))
        assert result is not None
        assert result[0] == inst.id

    def test_hit_test_pin_returns_none_far_from_pins(
        self, board_view: BoardView, session: EditorSession
    ) -> None:
        _place_component(board_view, session, 0.5, 0.5)
        board_view.refresh()
        board_view.set_tool(SelectTool())
        tool = board_view._active_tool
        assert isinstance(tool, SelectTool)
        result = tool._hit_test_pin(QPointF(10.0, 10.0))
        assert result is None

    def test_hit_test_pin_for_comp_finds_nearest_pin(
        self, board_view: BoardView, session: EditorSession
    ) -> None:
        inst = _place_component(board_view, session, 0.5, 0.5)
        board_view.refresh()
        board_view.set_tool(SelectTool())
        tool = board_view._active_tool
        assert isinstance(tool, SelectTool)
        # Should find a pin for this specific component.
        result = tool._hit_test_pin_for_comp(QPointF(0.5, 0.5), inst.id)
        assert result is not None
        assert result[0] == inst.id

    def test_hit_test_pin_for_comp_returns_none_for_unknown_id(
        self, board_view: BoardView, session: EditorSession
    ) -> None:
        import uuid as _uuid
        board_view.set_tool(SelectTool())
        tool = board_view._active_tool
        assert isinstance(tool, SelectTool)
        result = tool._hit_test_pin_for_comp(QPointF(0.5, 0.5), _uuid.uuid4())
        assert result is None


# ---------------------------------------------------------------------------
# Wire selection cycling
# ---------------------------------------------------------------------------

class TestWireClickCycle:
    def test_clicking_wire_selects_it(
        self, board_view: BoardView, session: EditorSession
    ) -> None:
        _add_wire(session, [(0.5, 1.0), (2.5, 1.0)])
        board_view.refresh()
        board_view.set_tool(SelectTool())
        tool = board_view._active_tool
        assert isinstance(tool, SelectTool)
        wire_id = session.layout.wires[0].id
        _press(tool, 1.5, 1.0)
        assert tool._selected_wire_id == wire_id

    def test_deselect_resets_cycle(
        self, board_view: BoardView, session: EditorSession
    ) -> None:
        _add_wire(session, [(0.5, 1.0), (2.5, 1.0)])
        board_view.refresh()
        board_view.set_tool(SelectTool())
        tool = board_view._active_tool
        assert isinstance(tool, SelectTool)
        _press(tool, 1.5, 1.0)
        # Click far away to deselect.
        _press(tool, 10.0, 10.0)
        assert tool._selected_wire_id is None
        assert tool._wire_click_cycle == 0
