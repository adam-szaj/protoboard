# SPDX-License-Identifier: MIT
"""Tests for SelectTool — selection, drag-move, rotate, delete, state management."""

from __future__ import annotations

import uuid

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
    *,
    insulated: bool = True,
    side: str = "top",
) -> uuid.UUID:
    """Add a Wire directly to the session layout; return its id."""
    from protoboard.domain.layout.wire import Wire
    from protoboard.geometry import Point as _Point

    wire = Wire(
        id=uuid.uuid4(),
        path=[_Point(x, y) for x, y in path],
        insulated=insulated,
        side=side,  # type: ignore[arg-type]
    )
    session.layout.wires.append(wire)
    return wire.id


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

    def test_rubber_band_reuses_item_across_moves(
        self, board_view: BoardView, session: EditorSession
    ) -> None:
        """Moves on a live rubber-band must mutate the existing scene
        item (setRect), not destroy-and-recreate it.  Saves an X11
        round-trip per move over SSH."""
        board_view.set_tool(SelectTool())
        tool = board_view._active_tool
        assert isinstance(tool, SelectTool)
        _press(tool, 0.0, 0.0)
        # Force apply once (throttle would otherwise defer).
        tool._apply_rubber_band(QPointF(1.0, 1.0))
        item_1 = tool._rubber_band_item
        assert item_1 is not None
        tool._apply_rubber_band(QPointF(2.0, 2.0))
        item_2 = tool._rubber_band_item
        # Same Python object — proves we didn't recreate it.
        assert item_1 is item_2
        # And the rect actually updated.
        assert item_2.rect().width() == 2.0
        _release(tool, 2.0, 2.0)

    def test_rubber_band_throttle_coalesces_moves(
        self, board_view: BoardView, session: EditorSession
    ) -> None:
        """Multiple `_update_rubber_band` calls inside one throttle window
        produce at most one scene item; the pending pt is the last one."""
        board_view.set_tool(SelectTool())
        tool = board_view._active_tool
        assert isinstance(tool, SelectTool)
        _press(tool, 0.0, 0.0)
        # Schedule three updates without letting the timer fire.
        tool._update_rubber_band(QPointF(0.5, 0.5))
        tool._update_rubber_band(QPointF(1.0, 1.0))
        tool._update_rubber_band(QPointF(2.0, 2.0))
        # Latest pt is parked, timer is armed, no apply happened yet.
        assert tool._pending_rubber_band_pt == QPointF(2.0, 2.0)
        assert tool._rubber_band_timer is not None
        assert tool._rubber_band_timer.isActive()
        assert tool._rubber_band_item is None
        # Fire the timer manually (avoids the qtbot.wait timing dance).
        tool._flush_rubber_band_update()
        # Item now exists with the latest pt's geometry.
        assert tool._rubber_band_item is not None
        assert tool._rubber_band_item.rect().bottomRight() == QPointF(2.0, 2.0)
        assert tool._pending_rubber_band_pt is None
        _release(tool, 2.0, 2.0)

    def test_clear_rubber_band_stops_timer(
        self, board_view: BoardView, session: EditorSession
    ) -> None:
        board_view.set_tool(SelectTool())
        tool = board_view._active_tool
        assert isinstance(tool, SelectTool)
        _press(tool, 0.0, 0.0)
        tool._update_rubber_band(QPointF(1.0, 1.0))
        assert tool._rubber_band_timer is not None
        assert tool._rubber_band_timer.isActive()
        tool._clear_rubber_band()
        assert not tool._rubber_band_timer.isActive()
        assert tool._pending_rubber_band_pt is None
        assert tool._rubber_band_item is None

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
        # Wire on pad-grid: (1.5,1.5) → (2.5,1.5).
        _add_wire(session, [(1.5, 1.5), (2.5, 1.5)])
        board_view.refresh()
        board_view.set_tool(SelectTool())
        tool = board_view._active_tool
        assert isinstance(tool, SelectTool)
        wire_id = session.layout.wires[0].id
        # RTL rubber-band to select wire.
        _press(tool, 3.5, 3.5)
        _move(tool, 0.5, 0.5)
        _release(tool, 0.5, 0.5)
        assert wire_id in tool._selected_wire_ids
        # Click on wire start pad and drag by (+1, +1).
        _press(tool, 1.5, 1.5)
        _move(tool, 2.5, 2.5)
        _release(tool, 2.5, 2.5)
        wire = session.layout.wires[0]
        assert wire.path[0] == Point(2.5, 2.5)
        assert wire.path[1] == Point(3.5, 2.5)

    def test_drag_wire_is_undoable(
        self, board_view: BoardView, session: EditorSession
    ) -> None:
        """Wire drag creates undo entry that restores original path."""
        _add_wire(session, [(1.5, 1.5), (2.5, 1.5)])
        board_view.refresh()
        board_view.set_tool(SelectTool())
        tool = board_view._active_tool
        assert isinstance(tool, SelectTool)
        wire_id = session.layout.wires[0].id
        _press(tool, 3.5, 3.5)
        _move(tool, 0.5, 0.5)
        _release(tool, 0.5, 0.5)
        assert wire_id in tool._selected_wire_ids
        _press(tool, 1.5, 1.5)
        _move(tool, 2.5, 2.5)
        _release(tool, 2.5, 2.5)
        session.stack.undo(session)
        wire = session.layout.wires[0]
        assert wire.path[0] == Point(1.5, 1.5)
        assert wire.path[1] == Point(2.5, 1.5)

    def test_mixed_drag_moves_both_component_and_wire(
        self, board_view: BoardView, session: EditorSession
    ) -> None:
        """Selecting component + wire and dragging moves both."""
        inst = _place_component(board_view, session, 1.5, 1.5)
        _add_wire(session, [(1.5, 3.5), (2.5, 3.5)])
        board_view.refresh()
        board_view.set_tool(SelectTool())
        tool = board_view._active_tool
        assert isinstance(tool, SelectTool)
        # RTL rubber-band to select both.
        _press(tool, 4.5, 4.5)
        _move(tool, 0.5, 0.5)
        _release(tool, 0.5, 0.5)
        assert inst.id in tool._selected_ids
        assert session.layout.wires[0].id in tool._selected_wire_ids
        # Drag from component position by (+1, 0).
        _press(tool, 1.5, 1.5)
        _move(tool, 2.5, 1.5)
        _release(tool, 2.5, 1.5)
        assert session.layout.placements[inst.id].position == Point(2.5, 1.5)
        wire = session.layout.wires[0]
        assert wire.path[0] == Point(2.5, 3.5)
        assert wire.path[1] == Point(3.5, 3.5)

    def test_wires_stay_selected_during_drag_from_bbox(
        self, board_view: BoardView, session: EditorSession
    ) -> None:
        """Regression: clicking inside component bbox (not near pin) must not
        deselect wires — wires should remain in selection and move with drag."""
        inst = _place_component(board_view, session, 1.5, 1.5)
        _add_wire(session, [(0.5, 3.5), (2.5, 3.5)])
        board_view.refresh()
        board_view.set_tool(SelectTool())
        tool = board_view._active_tool
        assert isinstance(tool, SelectTool)
        # RTL rubber-band to select both.
        _press(tool, 4.5, 4.5)
        _move(tool, 0.5, 0.5)
        _release(tool, 0.5, 0.5)
        wire_id = session.layout.wires[0].id
        assert inst.id in tool._selected_ids
        assert wire_id in tool._selected_wire_ids
        # Click in the middle of the component bbox — close to pin 1 so anchor
        # offset is well-defined.  SIL(2) rot=90: pin 1 at (1.5, 1.5).
        _press(tool, 1.5, 1.5)
        # Wires must still be selected after the press.
        assert wire_id in tool._selected_wire_ids
        # Drag by (+1, 0).
        _move(tool, 2.5, 1.5)
        _release(tool, 2.5, 1.5)
        wire = session.layout.wires[0]
        # Endpoints translated; intermediate pad-vertex (2.5, 3.5) inserted
        # by ReplaceWireFragmentsCommand's enrichment.
        assert wire.path[0] == Point(1.5, 3.5)
        assert wire.path[-1] == Point(3.5, 3.5)


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


