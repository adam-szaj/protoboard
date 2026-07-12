# SPDX-License-Identifier: MIT
"""Tests for ArmedTransformTool — the armed rotate/move/flip as its own tool.

The armed transform was extracted out of SelectTool into a registered tool that
drives the controller's E3 armed API. These tests drive it through a real
BoardView/session: SelectTool arms it (swapping the active tool), then keys /
wheel / clicks go to the now-active ArmedTransformTool, and accept / cancel hand
control back to SelectTool.
"""

from __future__ import annotations

from PySide6.QtCore import Qt

from protoboard.domain.components.instance import ComponentInstance
from protoboard.domain.layout.layout import ROOT_GROUP_ID
from protoboard.gui.board_view import BoardView
from protoboard.gui.session import EditorSession
from protoboard.gui.tools.armed_transform_tool import ArmedTransformTool
from protoboard.gui.tools.place_tool import PlaceTool
from protoboard.gui.tools.select_tool import SelectTool
from tests.gui.input_helpers import key_inp, pointer


def _place(
    board_view: BoardView, session: EditorSession, x: float, y: float
) -> ComponentInstance:
    board_view.set_tool(PlaceTool("R"))
    tool = board_view._active_tool
    assert isinstance(tool, PlaceTool)
    tool.on_press(pointer(x, y))
    while tool._phase == "drag_pin":
        tool.on_press(pointer(x, y))
    return list(session.instances.values())[-1]


def _select(board_view: BoardView) -> SelectTool:
    board_view.set_tool(SelectTool())
    tool = board_view._active_tool
    assert isinstance(tool, SelectTool)
    return tool


# -- E4a smoke: the tool arms via the controller and carries its own state ----


def test_construct_and_activate_arms_via_controller(
    board_view: BoardView, session: EditorSession
) -> None:
    inst = _place(board_view, session, 0.5, 0.5)
    tool = ArmedTransformTool(
        frozenset({inst.id}), None,
        active_group_id=ROOT_GROUP_ID,
        mode="rotate", pivot=None, return_to=lambda: None,
    )
    board_view.set_tool(tool)
    # Activating arms a transform on the controller and exposes it.
    assert session.edit_controller.armed_transform is not None
    assert board_view._active_tool is tool
    assert tool.state == "move.float.rotate"


def test_deactivate_cancels_a_live_transform(
    board_view: BoardView, session: EditorSession
) -> None:
    inst = _place(board_view, session, 0.5, 0.5)
    tool = ArmedTransformTool(
        frozenset({inst.id}), None,
        active_group_id=ROOT_GROUP_ID,
        mode="rotate", pivot=None, return_to=lambda: None,
    )
    board_view.set_tool(tool)
    base = session.layout.placements[inst.id].rotation
    tool.on_key(key_inp(Qt.Key.Key_Up))  # step +90 off-stack
    assert session.layout.placements[inst.id].rotation == (base + 90) % 360
    # Switching away cancels the still-live transform → pose restored.
    board_view.set_tool(_select(board_view))
    assert session.edit_controller.armed_transform is None
    assert session.layout.placements[inst.id].rotation == base


def test_state_is_idle_when_not_armed() -> None:
    tool = ArmedTransformTool(
        frozenset(), None,
        active_group_id=ROOT_GROUP_ID,
        mode="rotate", pivot=None, return_to=lambda: None,
    )
    assert tool.state == "select.idle"
