# SPDX-License-Identifier: MIT
"""Shortcuts page — sub-tabs (Application / Global / View / per-tool) and the
same-map conflict-steal rule: assigning a trigger already owned by another
action on the SAME KeyMap clears it from the previous owner."""

from __future__ import annotations

import pytest
from PySide6.QtWidgets import QMessageBox, QTabWidget

from protoboard.gui.board_view_tool import _Tool
from protoboard.gui.dialogs.preferences_dialog import (
    _AppShortcutsTab,
    _KeyMapTab,
    _ShortcutsPage,
)
from protoboard.gui.keybindings import KeyBindings
from protoboard.gui.keymap import GlobalKeyMap, KeyMap


@pytest.fixture(autouse=True)
def _mute_warnings(monkeypatch):
    # Validation failures pop a modal QMessageBox — mute it headless.
    monkeypatch.setattr(
        QMessageBox, "warning", staticmethod(lambda *a, **k: None))


def _base() -> KeyMap:
    from protoboard.gui.keymap import VIEW_ACTIONS
    km = KeyMap(GlobalKeyMap.with_globals())
    km.declare(VIEW_ACTIONS)
    return km


class _FakeTool(_Tool):
    def key_actions(self) -> dict[str, str | None]:
        return {
            "fake-tool.action.zap": "Z",
            "fake-tool.action.zip": "X",
            "fake-tool.action.edit": None,  # inherits global.action.edit
        }


class TestSubTabs:
    def test_tabs_present(self, qtbot) -> None:
        page = _ShortcutsPage(
            KeyBindings(), keymap=_base(), keymap_tools=(_FakeTool(),))
        qtbot.addWidget(page)
        tabs = page.findChild(QTabWidget)
        labels = [tabs.tabText(i) for i in range(tabs.count())]
        assert labels == ["Application", "Global", "View", "Fake Tool"]

    def test_no_keymap_keeps_legacy_only(self, qtbot) -> None:
        page = _ShortcutsPage(KeyBindings())
        qtbot.addWidget(page)
        tabs = page.findChild(QTabWidget)
        assert [tabs.tabText(i) for i in range(tabs.count())] == ["Application"]

    def test_production_tool_builds_its_tab(self, qtbot) -> None:
        # Regression: MainWindow passes a real, composed production tool
        # (SelectTool = (_SelectTool, ViewToolMixin)) as keymap_tools — it is
        # NOT a nominal _Tool subclass, so the old `assert isinstance(tool,
        # _Tool)` crashed here (AssertionError) when opening Preferences.
        from protoboard.gui.tools.gui_tools import SelectTool

        page = _ShortcutsPage(
            KeyBindings(), keymap=_base(), keymap_tools=(SelectTool(),))
        qtbot.addWidget(page)
        tabs = page.findChild(QTabWidget)
        labels = [tabs.tabText(i) for i in range(tabs.count())]
        # The production tool contributes at least its own key-map tab on top
        # of Application/Global/View (does not crash).
        assert labels[:3] == ["Application", "Global", "View"]