class TestEscPopsSelectionLayers:
    """Esc semantics per docs/highlight-state-machine.txt: pop one
    layer at a time, mirroring SelectionFSM.step_esc.

    Layer order: NORMAL → HIGHLIGHT → SELECTION → MULTI_SELECTION.
    The first Esc drops multi-set (keeps primary); second drops
    primary (keeps auto-highlight); third drops highlight.
    """

    def test_esc_pops_multi_selection_only(
        self, board_view: BoardView, session: EditorSession
    ) -> None:
        # Build a synthetic MULTI_SELECTION state: primary + multi set.
        inst_a = _place_component(board_view, session, 0.5, 0.5)
        inst_b = _place_component(board_view, session, 1.5, 0.5)
        board_view.set_tool(SelectTool())
        tool = board_view._active_tool
        assert isinstance(tool, SelectTool)
        tool._select_primary(inst_a.id)
        tool._selected_ids.add(inst_a.id)
        tool._selected_ids.add(inst_b.id)
        assert tool._selected_id == inst_a.id
        assert len(tool._selected_ids) == 2

        _key(tool, Qt.Key.Key_Escape)
        # Multi cleared; primary survives.
        assert tool._selected_ids == set()
        assert tool._selected_id == inst_a.id

    def test_esc_pops_selection_only_after_multi(
        self, board_view: BoardView, session: EditorSession
    ) -> None:
        inst = _place_component(board_view, session, 0.5, 0.5)
        board_view.set_tool(SelectTool())
        tool = board_view._active_tool
        assert isinstance(tool, SelectTool)
        tool._select_primary(inst.id)

        # First Esc — at SELECTION layer (no multi); pops to HIGHLIGHT.
        _key(tool, Qt.Key.Key_Escape)
        assert tool._selected_id is None
        assert tool._selected_ids == set()

    def test_esc_three_times_returns_to_normal(
        self, board_view: BoardView, session: EditorSession
    ) -> None:
        inst_a = _place_component(board_view, session, 0.5, 0.5)
        inst_b = _place_component(board_view, session, 1.5, 0.5)
        board_view.set_tool(SelectTool())
        tool = board_view._active_tool
        assert isinstance(tool, SelectTool)
        # MULTI_SELECTION layer
        tool._select_primary(inst_a.id)
        tool._selected_ids = {inst_a.id, inst_b.id}

        _key(tool, Qt.Key.Key_Escape)   # multi → selection
        assert tool._selected_ids == set()
        assert tool._selected_id == inst_a.id

        _key(tool, Qt.Key.Key_Escape)   # selection → highlight
        assert tool._selected_id is None

        _key(tool, Qt.Key.Key_Escape)   # highlight → normal (no-op for selection fields)
        assert tool._selected_id is None
        assert tool._selected_ids == set()


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


