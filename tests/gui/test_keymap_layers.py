# SPDX-License-Identifier: MIT
"""ToolHost lifecycle of the per-tool KeyMap layer (push on activate,
pop on deactivate) — docs/keymap-per-tool-layer.md."""

from __future__ import annotations

from protoboard.gui.board_view import BoardView
from protoboard.gui.board_view_tool import _Tool


class _ToolA(_Tool):
    def key_actions(self) -> dict[str, str | None]:
        return {"a-tool.action.foo": "K"}


class _ToolB(_Tool):
    def key_actions(self) -> dict[str, str | None]:
        return {"b-tool.action.bar": "J"}


class TestToolLayerLifecycle:
    def test_layer_pushed_on_activate(self, board_view: BoardView) -> None:
        board_view.set_tool(_ToolA())
        assert board_view.keymap.resolve("a-tool.action.foo") == "K"

    def test_layer_popped_on_tool_switch(self, board_view: BoardView) -> None:
        board_view.set_tool(_ToolA())
        board_view.set_tool(_ToolB())
        # The previous tool's binding is gone, the new tool's is live.
        assert board_view.keymap.resolve("a-tool.action.foo") == ""
        assert board_view.keymap.resolve("b-tool.action.bar") == "J"

    def test_layer_popped_on_none(self, board_view: BoardView) -> None:
        board_view.set_tool(_ToolA())
        board_view.set_tool(None)
        assert board_view.keymap.resolve("a-tool.action.foo") == ""

    def test_layer_popped_on_detach(self, board_view: BoardView) -> None:
        board_view.set_tool(_ToolA())
        board_view.tools.detach_active_tool()
        assert board_view.keymap.resolve("a-tool.action.foo") == ""

    def test_override_survives_relayering(self, board_view: BoardView) -> None:
        # Rebind through the head while the tool is active; pop + re-push must
        # keep honouring it (the store lives on the base map).
        board_view.set_tool(_ToolA())
        board_view.keymap.set_override("a-tool.action.foo", "M")
        board_view.set_tool(_ToolB())
        board_view.set_tool(_ToolA())
        assert board_view.keymap.resolve("a-tool.action.foo") == "M"

    def test_globals_resolve_with_and_without_tool(
        self, board_view: BoardView
    ) -> None:
        assert board_view.keymap.resolve("global.action.edit") == "E"
        board_view.set_tool(_ToolA())
        assert board_view.keymap.resolve("global.action.edit") == "E"
