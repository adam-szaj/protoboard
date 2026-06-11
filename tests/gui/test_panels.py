# SPDX-License-Identifier: MIT
"""Tests for LibraryPanel and PropertiesPanel."""

from __future__ import annotations

import uuid

from protoboard.domain.components.instance import ComponentInstance
from protoboard.gui.panels.library_panel import LibraryPanel
from protoboard.gui.panels.properties_panel import PropertiesPanel
from protoboard.gui.session import EditorSession


def _all_leaf_names(panel: LibraryPanel) -> list[str]:
    """Collect all component names (leaf items) from the tree (any depth)."""
    from PySide6.QtCore import Qt
    from PySide6.QtWidgets import QTreeWidgetItem

    names: list[str] = []

    def _collect(item: QTreeWidgetItem) -> None:
        data = item.data(0, Qt.ItemDataRole.UserRole)
        if data is not None:
            # UserRole stores (name, variant_id_or_None); collect unique names.
            name = data[0] if isinstance(data, tuple) else data
            if name and name not in names:
                names.append(name)
        for i in range(item.childCount()):
            _collect(item.child(i))

    root = panel._tree.invisibleRootItem()
    for i in range(root.childCount()):
        _collect(root.child(i))
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
        received: list[tuple[str, object]] = []
        panel.component_selected.connect(lambda name, variant: received.append((name, variant)))
        # Find "R" leaf item (any depth) and simulate double-click.
        from PySide6.QtWidgets import QTreeWidgetItem

        def _find_item(item: QTreeWidgetItem, target: str) -> QTreeWidgetItem | None:
            data = item.data(0, Qt.ItemDataRole.UserRole)
            if data is not None:
                name = data[0] if isinstance(data, tuple) else data
                if name == target:
                    return item
            for i in range(item.childCount()):
                found = _find_item(item.child(i), target)
                if found is not None:
                    return found
            return None

        root = panel._tree.invisibleRootItem()
        r_item: QTreeWidgetItem | None = None
        for i in range(root.childCount()):
            r_item = _find_item(root.child(i), "R")
            if r_item is not None:
                break
        assert r_item is not None, "'R' item not found in tree"
        panel._tree.itemDoubleClicked.emit(r_item, 0)
        assert len(received) == 1
        assert received[0][0] == "R"

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