class TestWireEditPadEnrichment:
    """Wires laid as shortcuts must materialise board-pad vertices on edit-time
    so hit-test can pick a pad pin in the middle of the shortcut.  Applies to
    both bare and insulated wires — vertex handles are what allow the user to
    grab the wire mid-path, regardless of side semantics."""

    def test_bare_shortcut_enriched_on_hit_test(
        self, board_view: BoardView, session: EditorSession
    ) -> None:
        # 5×5 perfboard from conftest — pads at (0.5..4.5, 0.5..4.5).
        wire_id = _add_wire(
            session, [(0.5, 0.5), (4.5, 0.5)], insulated=False
        )
        board_view.refresh()
        board_view.set_tool(SelectTool())
        tool = board_view._active_tool
        assert isinstance(tool, SelectTool)
        tool._selected_wire_id = wire_id

        # Trigger the helper directly (mimics what hit-test does on demand).
        tool._ensure_wire_ready_for_edit(wire_id)

        new_wire = next(w for w in session.layout.wires if w.id == wire_id)
        coords = [(p.x, p.y) for p in new_wire.path]
        assert coords == [
            (0.5, 0.5), (1.5, 0.5), (2.5, 0.5), (3.5, 0.5), (4.5, 0.5)
        ]

    def test_enrichment_is_idempotent(
        self, board_view: BoardView, session: EditorSession
    ) -> None:
        wire_id = _add_wire(
            session, [(0.5, 0.5), (4.5, 0.5)], insulated=False
        )
        board_view.refresh()
        board_view.set_tool(SelectTool())
        tool = board_view._active_tool
        assert isinstance(tool, SelectTool)

        tool._ensure_wire_ready_for_edit(wire_id)
        stack_depth_after_first = len(session.stack._undo)
        tool._ensure_wire_ready_for_edit(wire_id)
        stack_depth_after_second = len(session.stack._undo)
        # Second call must not push another undo step (path already enriched).
        assert stack_depth_after_first == stack_depth_after_second

    def test_insulated_wire_enriched_on_edit(
        self, board_view: BoardView, session: EditorSession
    ) -> None:
        # Insulated wires also need pad-vertex handles so the user can grab
        # them mid-path during edit.  AddWireCommand already enriches on
        # insert; this verifies the on-edit helper agrees.
        wire_id = _add_wire(
            session, [(0.5, 0.5), (4.5, 0.5)], insulated=True
        )
        board_view.refresh()
        board_view.set_tool(SelectTool())
        tool = board_view._active_tool
        assert isinstance(tool, SelectTool)

        tool._ensure_wire_ready_for_edit(wire_id)

        new_wire = next(w for w in session.layout.wires if w.id == wire_id)
        coords = [(p.x, p.y) for p in new_wire.path]
        assert coords == [
            (0.5, 0.5), (1.5, 0.5), (2.5, 0.5), (3.5, 0.5), (4.5, 0.5)
        ]

    def test_already_enriched_path_no_op(
        self, board_view: BoardView, session: EditorSession
    ) -> None:
        # Wire already lists every pad as a vertex — no command should be issued.
        wire_id = _add_wire(
            session,
            [(0.5, 0.5), (1.5, 0.5), (2.5, 0.5), (3.5, 0.5), (4.5, 0.5)],
            insulated=False,
        )
        board_view.refresh()
        board_view.set_tool(SelectTool())
        tool = board_view._active_tool
        assert isinstance(tool, SelectTool)

        baseline = len(session.stack._undo)
        tool._ensure_wire_ready_for_edit(wire_id)
        assert len(session.stack._undo) == baseline


