# SPDX-License-Identifier: MIT
import inspect

from protoboard.controller.ports import KeyInput, Mods
from protoboard.controller.tools.base import ControllerTool


class _FakeKeymap:
    def matches(self, key: int, mods: int, action_id: str) -> bool:
        return action_id == "test.hit" and key == 42


class _FakeView:
    keymap = _FakeKeymap()
    refresh_count = 0

    def refresh(self) -> None:
        self.refresh_count += 1


class _FakeController:
    def execute_command(self, command: object) -> bool:
        self.command = command
        return True

    def execute_command_silent(self, command: object) -> bool:
        self.silent_command = command
        return True


class _FakeSession:
    def __init__(self) -> None:
        self.edit_controller = _FakeController()


def test_controller_tool_is_qt_free_source():
    import protoboard.controller.tools.base as base

    src = inspect.getsource(base)
    assert "PySide6" not in src
    assert "gui.keymap" not in src
    assert "gui.theme" not in src


def test_matches_key_resolves_through_view_keymap():
    tool = ControllerTool()
    tool._view = _FakeView()  # type: ignore[assignment]
    inp = KeyInput(key=42, native_mods=0, is_auto_repeat=False, mods=Mods())
    assert tool.matches_key(inp, "test.hit") is True
    assert tool.matches_key(inp, "test.miss") is False


def test_matches_key_false_without_view():
    tool = ControllerTool()
    tool._view = None  # type: ignore[assignment]
    inp = KeyInput(key=42, native_mods=0, is_auto_repeat=False, mods=Mods())
    assert tool.matches_key(inp, "test.hit") is False


def test_execute_command_is_shared_by_controller_tools():
    tool = ControllerTool()
    session = _FakeSession()
    tool._session = session  # type: ignore[assignment]
    command = object()

    assert tool._execute_command(command) is True
    assert session.edit_controller.command is command


def test_execute_command_silent_is_shared_by_controller_tools():
    tool = ControllerTool()
    session = _FakeSession()
    tool._session = session  # type: ignore[assignment]
    command = object()

    assert tool._execute_command_silent(command) is True
    assert session.edit_controller.silent_command is command


def test_refresh_view_is_safe_before_activation():
    tool = ControllerTool()
    tool._refresh_view()

    view = _FakeView()
    tool._view = view  # type: ignore[assignment]
    tool._refresh_view()
    assert view.refresh_count == 1
