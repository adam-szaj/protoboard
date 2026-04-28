# SPDX-License-Identifier: MIT
"""Tests for LibraryPanel and PropertiesPanel."""

from __future__ import annotations

import uuid

from protoboard.domain.components.instance import ComponentInstance
from protoboard.gui.panels.library_panel import LibraryPanel
from protoboard.gui.panels.properties_panel import PropertiesPanel
from protoboard.gui.session import EditorSession


class TestLibraryPanel:
    def test_panel_lists_known_components(
        self, qtbot: object, session: EditorSession
    ) -> None:
        from pytestqt.qtbot import QtBot

        assert isinstance(qtbot, QtBot)
        panel = LibraryPanel(session.library)
        qtbot.addWidget(panel)
        names = [panel._list.item(i).text() for i in range(panel._list.count())]
        assert "R" in names

    def test_double_click_emits_component_selected(
        self, qtbot: object, session: EditorSession
    ) -> None:
        from pytestqt.qtbot import QtBot

        assert isinstance(qtbot, QtBot)
        panel = LibraryPanel(session.library)
        qtbot.addWidget(panel)
        received: list[str] = []
        panel.component_selected.connect(received.append)
        # Find "R" item and double-click it.
        for i in range(panel._list.count()):
            if panel._list.item(i).text() == "R":
                panel._list.itemDoubleClicked.emit(panel._list.item(i))
                break
        assert received == ["R"]

    def test_all_library_components_shown(
        self, qtbot: object, session: EditorSession
    ) -> None:
        from pytestqt.qtbot import QtBot

        assert isinstance(qtbot, QtBot)
        panel = LibraryPanel(session.library)
        qtbot.addWidget(panel)
        assert panel._list.count() == len(list(session.library.names()))


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