class TestDeleteWireFragment:
    """Delete on a fragment cuts the wire into two disjoint pieces — the
    fragment vertices (start, end, and all interior) are removed, not just
    interior nodes."""

    def test_interior_fragment_cuts_wire_in_two(
        self, board_view: BoardView, session: EditorSession
    ) -> None:
        # Path: [p0 p1 p2 p3 p4].  Fragment start=1 end=3.  Interior
        # vertex p2 is dropped; boundary pads p1 and p3 are kept as the
        # endpoints of the two new pieces.
        # Expected pieces: [p0,p1] and [p3,p4].
        wire_id = _add_wire(
            session,
            [(0.5, 0.5), (1.5, 0.5), (2.5, 0.5), (3.5, 0.5), (4.5, 0.5)],
            insulated=True,
        )
        board_view.refresh()
        board_view.set_tool(SelectTool())
        tool = board_view._active_tool
        assert isinstance(tool, SelectTool)
        tool._selected_wire_id = wire_id
        tool._edit_start_wire_id = wire_id
        tool._edit_end_wire_id = wire_id
        tool._edit_start_idx = 1
        tool._edit_end_idx = 3

        tool._delete_wire_fragment()

        ids = [w.id for w in session.layout.wires]
        assert wire_id not in ids
        assert len(session.layout.wires) == 2
        paths = sorted(
            [[(p.x, p.y) for p in w.path] for w in session.layout.wires]
        )
        assert paths == sorted([
            [(0.5, 0.5), (1.5, 0.5)],
            [(3.5, 0.5), (4.5, 0.5)],
        ])

    def test_fragment_at_left_endpoint_keeps_right_piece(
        self, board_view: BoardView, session: EditorSession
    ) -> None:
        # Fragment start=0 end=2 — interior is just p1; boundary pads
        # p0 (idx 0) and p2 (idx 2).  Left piece [p0] has length 1 → dropped.
        # Right piece [p2,p3,p4] survives.
        wire_id = _add_wire(
            session,
            [(0.5, 0.5), (1.5, 0.5), (2.5, 0.5), (3.5, 0.5), (4.5, 0.5)],
            insulated=True,
        )
        board_view.refresh()
        board_view.set_tool(SelectTool())
        tool = board_view._active_tool
        assert isinstance(tool, SelectTool)
        tool._selected_wire_id = wire_id
        tool._edit_start_wire_id = wire_id
        tool._edit_end_wire_id = wire_id
        tool._edit_start_idx = 0
        tool._edit_end_idx = 2

        tool._delete_wire_fragment()

        ids = [w.id for w in session.layout.wires]
        assert wire_id not in ids
        assert len(session.layout.wires) == 1
        coords = [(p.x, p.y) for p in session.layout.wires[0].path]
        assert coords == [(2.5, 0.5), (3.5, 0.5), (4.5, 0.5)]

    def test_adjacent_fragment_only_drops_segment(
        self, board_view: BoardView, session: EditorSession
    ) -> None:
        # Fragment start=1 end=2 — no interior vertices, just removes the
        # segment between two adjacent vertices.  Both halves [p0,p1] and
        # [p2,p3,p4] survive.
        wire_id = _add_wire(
            session,
            [(0.5, 0.5), (1.5, 0.5), (2.5, 0.5), (3.5, 0.5), (4.5, 0.5)],
            insulated=True,
        )
        board_view.refresh()
        board_view.set_tool(SelectTool())
        tool = board_view._active_tool
        assert isinstance(tool, SelectTool)
        tool._selected_wire_id = wire_id
        tool._edit_start_wire_id = wire_id
        tool._edit_end_wire_id = wire_id
        tool._edit_start_idx = 1
        tool._edit_end_idx = 2

        tool._delete_wire_fragment()

        assert wire_id not in [w.id for w in session.layout.wires]
        assert len(session.layout.wires) == 2
        paths = sorted([
            [(p.x, p.y) for p in w.path] for w in session.layout.wires
        ])
        assert paths == sorted([
            [(0.5, 0.5), (1.5, 0.5)],
            [(2.5, 0.5), (3.5, 0.5), (4.5, 0.5)],
        ])

    def test_whole_wire_fragment_removes_wire(
        self, board_view: BoardView, session: EditorSession
    ) -> None:
        wire_id = _add_wire(
            session,
            [(0.5, 0.5), (1.5, 0.5), (2.5, 0.5)],
            insulated=True,
        )
        board_view.refresh()
        board_view.set_tool(SelectTool())
        tool = board_view._active_tool
        assert isinstance(tool, SelectTool)
        tool._selected_wire_id = wire_id
        tool._edit_start_wire_id = wire_id
        tool._edit_end_wire_id = wire_id
        tool._edit_start_idx = 0
        tool._edit_end_idx = 2

        tool._delete_wire_fragment()

        assert session.layout.wires == []


