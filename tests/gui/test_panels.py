# SPDX-License-Identifier: MIT
"""Tests for LibraryPanel and PropertiesPanel."""

from __future__ import annotations

import uuid

from protoboard.domain.components.instance import ComponentInstance
from protoboard.gui.panels.library_panel import LibraryPanel
from protoboard.gui.panels.properties_panel import PropertiesPanel
from protoboard.gui.session import EditorSession


def _all_leaf_names(panel: LibraryPanel) -> list[str]:
    """Collect all component names (leaf items) from the tree."""
    from PySide6.QtCore import Qt

    names: list[str] = []
    root = panel._tree.invisibleRootItem()
    for i in range(root.childCount()):
        group = root.child(i)
        for j in range(group.childCount()):
            child = group.child(j)
            name = child.data(0, Qt.ItemDataRole.UserRole)
            if name:
                names.append(name)
    return names


class TestLibraryPanel:
    def test_panel_lists_known_components(
        self, qtbot: object, session: EditorSession
    ) -> None:
        from pytestqt.qtbot import QtBot

        assert isinstance(qtbot, QtBot)
        panel = LibraryPanel(session.library)
        qtbot.addWidget(panel)
        assert "R" in _all_leaf_names(panel)

    def test_double_click_emits_component_selected(
        self, qtbot: object, session: EditorSession
    ) -> None:
        from PySide6.QtCore import Qt
        from pytestqt.qtbot import QtBot

        assert isinstance(qtbot, QtBot)
        panel = LibraryPanel(session.library)
        qtbot.addWidget(panel)
        received: list[str] = []
        panel.component_selected.connect(received.append)
        # Find "R" leaf item and simulate double-click.
        root = panel._tree.invisibleRootItem()
        for i in range(root.childCount()):
            group = root.child(i)
            for j in range(group.childCount()):
                child = group.child(j)
                if child.data(0, Qt.ItemDataRole.UserRole) == "R":
                    panel._tree.itemDoubleClicked.emit(child, 0)
                    break
        assert received == ["R"]

    def test_all_library_components_shown(
        self, qtbot: object, session: EditorSession
    ) -> None:
        from pytestqt.qtbot import QtBot

        assert isinstance(qtbot, QtBot)
        panel = LibraryPanel(session.library)
        qtbot.addWidget(panel)
        assert sorted(_all_leaf_names(panel)) == sorted(session.library.names())

    def test_search_filters_components(
        self, qtbot: object, session: EditorSession
    ) -> None:
        from pytestqt.qtbot import QtBot

        assert isinstance(qtbot, QtBot)
        panel = LibraryPanel(session.library)
        qtbot.addWidget(panel)
        panel._search.setText("BC5")
        names = _all_leaf_names(panel)
        assert all("BC5" in n for n in names)
        assert len(names) > 0

    def test_search_empty_restores_all(
        self, qtbot: object, session: EditorSession
    ) -> None:
        from pytestqt.qtbot import QtBot

        assert isinstance(qtbot, QtBot)
        panel = LibraryPanel(session.library)
        qtbot.addWidget(panel)
        panel._search.setText("XXXXXXXXX")
        assert _all_leaf_names(panel) == []
        panel._search.clear()
        assert sorted(_all_leaf_names(panel)) == sorted(session.library.names())


class TestPropertiesPanel:
    def test_show_component_fills_refdes(self, qtbot: object) -> None:
        from pytestqt.qtbot import QtBot

        assert isinstance(qtbot, QtBot)
        panel = PropertiesPanel()
        qtbot.addWidget(panel)
        inst = ComponentInstance.new(definition_ref="R", refdes="R3", value="4k7")
        panel.show_component(inst)
        assert panel._refdes_edit.text() == "R3"

    def test_show_component_fills_value(self, qtbot: object) -> None:
        from pytestqt.qtbot import QtBot

        assert isinstance(qtbot, QtBot)
        panel = PropertiesPanel()
        qtbot.addWidget(panel)
        inst = ComponentInstance.new(definition_ref="R", refdes="R3", value="4k7")
        panel.show_component(inst)
        assert panel._value_edit.text() == "4k7"

    def test_clear_component_empties_fields(self, qtbot: object) -> None:
        from pytestqt.qtbot import QtBot

        assert isinstance(qtbot, QtBot)
        panel = PropertiesPanel()
        qtbot.addWidget(panel)
        inst = ComponentInstance.new(definition_ref="R", refdes="R3", value="4k7")
        panel.show_component(inst)
        panel.clear_component()
        assert panel._refdes_edit.text() == ""
        assert panel._value_edit.text() == ""

    def test_apply_emits_component_changed(self, qtbot: object) -> None:
        from pytestqt.qtbot import QtBot

        assert isinstance(qtbot, QtBot)
        panel = PropertiesPanel()
        qtbot.addWidget(panel)
        inst = ComponentInstance.new(definition_ref="R", refdes="R1", value="10k")
        panel.show_component(inst)
        received: list[tuple[uuid.UUID, str, str, int]] = []
        panel.component_changed.connect(
            lambda cid, rd, val, rot: received.append((cid, rd, val, rot))
        )
        panel._refdes_edit.setText("R99")
        panel._value_edit.setText("22k")
        panel._rotation_combo.setCurrentIndex(1)  # 90°
        panel._apply_btn.click()
        assert len(received) == 1
        _, refdes, value, rotation = received[0]
        assert refdes == "R99"
        assert value == "22k"
        assert rotation == 90

    def test_apply_without_selection_does_nothing(self, qtbot: object) -> None:
        from pytestqt.qtbot import QtBot

        assert isinstance(qtbot, QtBot)
        panel = PropertiesPanel()
        qtbot.addWidget(panel)
        received: list[object] = []
        panel.component_changed.connect(lambda *a: received.append(a))
        panel._apply_btn.click()
        assert received == []
