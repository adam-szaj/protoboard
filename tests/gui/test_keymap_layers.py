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


class _DispatchTool(_Tool):
    """Two actions on one trigger — first table entry decides."""

    def __init__(self) -> None:
        super().__init__()
        self.calls: list[str] = []
        self.first_consumes = True

    def key_actions(self) -> dict[str, str | None]:
        return {"d-tool.action.first": "K", "d-tool.action.second": "K"}

    def key_dispatch(self):  # type: ignore[override]
        return (
            ("d-tool.action.first", self._on_first),
            ("d-tool.action.second", self._on_second),
        )

    def _on_first(self, _event) -> bool:
        self.calls.append("first")
        return self.first_consumes

    def _on_second(self, _event) -> bool:
        self.calls.append("second")
        return True


def _key_event(key):
    from PySide6.QtCore import QEvent, Qt
    from PySide6.QtGui import QKeyEvent
    return QKeyEvent(QEvent.Type.KeyPress, key, Qt.KeyboardModifier.NoModifier)


class TestDispatchTable:
    def test_first_matching_entry_wins(self, board_view: BoardView) -> None:
        tool = _DispatchTool()
        board_view.set_tool(tool)
        from PySide6.QtCore import Qt
        event = _key_event(Qt.Key.Key_K)
        assert tool._dispatch_key(event) is True
        assert tool.calls == ["first"]
        assert event.isAccepted()

    def test_declining_handler_ends_the_scan(
        self, board_view: BoardView
    ) -> None:
        # elif-ladder parity: the first id match decides; a declined key is
        # NOT offered to later entries sharing the trigger.
        tool = _DispatchTool()
        tool.first_consumes = False
        board_view.set_tool(tool)
        from PySide6.QtCore import Qt
        assert tool._dispatch_key(_key_event(Qt.Key.Key_K)) is False
        assert tool.calls == ["first"]

    def test_unbound_key_dispatches_nothing(
        self, board_view: BoardView
    ) -> None:
        tool = _DispatchTool()
        board_view.set_tool(tool)
        from PySide6.QtCore import Qt
        assert tool._dispatch_key(_key_event(Qt.Key.Key_J)) is False
        assert tool.calls == []
