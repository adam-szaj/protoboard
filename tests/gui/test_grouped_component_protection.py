# SPDX-License-Identifier: MIT
"""Grouped-component protection: from outside the group, components inside
are selectable (for net inspection / properties) but their layout cannot be
mutated.  Entering the group via double-click unlocks them.
"""

from __future__ import annotations

import uuid

from PySide6.QtCore import QPointF, Qt
from PySide6.QtGui import QMouseEvent

from protoboard.domain.components.instance import ComponentInstance
from protoboard.domain.layout.group import ComponentGroup
from protoboard.domain.layout.layout import ROOT_GROUP_ID
from protoboard.gui.board_view import BoardView
from protoboard.gui.selection_axes import SelectionFilter
from protoboard.gui.session import EditorSession
from protoboard.gui.tools.place_tool import PlaceTool
from protoboard.gui.tools.select_tool import SelectTool


def _place(board_view: BoardView, session: EditorSession, x: float, y: float) -> ComponentInstance:
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


def _press(tool: SelectTool, x: float, y: float, modifiers: Qt.KeyboardModifier = Qt.KeyboardModifier.NoModifier) -> None:
    tool.on_press(
        QMouseEvent(
            QMouseEvent.Type.MouseButtonPress,
            QPointF(x, y), QPointF(x, y),
            Qt.MouseButton.LeftButton, Qt.MouseButton.LeftButton,
            modifiers,
        ),
        QPointF(x, y),
    )


def _make_group(session: EditorSession, member_ids: list[uuid.UUID]) -> uuid.UUID:
    """Wrap member_ids in a new (non-root) group that's a child of ROOT."""
    grp_id = uuid.uuid4()
    session.layout.groups[grp_id] = ComponentGroup(
        id=grp_id, name="G1",
        component_ids=frozenset(member_ids),
        child_group_ids=frozenset(),
        exported_pins=frozenset(),
    )
    root = session.layout.groups[ROOT_GROUP_ID]
    session.layout.groups[ROOT_GROUP_ID] = ComponentGroup(
        id=ROOT_GROUP_ID, name=root.name,
        component_ids=root.component_ids - frozenset(member_ids),
        child_group_ids=root.child_group_ids | {grp_id},
        exported_pins=root.exported_pins,
    )
    return grp_id


class TestSelectionAllowedForGrouped:
    def test_click_selects_grouped_component_with_groups_off(
        self, board_view: BoardView, session: EditorSession
    ) -> None:
        """With GROUPS off, clicking a component inside a group still
        selects it (for net highlight / properties); only drag is blocked."""
        inst = _place(board_view, session, 0.5, 0.5)
        _make_group(session, [inst.id])
        board_view.set_tool(SelectTool())
        tool = board_view._active_tool
        assert isinstance(tool, SelectTool)
        tool.set_filter(SelectionFilter.COMPONENTS | SelectionFilter.WIRES)
        _press(tool, 0.5, 0.5)
        assert tool._selected_id == inst.id


class TestDragBlocked:
    def test_press_on_grouped_component_arms_group_drag(
        self, board_view: BoardView, session: EditorSession
    ) -> None:
        """Click on a component inside a non-active group arms a
        group-level drag (per the 2026-06-01 group selection refactor).

        Previously the click was blocked entirely; now it expands
        selection to the whole group and arms drag — a subsequent
        cursor motion beyond the threshold moves the group, while a
        plain release leaves the group selected but unmoved.  Clicking
        a single component without crossing the drag threshold is
        effectively "select group", not "modify component".
        """
        inst = _place(board_view, session, 0.5, 0.5)
        gid = _make_group(session, [inst.id])
        board_view.set_tool(SelectTool())
        tool = board_view._active_tool
        assert isinstance(tool, SelectTool)
        tool.set_filter(SelectionFilter.COMPONENTS | SelectionFilter.WIRES)
        _press(tool, 0.5, 0.5)
        # Selection expanded to the whole group.
        assert inst.id in tool._selected_ids
        assert tool._selected_group_id == gid
        # Drag is armed — but cursor hasn't moved, so no mutation yet.
        assert tool._dragging is True
        assert tool._drag_threshold_crossed is False

    def test_drag_starts_after_entering_group(
        self, board_view: BoardView, session: EditorSession
    ) -> None:
        inst = _place(board_view, session, 0.5, 0.5)
        gid = _make_group(session, [inst.id])
        board_view.set_tool(SelectTool())
        tool = board_view._active_tool
        assert isinstance(tool, SelectTool)
        tool.set_filter(SelectionFilter.COMPONENTS | SelectionFilter.WIRES)
        # Step inside the group (simulates double-click flow).
        tool._group_stack.append(gid)
        _press(tool, 0.5, 0.5)
        assert tool._dragging is True


