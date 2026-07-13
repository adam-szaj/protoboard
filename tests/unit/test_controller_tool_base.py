# SPDX-License-Identifier: MIT
import inspect
from protoboard.controller.tools.base import ControllerTool
from protoboard.controller.ports import KeyInput, Mods


class _FakeKeymap:
    def matches(self, key: int, mods: int, action_id: str) -> bool:
        return action_id == "test.hit" and key == 42


class _FakeView:
    keymap = _FakeKeymap()


def test_controller_tool_is_qt_free_source():
    import protoboard.controller.tools.base as base
    src = inspect.getsource(base)
    assert "PySide6" not in src
    assert "gui.keymap" not in src
    assert "gui.theme" not in src


def test_key_matches_resolves_through_view_keymap():
    tool = ControllerTool()
    tool._view = _FakeView()  # type: ignore[assignment]
    inp = KeyInput(key=42, native_mods=0, is_auto_repeat=False, mods=Mods())
    assert tool._key_matches(inp, "test.hit") is True
    assert tool._key_matches(inp, "test.miss") is False


def test_key_matches_false_without_view():
    tool = ControllerTool()
    tool._view = None  # type: ignore[assignment]
    inp = KeyInput(key=42, native_mods=0, is_auto_repeat=False, mods=Mods())
    assert tool._key_matches(inp, "test.hit") is False
