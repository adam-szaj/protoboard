# SPDX-License-Identifier: MIT
"""Parity guards for the key-dispatch ladder (qt-ports phase 3).

Pin the observable behaviour of ``ToolHost.handle_key_press`` /
``BoardView.event(ShortcutOverride)`` BEFORE the ``event.accept()`` protocol
becomes bool returns, so the swap cannot silently regress:

* a key the tool consumes ends up accepted (no double-dispatch);
* an unconsumed key stays ignored (Qt propagates it; global WindowShortcut
  QActions fire via ShortcutOverride, which ``wants_key`` may veto);
* Esc closes the ``[?]`` overlay first and only then escapes the tool;
* arrows scroll the view unless an armed tool claims them.

Note: a hand-constructed ``QKeyEvent`` is *accepted* by default, so each test
calls ``ignore()`` first — mimicking a freshly delivered event the handlers
must explicitly consume.
"""

from __future__ import annotations

from PySide6.QtCore import QEvent, Qt
from PySide6.QtGui import QKeyEvent

from protoboard.gui.board_view import BoardView, _Tool


def _key_event(key: Qt.Key, mods: Qt.KeyboardModifier = Qt.KeyboardModifier.NoModifier) -> QKeyEvent:
    ev = QKeyEvent(QEvent.Type.KeyPress, key, mods)
    ev.ignore()
    return ev


class _RecordingWindow:
    """Stands in for MainWindow behind ``view.window()``."""

    def __init__(self) -> None:
        self.forwarded: list[int] = []

    def keyPressEvent(self, event: QKeyEvent) -> None:
        self.forwarded.append(event.key())


class _ClaimingTool(_Tool):
    """Consumes the keys in *claimed* and records calls."""

    def __init__(self, claimed: set[Qt.Key]) -> None:
        super().__init__()
        self._claimed = claimed
        self.seen: list[int] = []

    def wants_key(self, inp) -> bool:  # type: ignore[override]
        return inp.key in self._claimed

    def on_key(self, inp) -> bool:  # type: ignore[override]
        self.seen.append(inp.key)
        return inp.key in self._claimed


def _install(board_view: BoardView, tool: _Tool, monkeypatch) -> _RecordingWindow:
    win = _RecordingWindow()
    monkeypatch.setattr(board_view, "window", lambda: win)
    board_view.set_tool(tool)
    return win


class TestConsumptionParity:
    def test_consumed_key_ends_accepted(
        self, board_view: BoardView, monkeypatch
    ) -> None:
        tool = _ClaimingTool({Qt.Key.Key_F})
        _install(board_view, tool, monkeypatch)
        ev = _key_event(Qt.Key.Key_F)
        board_view.tools.handle_key_press(ev)
        assert tool.seen == [Qt.Key.Key_F]
        assert ev.isAccepted()

    def test_unconsumed_key_stays_ignored(
        self, board_view: BoardView, monkeypatch
    ) -> None:
        tool = _ClaimingTool(set())  # consumes nothing
        _install(board_view, tool, monkeypatch)
        ev = _key_event(Qt.Key.Key_H)
        board_view.tools.handle_key_press(ev)
        assert tool.seen == [Qt.Key.Key_H]
        assert not ev.isAccepted()

    def test_auto_repeat_is_dropped(
        self, board_view: BoardView, monkeypatch
    ) -> None:
        tool = _ClaimingTool(set())
        win = _install(board_view, tool, monkeypatch)
        ev = QKeyEvent(
            QEvent.Type.KeyPress, Qt.Key.Key_H,
            Qt.KeyboardModifier.NoModifier, autorep=True,
        )
        board_view.tools.handle_key_press(ev)
        assert tool.seen == []
        assert win.forwarded == []


class TestShortcutOverrideVeto:
    def test_wanting_tool_vetoes_global_shortcut(
        self, board_view: BoardView, monkeypatch
    ) -> None:
        tool = _ClaimingTool({Qt.Key.Key_W})
        _install(board_view, tool, monkeypatch)
        ev = _key_event(Qt.Key.Key_W)
        assert board_view.tools.wants_shortcut_override(ev) is True

    def test_indifferent_tool_lets_shortcut_fire(
        self, board_view: BoardView, monkeypatch
    ) -> None:
        tool = _ClaimingTool(set())
        _install(board_view, tool, monkeypatch)
        ev = _key_event(Qt.Key.Key_W)
        assert board_view.tools.wants_shortcut_override(ev) is False


class TestEscapeLadder:
    def test_escape_emits_escape_tool_and_reaches_tool(
        self, board_view: BoardView, monkeypatch
    ) -> None:
        tool = _ClaimingTool(set())
        win = _install(board_view, tool, monkeypatch)
        fired: list[bool] = []
        board_view.escape_tool.connect(lambda: fired.append(True))
        board_view.tools.handle_key_press(_key_event(Qt.Key.Key_Escape))
        assert tool.seen == [Qt.Key.Key_Escape]
        assert fired == [True]
        assert win.forwarded == []  # Esc never leaves the view

    def test_escape_closes_help_overlay_without_escaping_tool(
        self, board_view: BoardView, monkeypatch
    ) -> None:
        tool = _ClaimingTool(set())
        _install(board_view, tool, monkeypatch)
        board_view.tools._show_shortcuts_overlay()
        assert board_view.tools._shortcuts_overlay_visible()
        fired: list[bool] = []
        board_view.escape_tool.connect(lambda: fired.append(True))
        board_view.tools.handle_key_press(_key_event(Qt.Key.Key_Escape))
        assert not board_view.tools._shortcuts_overlay_visible()
        assert fired == []
        assert tool.seen == []


class TestArrowKeys:
    def test_arrow_scrolls_when_tool_is_idle(
        self, board_view: BoardView, monkeypatch
    ) -> None:
        tool = _ClaimingTool(set())
        win = _install(board_view, tool, monkeypatch)
        # Give the scrollbar a real range (content must exceed the viewport).
        board_view.scale(50, 50)
        hbar = board_view.horizontalScrollBar()
        assert hbar.maximum() > hbar.minimum()
        hbar.setValue(hbar.minimum())
        before = hbar.value()
        board_view.tools.handle_key_press(_key_event(Qt.Key.Key_Right))
        after = hbar.value()
        assert after > before
        assert tool.seen == []  # scroll fallback, not the tool
        assert win.forwarded == []

    def test_armed_tool_claims_arrow_instead_of_scroll(
        self, board_view: BoardView, monkeypatch
    ) -> None:
        tool = _ClaimingTool({Qt.Key.Key_Right})
        win = _install(board_view, tool, monkeypatch)
        before = board_view.horizontalScrollBar().value()
        board_view.tools.handle_key_press(_key_event(Qt.Key.Key_Right))
        after = board_view.horizontalScrollBar().value()
        assert after == before
        assert tool.seen == [Qt.Key.Key_Right]
        assert win.forwarded == []