class TestDeleteRotateBlocked:
    def test_delete_skipped_for_grouped(
        self, board_view: BoardView, session: EditorSession
    ) -> None:
        inst = _place(board_view, session, 0.5, 0.5)
        _make_group(session, [inst.id])
        board_view.set_tool(SelectTool())
        tool = board_view._active_tool
        assert isinstance(tool, SelectTool)
        tool._selected_id = inst.id
        tool._delete_selected()
        # Component still present — delete was vetoed.
        assert inst.id in session.layout.placements

    def test_rotate_skipped_for_grouped(
        self, board_view: BoardView, session: EditorSession
    ) -> None:
        inst = _place(board_view, session, 0.5, 0.5)
        _make_group(session, [inst.id])
        original_rot = session.layout.placements[inst.id].rotation
        board_view.set_tool(SelectTool())
        tool = board_view._active_tool
        assert isinstance(tool, SelectTool)
        tool._selected_id = inst.id
        tool._rotate_selected(delta=90)
        assert session.layout.placements[inst.id].rotation == original_rot

    def test_multi_delete_filters_grouped(
        self, board_view: BoardView, session: EditorSession
    ) -> None:
        inst_free = _place(board_view, session, 0.5, 0.5)
        inst_grouped = _place(board_view, session, 1.5, 0.5)
        _make_group(session, [inst_grouped.id])
        board_view.set_tool(SelectTool())
        tool = board_view._active_tool
        assert isinstance(tool, SelectTool)
        tool._selected_ids = {inst_free.id, inst_grouped.id}
        tool._delete_multi()
        # Free one deleted, grouped one survives.
        assert inst_free.id not in session.layout.placements
        assert inst_grouped.id in session.layout.placements

    def test_multi_rotate_filters_grouped(
        self, board_view: BoardView, session: EditorSession
    ) -> None:
        inst_free = _place(board_view, session, 0.5, 0.5)
        inst_grouped = _place(board_view, session, 1.5, 0.5)
        _make_group(session, [inst_grouped.id])
        free_rot0 = session.layout.placements[inst_free.id].rotation
        grouped_rot0 = session.layout.placements[inst_grouped.id].rotation
        board_view.set_tool(SelectTool())
        tool = board_view._active_tool
        assert isinstance(tool, SelectTool)
        tool._selected_ids = {inst_free.id, inst_grouped.id}
        tool._rotate_multi(delta=90)
        # Free one rotated, grouped one untouched.
        assert session.layout.placements[inst_free.id].rotation == (free_rot0 + 90) % 360
        assert session.layout.placements[inst_grouped.id].rotation == grouped_rot0


class TestUnlockedAfterEnter:
    def test_delete_works_after_entering_group(
        self, board_view: BoardView, session: EditorSession
    ) -> None:
        inst = _place(board_view, session, 0.5, 0.5)
        gid = _make_group(session, [inst.id])
        board_view.set_tool(SelectTool())
        tool = board_view._active_tool
        assert isinstance(tool, SelectTool)
        tool._group_stack.append(gid)
        tool._selected_id = inst.id
        tool._delete_selected()
        assert inst.id not in session.layout.placements
