# SPDX-License-Identifier: MIT
"""Context-menu cancellation follows the keyboard tool lifecycle."""

from protoboard.controller.ports import ActionOption
from protoboard.gui.session import EditorSession
from protoboard.gui.tools.gui_tools import PlaceTool, RouteTool
from tests.fakes.board_view import HeadlessBoardView
from tests.gui.input_helpers import pointer


def test_context_cancel_exits_place_before_first_placement(
    session: EditorSession,
) -> None:
    view = HeadlessBoardView(session)
    view.tools.set_tool(PlaceTool("R"))

    view.tools.perform_context_action(
        ActionOption(action_id="global.action.cancel", label="Cancel")
    )

    assert view.notifications.escapes == 1
    assert not session.layout.placements
    assert not session.instances
    assert not session.stack.can_undo()


def test_context_cancel_reverts_route_before_exiting(session: EditorSession) -> None:
    view = HeadlessBoardView(session)
    tool = RouteTool()
    view.tools.set_tool(tool)
    tool.on_press(pointer(0.5, 0.5))
    tool.on_press(pointer(0.5, 1.5))
    cancel = ActionOption(action_id="global.action.cancel", label="Cancel")

    view.tools.perform_context_action(cancel)

    assert not tool._points
    assert not session.layout.wires
    assert view.notifications.escapes == 0
    assert view.active_tool is tool

    view.tools.perform_context_action(cancel)

    assert view.notifications.escapes == 1