class TestComponentDragAnchorSnap:
    """Regression: component pin 1 must always land on a pad after drag,
    even when the component started off-grid (e.g. previous drag dragged it
    off the board area)."""

    def test_drag_snaps_pin1_to_pad_when_starting_off_grid(
        self, board_view: BoardView, session: EditorSession
    ) -> None:
        inst = _place_component(board_view, session, 1.5, 1.5)
        # Force the component off-grid (simulating a prior drag that left it
        # at a fractional position, e.g. cursor outside the board).
        pl = session.layout.placements[inst.id]
        from protoboard.geometry import Point as _P
        pl.position = _P(1.7, 1.3)

        board_view.set_tool(SelectTool())
        tool = board_view._active_tool
        assert isinstance(tool, SelectTool)
        # Click on the off-grid pin 1 and drag.
        _press(tool, 1.7, 1.3)
        _move(tool, 2.5, 2.5)
        _release(tool, 2.5, 2.5)

        new_pos = session.layout.placements[inst.id].position
        # Pin 1 must be on a pad centre (half-integer coords).
        assert (new_pos.x - 0.5) == int(new_pos.x - 0.5), (
            f"x={new_pos.x} is not on pad grid"
        )
        assert (new_pos.y - 0.5) == int(new_pos.y - 0.5), (
            f"y={new_pos.y} is not on pad grid"
        )

    def test_snap_fallback_is_grid_aligned_off_board(
        self, board_view: BoardView, session: EditorSession
    ) -> None:
        # Snap called far outside any pad must still return a half-integer
        # grid position — never a raw cursor value.
        board_view.refresh()
        from PySide6.QtCore import QPointF as _QPF
        snapped = board_view.snap(_QPF(50.0, 50.0))
        assert (snapped.x - 0.5) == int(snapped.x - 0.5), snapped
        assert (snapped.y - 0.5) == int(snapped.y - 0.5), snapped