class TestKeyMapTabEditing:
    def _tool_tab(self, base: KeyMap) -> _KeyMapTab:
        layer = _FakeTool().make_key_map(base)
        return _KeyMapTab(layer, [a.id for a in layer.known_actions()])

    def test_edit_applies_override_to_shared_store(self, qtbot, base=None) -> None:
        base = _base()
        tab = self._tool_tab(base)
        qtbot.addWidget(tab)
        tab._edits["fake-tool.action.zap"].setText("K")
        tab.apply()
        # Lands in the shared store → a future layer of the tool sees it.
        relayer = _FakeTool().make_key_map(base)
        assert relayer.resolve("fake-tool.action.zap") == "K"

    def test_same_map_conflict_steals_old_binding(self, qtbot) -> None:
        base = _base()
        tab = self._tool_tab(base)
        qtbot.addWidget(tab)
        # zip takes over zap's "Z" — zap must not keep it (ambiguous keys).
        tab._edits["fake-tool.action.zip"].setText("Z")
        tab.apply()
        relayer = _FakeTool().make_key_map(base)
        assert relayer.resolve("fake-tool.action.zip") == "Z"
        assert relayer.resolve("fake-tool.action.zap") == ""
        # The stolen row updates visibly in the UI too.
        assert tab._edits["fake-tool.action.zap"].text() == ""

    def test_steal_removes_only_the_taken_alternative(self, qtbot) -> None:
        base = _base()
        layer = KeyMap.layer_over(base)
        layer.register_tool_actions(
            {"t.action.step": "Up|W", "t.action.other": ""})
        tab = _KeyMapTab(layer, ["t.action.step", "t.action.other"])
        qtbot.addWidget(tab)
        tab._edits["t.action.other"].setText("W")
        tab.apply()
        assert layer.resolve("t.action.other") == "W"
        assert layer.resolve("t.action.step") == "Up"  # kept the arrow

    def test_final_row_is_read_only(self, qtbot) -> None:
        base = _base()
        from protoboard.gui.keymap import GLOBAL_ACTIONS
        tab = _KeyMapTab(base, [a.id for a in GLOBAL_ACTIONS])
        qtbot.addWidget(tab)
        assert tab._edits["global.action.cancel"].isEnabled() is False
        assert tab._edits["global.action.edit"].isEnabled() is True

    def test_invalid_text_is_rejected_and_reverted(self, qtbot) -> None:
        base = _base()
        tab = self._tool_tab(base)
        qtbot.addWidget(tab)
        tab._edits["fake-tool.action.zap"].setText("NotAKey123|???")
        tab.apply()
        assert tab._edits["fake-tool.action.zap"].text() == "Z"
        relayer = _FakeTool().make_key_map(base)
        assert relayer.resolve("fake-tool.action.zap") == "Z"

    def test_claiming_final_trigger_is_rejected(self, qtbot) -> None:
        base = _base()
        tab = self._tool_tab(base)
        qtbot.addWidget(tab)
        tab._edits["fake-tool.action.zap"].setText("Esc")
        tab.apply()
        # Guarded by FinalShortcutError — reverted, cancel keeps Esc.
        assert tab._edits["fake-tool.action.zap"].text() == "Z"
        assert base.resolve("global.action.cancel") == "Esc"

    def test_mouse_chord_is_editable_text(self, qtbot) -> None:
        base = _base()
        from protoboard.gui.keymap import VIEW_ACTIONS, VIEW_ZOOM_IN
        tab = _KeyMapTab(base, [a.id for a in VIEW_ACTIONS])
        qtbot.addWidget(tab)
        assert tab._edits[VIEW_ZOOM_IN].text() == "WheelUp"
        tab._edits[VIEW_ZOOM_IN].setText("Shift+WheelUp")
        tab.apply()
        assert base.resolve(VIEW_ZOOM_IN) == "Shift+WheelUp"


class TestLegacyTabSteal:
    def test_duplicate_shortcut_clears_previous_owner(self, qtbot) -> None:
        from PySide6.QtGui import QKeySequence
        from protoboard.gui.keybindings import (
            ACTION_ROTATE_CW,
            ACTION_TOOL_SELECT,
        )
        kb = KeyBindings()
        tab = _AppShortcutsTab(kb)
        qtbot.addWidget(tab)
        # Give rotate_cw the Select tool's "S" — Select must lose it.
        tab._seq_widgets[ACTION_ROTATE_CW].setKeySequence(QKeySequence("S"))
        tab.apply()
        assert kb.get(ACTION_ROTATE_CW) == "S"
        assert kb.get(ACTION_TOOL_SELECT) == ""
        assert tab._seq_widgets[ACTION_TOOL_SELECT].keySequence().isEmpty()
