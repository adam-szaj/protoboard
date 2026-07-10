# SPDX-License-Identifier: MIT
"""ToolHost lifecycle of the per-tool KeyMap layer (push on activate,
pop on deactivate) — docs/keymap-per-tool-layer.md."""

from __future__ import annotations

from protoboard.gui.board_view import BoardView
from protoboard.gui.board_view_tool import _Tool
from tests.gui.input_helpers import pointer


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
    from tests.gui.input_helpers import key_inp
    return key_inp(key)


def _mouse_press(button, modifiers=None):
    from PySide6.QtCore import QEvent, QPointF, Qt
    from PySide6.QtGui import QMouseEvent
    return QMouseEvent(
        QEvent.Type.MouseButtonPress, QPointF(1, 1), QPointF(1, 1),
        button, button, modifiers or Qt.KeyboardModifier.NoModifier,
    )


class TestMouseChordRouting:
    def test_middle_button_starts_pan(self, board_view: BoardView) -> None:
        from PySide6.QtCore import Qt
        host = board_view.tools
        assert host._panning is False
        assert host.handle_mouse_press(
            _mouse_press(Qt.MouseButton.MiddleButton)) is True
        assert host._panning is True
        assert host._ctrl_panning is False

    def test_ctrl_lmb_pans_when_tool_declines(
        self, board_view: BoardView
    ) -> None:
        from PySide6.QtCore import Qt
        board_view.set_tool(_ToolA())  # declines every press (default False)
        host = board_view.tools
        assert host.handle_mouse_press(_mouse_press(
            Qt.MouseButton.LeftButton, Qt.KeyboardModifier.ControlModifier,
        )) is True
        assert host._panning is True
        assert host._ctrl_panning is True

    def test_ctrl_lmb_tool_first_refusal(self, board_view: BoardView) -> None:
        from PySide6.QtCore import Qt

        class _Grabby(_ToolA):
            def on_press(self, inp) -> bool:
                return True  # consumes (component drag)

        board_view.set_tool(_Grabby())
        host = board_view.tools
        assert host.handle_mouse_press(_mouse_press(
            Qt.MouseButton.LeftButton, Qt.KeyboardModifier.ControlModifier,
        )) is True
        assert host._panning is False  # the tool ate it — no pan

    def test_plain_lmb_stays_a_stream(self, board_view: BoardView) -> None:
        from PySide6.QtCore import Qt
        pressed: list[bool] = []

        class _Probe(_ToolA):
            def on_press(self, inp) -> bool:
                pressed.append(True)
                return True

        board_view.set_tool(_Probe())
        board_view.tools.handle_mouse_press(
            _mouse_press(Qt.MouseButton.LeftButton))
        assert pressed == [True]
        assert board_view.tools._panning is False

    def test_plain_lmb_routes_through_default_action(
        self, board_view: BoardView
    ) -> None:
        # Sub-commit 4: the unchorded LMB residue is delivered as the
        # default_action verb, not a raw on_press.
        from PySide6.QtCore import Qt
        seen: list[str] = []

        class _Probe(_ToolA):
            def default_action(self, point, mods) -> bool:
                seen.append("verb")
                return True

        board_view.set_tool(_Probe())
        board_view.tools.handle_mouse_press(
            _mouse_press(Qt.MouseButton.LeftButton))
        assert seen == ["verb"]


class TestDispatchTable:
    def test_first_matching_entry_wins(self, board_view: BoardView) -> None:
        tool = _DispatchTool()
        board_view.set_tool(tool)
        from PySide6.QtCore import Qt
        inp = _key_event(Qt.Key.Key_K)
        assert tool._dispatch_key(inp) is True
        assert tool.calls == ["first"]

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