class TestMergeFragmentsCommand:
    """User-triggered Merge Same-Side Fragments action.

    Builds wires with multiple Fragments (boundary at every via) directly in
    the layout, runs the command on the selection, and checks that adjacent
    same-side fragments fuse while real via boundaries (top↔bottom) survive.
    """

    @staticmethod
    def _add_multi_fragment_wire(
        session: EditorSession,
        fragments: list[tuple[list[tuple[float, float]], str]],
    ) -> uuid.UUID:
        """Insert a Wire built from an explicit list of (path, side) fragments."""
        from protoboard.domain.layout.wire import Fragment, Wire
        from protoboard.geometry import Point as _Point
        frag_list = [
            Fragment(
                path=[_Point(x, y) for x, y in pts],
                side=side,  # type: ignore[arg-type]
            )
            for pts, side in fragments
        ]
        wire = Wire(
            id=uuid.uuid4(),
            fragments=frag_list,
            insulated=True,
        )
        session.layout.wires.append(wire)
        return wire.id

    def test_same_side_fragments_fuse(
        self, board_view: BoardView, session: EditorSession,
    ) -> None:
        # Two top-side fragments sharing the boundary (2.5, 0.5).  Merge
        # should collapse them into a single fragment.
        wire_id = self._add_multi_fragment_wire(
            session,
            [
                ([(0.5, 0.5), (2.5, 0.5)], "top"),
                ([(2.5, 0.5), (4.5, 0.5)], "top"),
            ],
        )
        board_view.refresh()
        board_view.set_tool(SelectTool())
        tool = board_view._active_tool
        assert isinstance(tool, SelectTool)
        tool._selected_wire_id = wire_id
        tool._merge_wires_and_fragments(frozenset({wire_id}))

        new_wire = next(w for w in session.layout.wires if w.id == wire_id)
        assert len(new_wire.fragments) == 1
        assert new_wire.fragments[0].side == "top"
        assert [(p.x, p.y) for p in new_wire.fragments[0].path] == [
            (0.5, 0.5), (2.5, 0.5), (4.5, 0.5),
        ]

    def test_via_boundary_preserved(
        self, board_view: BoardView, session: EditorSession,
    ) -> None:
        # top → bottom → top: only the cross-side boundaries are real vias;
        # nothing should fuse here.
        wire_id = self._add_multi_fragment_wire(
            session,
            [
                ([(0.5, 0.5), (1.5, 0.5)], "top"),
                ([(1.5, 0.5), (2.5, 0.5)], "bottom"),
                ([(2.5, 0.5), (3.5, 0.5)], "top"),
            ],
        )
        board_view.refresh()
        board_view.set_tool(SelectTool())
        tool = board_view._active_tool
        assert isinstance(tool, SelectTool)
        tool._selected_wire_id = wire_id
        tool._merge_wires_and_fragments(frozenset({wire_id}))

        new_wire = next(w for w in session.layout.wires if w.id == wire_id)
        assert len(new_wire.fragments) == 3
        assert [f.side for f in new_wire.fragments] == ["top", "bottom", "top"]

    def test_single_fragment_wire_is_no_op(
        self, board_view: BoardView, session: EditorSession,
    ) -> None:
        # Wire that already has a single fragment — command must not push
        # a command onto the undo stack.
        wire_id = _add_wire(
            session, [(0.5, 0.5), (4.5, 0.5)], insulated=False
        )
        board_view.refresh()
        board_view.set_tool(SelectTool())
        tool = board_view._active_tool
        assert isinstance(tool, SelectTool)
        baseline_depth = len(session.stack._undo)
        tool._merge_wires_and_fragments(frozenset({wire_id}))
        assert len(session.stack._undo) == baseline_depth

    def test_merge_is_undoable(
        self, board_view: BoardView, session: EditorSession,
    ) -> None:
        wire_id = self._add_multi_fragment_wire(
            session,
            [
                ([(0.5, 0.5), (2.5, 0.5)], "top"),
                ([(2.5, 0.5), (4.5, 0.5)], "top"),
            ],
        )
        board_view.refresh()
        board_view.set_tool(SelectTool())
        tool = board_view._active_tool
        assert isinstance(tool, SelectTool)
        tool._merge_wires_and_fragments(frozenset({wire_id}))
        assert len(next(
            w for w in session.layout.wires if w.id == wire_id
        ).fragments) == 1

        session.stack.undo(session)
        assert len(next(
            w for w in session.layout.wires if w.id == wire_id
        ).fragments) == 2

    def test_empty_selection_no_op(
        self, board_view: BoardView, session: EditorSession,
    ) -> None:
        board_view.set_tool(SelectTool())
        tool = board_view._active_tool
        assert isinstance(tool, SelectTool)
        baseline_depth = len(session.stack._undo)
        tool._merge_wires_and_fragments(frozenset())
        assert len(session.stack._undo) == baseline_depth


