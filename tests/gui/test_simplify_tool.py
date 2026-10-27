# SPDX-License-Identifier: MIT
"""Tests for SimplifyTool — SelectTool subclass with Shift+LPM draft/apply."""

from __future__ import annotations

import uuid
from typing import cast

from PySide6.QtCore import Qt

from protoboard.domain.commands.base import ComponentState
from protoboard.domain.layout.wire import Wire
from protoboard.geometry import Point
from protoboard.gui.board_view import BoardView
from protoboard.gui.session import EditorSession
from protoboard.gui.tools.gui_tools import SimplifyTool
from protoboard.gui.tools.simplify_preview import QtSimplifyPreview
from tests.gui.input_helpers import key_inp, pointer


def _draft_items(tool: SimplifyTool) -> list[object]:
    """The live draft-overlay items (via the Qt preview)."""
    pv = tool.draft_preview
    assert isinstance(pv, QtSimplifyPreview)
    return list(pv.items)


def _press(
    view: BoardView,
    x: float,
    y: float,
    *,
    shift: bool = False,
    button: Qt.MouseButton = Qt.MouseButton.LeftButton,
) -> None:
    tool = view.active_tool
    assert tool is not None
    mods = (
        Qt.KeyboardModifier.ShiftModifier if shift else Qt.KeyboardModifier.NoModifier
    )
    tool.on_press(pointer(x, y, button=int(button.value), mods=mods))


def _key(view: BoardView, key: Qt.Key) -> None:
    tool = view.active_tool
    assert tool is not None
    tool.on_key(key_inp(key))


def _add_l_shape_wire(session: EditorSession) -> uuid.UUID:
    """Insert an L-shape wire (0.5,0.5)→(2.5,0.5)→(2.5,2.5).

    The canonical replacement for the full path is the direct diagonal
    (0.5,0.5)→(2.5,2.5) — strictly shorter, so simplify will accept it.
    """
    w = Wire(
        id=uuid.uuid4(),
        path=[Point(0.5, 0.5), Point(2.5, 0.5), Point(2.5, 2.5)],
        insulated=True,
        side="top",
    )
    session.layout.wires.append(w)
    session.invalidate_graph()
    session.layout_changed.emit()
    return w.id


class TestSimplifyToolDraft:
    """Shift+LPM on a wire creates a draft (no commit yet)."""

    def test_shift_lpm_creates_draft_overlay(
        self,
        board_view: BoardView,
        session: EditorSession,
    ) -> None:
        _add_l_shape_wire(session)
        board_view.set_tool(SimplifyTool())
        tool = board_view.active_tool
        assert isinstance(tool, SimplifyTool)
        _press(board_view, 2.5, 0.5, shift=True)
        assert len(_draft_items(tool)) > 0
        # Wire NOT yet modified.
        assert session.layout.wires[0].path == [
            Point(0.5, 0.5),
            Point(2.5, 0.5),
            Point(2.5, 2.5),
        ]

    def test_plain_lpm_does_not_create_draft(
        self,
        board_view: BoardView,
        session: EditorSession,
    ) -> None:
        # Plain LPM (no Shift) → SelectTool behaviour (select), no draft.
        _add_l_shape_wire(session)
        board_view.set_tool(SimplifyTool())
        tool = board_view.active_tool
        assert isinstance(tool, SimplifyTool)
        _press(board_view, 2.5, 0.5)
        assert _draft_items(tool) == []

    def test_escape_clears_draft(
        self,
        board_view: BoardView,
        session: EditorSession,
    ) -> None:
        _add_l_shape_wire(session)
        board_view.set_tool(SimplifyTool())
        tool = board_view.active_tool
        assert isinstance(tool, SimplifyTool)
        _press(board_view, 2.5, 0.5, shift=True)
        _key(board_view, Qt.Key.Key_Escape)
        assert _draft_items(tool) == []

    def test_cancel_action_clears_draft(
        self, board_view: BoardView, session: EditorSession
    ) -> None:
        _add_l_shape_wire(session)
        board_view.set_tool(SimplifyTool())
        tool = board_view.active_tool
        assert isinstance(tool, SimplifyTool)
        _press(board_view, 2.5, 0.5, shift=True)
        assert _draft_items(tool)
        tool.command_table()["select-tool.action.cancel"]()
        assert _draft_items(tool) == []

    def test_plain_lpm_after_draft_clears_it(
        self,
        board_view: BoardView,
        session: EditorSession,
    ) -> None:
        _add_l_shape_wire(session)
        board_view.set_tool(SimplifyTool())
        tool = board_view.active_tool
        assert isinstance(tool, SimplifyTool)
        _press(board_view, 2.5, 0.5, shift=True)
        # Plain LPM (no shift) — falls through to SelectTool, clears draft.
        _press(board_view, 99.0, 99.0)
        assert _draft_items(tool) == []

    def test_deactivate_clears_draft(
        self,
        board_view: BoardView,
        session: EditorSession,
    ) -> None:
        _add_l_shape_wire(session)
        board_view.set_tool(SimplifyTool())
        tool = board_view.active_tool
        assert isinstance(tool, SimplifyTool)
        _press(board_view, 2.5, 0.5, shift=True)
        assert _draft_items(tool)
        from protoboard.gui.tools.gui_tools import SelectTool

        board_view.set_tool(SelectTool())
        assert _draft_items(tool) == []