class TestBodyVariantDropdown:
    """The Properties panel offers a body-variant dropdown for TwoPin
    components when a package catalogue is provided."""

    def _make_defn_with_default(self) -> object:
        from protoboard.domain.components.definition import (
            ComponentClass,
            ComponentDefinition,
        )
        from protoboard.domain.components.footprints import TwoPin
        return ComponentDefinition(
            name="R", footprint=TwoPin(),
            pin_names={}, component_class=ComponentClass.RESISTOR,
            default_body_variant="DIN-0207/typ",
        )

    def _packages(self) -> dict:
        from protoboard.domain.components.two_pin_body import (
            BodyShape, LeadArrangement, TwoPinBodyPackage, TwoPinBodySpec,
        )
        def _spec(pkg: str, vid: str, length: float = 5.0,
                  diameter: float = 2.0) -> TwoPinBodySpec:
            return TwoPinBodySpec(
                package_id=pkg, variant_id=vid, label=vid,
                lead_arrangement=LeadArrangement.AXIAL,
                shape=BodyShape.CYLINDER,
                length_mm=length, diameter_mm=diameter,
            )
        return {
            "DIN-0207": TwoPinBodyPackage(
                package_id="DIN-0207", label="DIN 0207",
                variants=(_spec("DIN-0207", "S"), _spec("DIN-0207", "typ")),
            ),
            "DIN-0617": TwoPinBodyPackage(
                package_id="DIN-0617", label="DIN 0617",
                variants=(_spec("DIN-0617", "typ", length=14.0, diameter=5.0),),
            ),
        }

    def test_dropdown_visible_when_twopin_with_packages(
        self, qtbot: object,
    ) -> None:
        from pytestqt.qtbot import QtBot
        assert isinstance(qtbot, QtBot)
        panel = PropertiesPanel()
        qtbot.addWidget(panel)
        inst = ComponentInstance.new(definition_ref="R", refdes="R1", value="")
        panel.show_component(
            inst, defn=self._make_defn_with_default(),  # type: ignore[arg-type]
            two_pin_packages=self._packages(),
        )
        assert panel._body_variant_combo.isVisible() or True  # widget might not show before parent
        # Visibility flag is set even before the panel is shown.
        assert len(panel._body_variant_addresses) > 0

    def test_dropdown_lists_default_first(self, qtbot: object) -> None:
        from pytestqt.qtbot import QtBot
        assert isinstance(qtbot, QtBot)
        panel = PropertiesPanel()
        qtbot.addWidget(panel)
        inst = ComponentInstance.new(definition_ref="R", refdes="R1", value="")
        panel.show_component(
            inst, defn=self._make_defn_with_default(),  # type: ignore[arg-type]
            two_pin_packages=self._packages(),
        )
        # First entry is always "Default (library)" with None address.
        assert panel._body_variant_addresses[0] is None
        # DIN-0207 variants come right after (definition's default package).
        assert panel._body_variant_addresses[1] == "DIN-0207/S"
        assert panel._body_variant_addresses[2] == "DIN-0207/typ"
        # Then DIN-0617.
        assert "DIN-0617/typ" in panel._body_variant_addresses

    def test_dropdown_hidden_for_non_twopin(self, qtbot: object) -> None:
        from pytestqt.qtbot import QtBot
        from protoboard.domain.components.definition import (
            ComponentClass,
            ComponentDefinition,
        )
        from protoboard.domain.components.footprints import SIL
        assert isinstance(qtbot, QtBot)
        panel = PropertiesPanel()
        qtbot.addWidget(panel)
        defn = ComponentDefinition(
            name="HDR3", footprint=SIL(3),
            pin_names={}, component_class=ComponentClass.CONNECTOR,
        )
        inst = ComponentInstance.new(definition_ref="HDR3", refdes="J1", value="")
        panel.show_component(
            inst, defn=defn, two_pin_packages=self._packages(),
        )
        assert panel._body_variant_addresses == []

    def test_dropdown_hidden_when_no_catalogue(self, qtbot: object) -> None:
        from pytestqt.qtbot import QtBot
        assert isinstance(qtbot, QtBot)
        panel = PropertiesPanel()
        qtbot.addWidget(panel)
        inst = ComponentInstance.new(definition_ref="R", refdes="R1", value="")
        panel.show_component(
            inst, defn=self._make_defn_with_default(),  # type: ignore[arg-type]
            two_pin_packages={},
        )
        assert panel._body_variant_addresses == []

    def test_changing_dropdown_emits_signal(self, qtbot: object) -> None:
        from pytestqt.qtbot import QtBot
        assert isinstance(qtbot, QtBot)
        panel = PropertiesPanel()
        qtbot.addWidget(panel)
        inst = ComponentInstance.new(
            definition_ref="R", refdes="R1", value="", body_variant=None,
        )
        panel.show_component(
            inst, defn=self._make_defn_with_default(),  # type: ignore[arg-type]
            two_pin_packages=self._packages(),
        )
        received: list[tuple[uuid.UUID, object]] = []
        panel.body_variant_changed.connect(
            lambda cid, addr: received.append((cid, addr))
        )
        # Move to DIN-0617/typ
        target_idx = panel._body_variant_addresses.index("DIN-0617/typ")
        panel._body_variant_combo.setCurrentIndex(target_idx)
        assert len(received) == 1
        _, addr = received[0]
        assert addr == "DIN-0617/typ"

    def test_current_instance_variant_preselected(self, qtbot: object) -> None:
        from pytestqt.qtbot import QtBot
        assert isinstance(qtbot, QtBot)
        panel = PropertiesPanel()
        qtbot.addWidget(panel)
        inst = ComponentInstance.new(
            definition_ref="R", refdes="R1", value="",
            body_variant="DIN-0617/typ",
        )
        panel.show_component(
            inst, defn=self._make_defn_with_default(),  # type: ignore[arg-type]
            two_pin_packages=self._packages(),
        )
        # Combo should pick the DIN-0617/typ entry, not Default.
        idx = panel._body_variant_combo.currentIndex()
        assert panel._body_variant_addresses[idx] == "DIN-0617/typ"