class TestCtrlClickDeselect:
    """Ctrl+click on an already-selected component / wire toggles it off."""

    def test_ctrl_click_on_selected_component_removes_it(
        self, board_view: BoardView, session: EditorSession,
    ) -> None:
        inst1 = _place_component(board_view, session, 1.5, 1.5)
        inst2 = _place_component(board_view, session, 4.5, 4.5)
        board_view.set_tool(SelectTool())
        tool = board_view._active_tool
        assert isinstance(tool, SelectTool)
        # Build a 2-component multi-selection.
        _ctrl_press(tool, 1.5, 1.5)
        _ctrl_press(tool, 4.5, 4.5)
        selected = set(tool._selected_ids) | (
            {tool._selected_id} if tool._selected_id else set()
        )
        assert selected == {inst1.id, inst2.id}
        # Ctrl+click on the first one — it should drop out.
        _ctrl_press(tool, 1.5, 1.5)
        selected = set(tool._selected_ids) | (
            {tool._selected_id} if tool._selected_id else set()
        )
        assert selected == {inst2.id}

    def test_ctrl_click_on_primary_selected_component_drops_primary(
        self, board_view: BoardView, session: EditorSession,
    ) -> None:
        inst = _place_component(board_view, session, 1.5, 1.5)
        board_view.set_tool(SelectTool())
        tool = board_view._active_tool
        assert isinstance(tool, SelectTool)
        # First Ctrl+click promotes it to primary.
        _ctrl_press(tool, 1.5, 1.5)
        assert tool._selected_id == inst.id
        # Second Ctrl+click drops it entirely (was the only selection).
        _ctrl_press(tool, 1.5, 1.5)
        assert tool._selected_id is None
        assert inst.id not in tool._selected_ids

    def test_ctrl_click_on_multi_selected_wire_removes_it(
        self, board_view: BoardView, session: EditorSession,
    ) -> None:
        # Seed multi-selection: wid1 as primary (would normally come
        # from a plain click), wid2 added via Ctrl+click.  Ctrl+clicking
        # wid2 again should drop only wid2 — without re-selecting it via
        # ``_select_primary`` (the bug this fix addresses).
        wid1 = _add_wire(session, [(0.5, 1.5), (4.5, 1.5)])
        wid2 = _add_wire(session, [(0.5, 3.5), (4.5, 3.5)])
        board_view.refresh()
        board_view.set_tool(SelectTool())
        tool = board_view._active_tool
        assert isinstance(tool, SelectTool)
        tool._selected_wire_ids.add(wid1)
        tool._selected_wire_ids.add(wid2)
        # Drive the toggle branch directly (the wire-edit branch fires
        # only when the wire is *primary*; here both are multi-selected
        # without a primary).
        from PySide6.QtCore import QPointF as _QPF
        tool._hit_test_wire = lambda _pt: wid2  # type: ignore[method-assign]
        tool.on_press(
            QMouseEvent(
                QMouseEvent.Type.MouseButtonPress,
                _QPF(2.5, 3.5), _QPF(2.5, 3.5),
                Qt.MouseButton.LeftButton, Qt.MouseButton.LeftButton,
                Qt.KeyboardModifier.ControlModifier,
            ),
            _QPF(2.5, 3.5),
        )
        assert wid2 not in tool._selected_wire_ids
        assert wid1 in tool._selected_wire_ids