class TestSimplifyToolApply:
    """Second Shift+LPM on the same wire commits the draft."""

    def test_second_shift_lpm_applies_draft(
        self,
        board_view: BoardView,
        session: EditorSession,
    ) -> None:
        _add_l_shape_wire(session)
        board_view.set_tool(SimplifyTool())
        # First Shift+LPM = draft.
        _press(board_view, 2.5, 0.5, shift=True)
        # Second Shift+LPM on the same wire = apply.
        _press(board_view, 2.5, 0.5, shift=True)
        new_wire = session.layout.wires[0]
        assert new_wire.path[0] == Point(0.5, 0.5)
        assert new_wire.path[-1] == Point(2.5, 2.5)
        coords = {(p.x, p.y) for p in new_wire.path}
        assert (1.5, 1.5) in coords  # diagonal pad-handle re-inserted
        assert (2.5, 0.5) not in coords  # old L-corner gone

    def test_apply_clears_draft_state(
        self,
        board_view: BoardView,
        session: EditorSession,
    ) -> None:
        _add_l_shape_wire(session)
        board_view.set_tool(SimplifyTool())
        tool = board_view.active_tool
        assert isinstance(tool, SimplifyTool)
        _press(board_view, 2.5, 0.5, shift=True)
        _press(board_view, 2.5, 0.5, shift=True)
        assert _draft_items(tool) == []

    def test_apply_undoable(
        self,
        board_view: BoardView,
        session: EditorSession,
    ) -> None:
        _add_l_shape_wire(session)
        original_path = list(session.layout.wires[0].path)
        board_view.set_tool(SimplifyTool())
        _press(board_view, 2.5, 0.5, shift=True)
        _press(board_view, 2.5, 0.5, shift=True)
        session.stack.undo(cast(ComponentState, session))
        assert len(session.layout.wires) == 1
        assert list(session.layout.wires[0].path) == original_path

    def test_shift_lpm_on_different_wire_replaces_draft(
        self,
        board_view: BoardView,
        session: EditorSession,
    ) -> None:
        # Two wires; draft for first, then Shift+LPM on second → new draft
        # for second (does NOT apply the first wire's draft).
        _add_l_shape_wire(session)
        # Second wire — different L-shape elsewhere.
        wb = Wire(
            id=uuid.uuid4(),
            path=[Point(0.5, 3.5), Point(2.5, 3.5), Point(2.5, 4.5)],
            insulated=True,
            side="top",
        )
        session.layout.wires.append(wb)
        session.invalidate_graph()
        session.layout_changed.emit()

        board_view.set_tool(SimplifyTool())
        tool = board_view.active_tool
        assert isinstance(tool, SimplifyTool)
        _press(board_view, 2.5, 0.5, shift=True)  # draft for wid_a
        assert _draft_items(tool)
        _press(board_view, 2.5, 3.5, shift=True)  # draft for wb
        assert _draft_items(tool)
        # Neither wire was committed yet.
        assert session.layout.wires[0].path == [
            Point(0.5, 0.5),
            Point(2.5, 0.5),
            Point(2.5, 2.5),
        ]
        assert session.layout.wires[1].path == [
            Point(0.5, 3.5),
            Point(2.5, 3.5),
            Point(2.5, 4.5),
        ]


class TestSimplifyToolNoopWire:
    def test_no_simplification_possible_keeps_draft_empty(
        self,
        board_view: BoardView,
        session: EditorSession,
    ) -> None:
        w = Wire(
            id=uuid.uuid4(),
            path=[Point(0.5, 0.5), Point(2.5, 2.5)],
            insulated=True,
            side="top",
        )
        session.layout.wires.append(w)
        session.invalidate_graph()
        session.layout_changed.emit()
        board_view.set_tool(SimplifyTool())
        tool = board_view.active_tool
        assert isinstance(tool, SimplifyTool)
        _press(board_view, 1.5, 1.5, shift=True)
        assert _draft_items(tool) == []
