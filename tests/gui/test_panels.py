# SPDX-License-Identifier: MIT
"""Tests for LibraryPanel and PropertiesPanel."""

from __future__ import annotations

import uuid

import pytest

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

    def test_show_component_fills_tolerance(self, qtbot: object) -> None:
        from pytestqt.qtbot import QtBot

        assert isinstance(qtbot, QtBot)
        panel = PropertiesPanel()
        qtbot.addWidget(panel)
        inst = ComponentInstance.new(
            definition_ref="R", refdes="R3", value="4k7", tolerance="1%")
        panel.show_component(inst)
        assert panel._tolerance_edit.text() == "1%"

    def test_clear_component_empties_fields(self, qtbot: object) -> None:
        from pytestqt.qtbot import QtBot

        assert isinstance(qtbot, QtBot)
        panel = PropertiesPanel()
        qtbot.addWidget(panel)
        inst = ComponentInstance.new(
            definition_ref="R", refdes="R3", value="4k7", tolerance="1%")
        panel.show_component(inst)
        panel.clear_component()
        assert panel._refdes_edit.text() == ""
        assert panel._value_edit.text() == ""
        assert panel._tolerance_edit.text() == ""

    def test_apply_emits_component_changed(self, qtbot: object) -> None:
        from pytestqt.qtbot import QtBot

        assert isinstance(qtbot, QtBot)
        panel = PropertiesPanel()
        qtbot.addWidget(panel)
        inst = ComponentInstance.new(definition_ref="R", refdes="R1", value="10k")
        panel.show_component(inst)
        received: list[tuple[uuid.UUID, str, str, str, int]] = []
        panel.component_changed.connect(
            lambda cid, rd, val, tol, rot:
            received.append((cid, rd, val, tol, rot))
        )
        panel._refdes_edit.setText("R99")
        panel._value_edit.setText("22k")
        panel._tolerance_edit.setText("1%")
        panel._rotation_combo.setCurrentIndex(1)  # 90°
        panel._apply_btn.click()
        assert len(received) == 1
        _, refdes, value, tolerance, rotation = received[0]
        assert refdes == "R99"
        assert value == "22k"
        assert tolerance == "1%"
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


class TestPropertiesPanelAppearance:
    """The per-instance style override editor: two text fields (colour hex +
    corner radius mm) committed via Apply; empty fields = no override."""

    def _panel(self, qtbot: object) -> PropertiesPanel:
        from pytestqt.qtbot import QtBot

        assert isinstance(qtbot, QtBot)
        panel = PropertiesPanel()
        qtbot.addWidget(panel)
        return panel

    def test_show_component_populates_style_fields(self, qtbot: object) -> None:
        from protoboard.domain.components.appearance import Appearance

        panel = self._panel(qtbot)
        inst = ComponentInstance.new(
            definition_ref="R", refdes="R3", value="4k7",
            appearance=Appearance(
                color="#1a2b3c", corner_radius_mm=(0.5, 0.0),
            ),
        )
        panel.show_component(inst)
        assert panel._style_color_edit.text() == "#1a2b3c"
        assert panel._style_radius_edit.text() == "0.5 0"

    def test_no_override_shows_empty_fields(self, qtbot: object) -> None:
        panel = self._panel(qtbot)
        inst = ComponentInstance.new(definition_ref="R", refdes="R3", value="4k7")
        panel.show_component(inst)
        assert panel._style_color_edit.text() == ""
        assert panel._style_radius_edit.text() == ""

    def test_clear_component_empties_style_fields(self, qtbot: object) -> None:
        from protoboard.domain.components.appearance import Appearance

        panel = self._panel(qtbot)
        inst = ComponentInstance.new(
            definition_ref="R", refdes="R3", value="4k7",
            appearance=Appearance(color="#1a2b3c"),
        )
        panel.show_component(inst)
        panel.clear_component()
        assert panel._style_color_edit.text() == ""
        assert panel._style_radius_edit.text() == ""

    def test_apply_emits_parsed_appearance(self, qtbot: object) -> None:
        from protoboard.domain.components.appearance import Appearance

        panel = self._panel(qtbot)
        inst = ComponentInstance.new(definition_ref="R", refdes="R1", value="10k")
        panel.show_component(inst)
        received: list[tuple[object, object]] = []
        panel.appearance_changed.connect(
            lambda cid, app: received.append((cid, app))
        )
        panel._style_color_edit.setText("#1a1a1a")
        panel._style_radius_edit.setText("0.5 0 0.5 0")
        panel._apply_btn.click()
        assert len(received) == 1
        cid, app = received[0]
        assert cid == inst.id
        assert app == Appearance(
            color="#1a1a1a", corner_radius_mm=(0.5, 0.0, 0.5, 0.0),
        )

    def test_apply_unchanged_fields_does_not_emit(self, qtbot: object) -> None:
        from protoboard.domain.components.appearance import Appearance

        panel = self._panel(qtbot)
        inst = ComponentInstance.new(
            definition_ref="R", refdes="R1", value="10k",
            appearance=Appearance(color="#1a1a1a"),
        )
        panel.show_component(inst)
        received: list[object] = []
        panel.appearance_changed.connect(lambda *a: received.append(a))
        panel._apply_btn.click()
        assert received == []

    def test_apply_cleared_fields_emits_none(self, qtbot: object) -> None:
        from protoboard.domain.components.appearance import Appearance

        panel = self._panel(qtbot)
        inst = ComponentInstance.new(
            definition_ref="R", refdes="R1", value="10k",
            appearance=Appearance(color="#1a1a1a"),
        )
        panel.show_component(inst)
        received: list[tuple[object, object]] = []
        panel.appearance_changed.connect(
            lambda cid, app: received.append((cid, app))
        )
        panel._style_color_edit.clear()
        panel._apply_btn.click()
        assert received == [(inst.id, None)]

    def test_bad_colour_rejected_no_emit(self, qtbot: object) -> None:
        panel = self._panel(qtbot)
        inst = ComponentInstance.new(definition_ref="R", refdes="R1", value="10k")
        panel.show_component(inst)
        received: list[object] = []
        panel.appearance_changed.connect(lambda *a: received.append(a))
        panel._style_color_edit.setText("not-a-colour")
        panel._apply_btn.click()
        assert received == []

    def test_bad_radius_rejected_no_emit(self, qtbot: object) -> None:
        panel = self._panel(qtbot)
        inst = ComponentInstance.new(definition_ref="R", refdes="R1", value="10k")
        panel.show_component(inst)
        received: list[object] = []
        panel.appearance_changed.connect(lambda *a: received.append(a))
        panel._style_radius_edit.setText("0.5 x")
        panel._apply_btn.click()
        assert received == []


class TestLibraryPanelBodyPicker:
    """LibraryPanel exposes a body-variant picker under the tree when a
    two-pin component is selected and a package catalogue is wired."""

    @pytest.fixture(autouse=True)
    def _isolate_recent(self, tmp_path: object, monkeypatch: object) -> None:
        # Placing through the picker records a recent footprint; isolate the
        # store so it never touches ~/.config and never bleeds between tests.
        import pytest as _pytest
        from pathlib import Path
        from protoboard.gui import recent_footprints
        assert isinstance(tmp_path, Path)
        assert isinstance(monkeypatch, _pytest.MonkeyPatch)
        monkeypatch.setattr(recent_footprints, "_CONFIG_DIR", tmp_path)
        monkeypatch.setattr(
            recent_footprints, "_RECENT_FILE",
            tmp_path / "recent_footprints.json",
        )

    def _packages(self) -> dict:
        from protoboard.domain.components.package import (
            PackageFamily, PackageVariant,
        )
        from protoboard.domain.components.package.loader import build_package

        def _cyl() -> object:
            return build_package(
                {
                    "shape": "cylinder",
                    "dims": {"diameter": 2.0, "length": 5.0},
                    "bend": 0,
                    "leads": {
                        "direction": [[0, 0, -1], [0, 0, 1]],
                        "body_offset": 0, "board_offset": "solve",
                        "lead_r": 0.25, "lead_length": 8.0,
                    },
                },
                "C",
            )

        return {
            "DIN-0207": PackageFamily(
                family_id="DIN-0207", label="DIN 0207",
                variants=(PackageVariant("typ", "typ", _cyl()),),  # type: ignore[arg-type]
            ),
            "DIN-0617": PackageFamily(
                family_id="DIN-0617", label="DIN 0617",
                variants=(PackageVariant("typ", "typ", _cyl()),),  # type: ignore[arg-type]
            ),
        }

    def _select_R(self, panel: LibraryPanel) -> object:
        from PySide6.QtCore import Qt
        from PySide6.QtWidgets import QTreeWidgetItem

        def _find_item(item: QTreeWidgetItem, target: str) -> object:
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
        for i in range(root.childCount()):
            r = _find_item(root.child(i), "R")
            if r is not None:
                panel._tree.setCurrentItem(r)  # type: ignore[arg-type]
                return r
        return None

    def _patch_dialog(self, monkeypatch: object, chosen: "str | None") -> list:
        """Stub the footprint pop-up: capture its (recent, options) and return
        *chosen* (or reject when ``chosen is None``). Returns the capture list."""
        import pytest as _pytest
        from PySide6.QtWidgets import QDialog
        from protoboard.gui.dialogs import footprint_picker_dialog
        assert isinstance(monkeypatch, _pytest.MonkeyPatch)
        seen: list = []

        class _Stub:
            def __init__(self, provider, recent, options, parent=None):
                seen.append((recent, options))
                self.chosen_address = chosen

            def exec(self):
                return (
                    QDialog.DialogCode.Accepted if chosen is not None
                    else QDialog.DialogCode.Rejected
                )

        monkeypatch.setattr(
            footprint_picker_dialog, "FootprintPickerDialog", _Stub
        )
        return seen

    def test_multi_option_offers_packages(
        self, qtbot: object, session: EditorSession,
    ) -> None:
        # R offers two package footprints; the provider reports both (no
        # placeholder / forced choice) and neither is a kind.
        from pytestqt.qtbot import QtBot
        assert isinstance(qtbot, QtBot)
        panel = LibraryPanel(session.library)
        qtbot.addWidget(panel)
        panel.set_package_families(self._packages())
        opts = panel._choice.options(session.library.get("R"))
        addrs = [o.address for o in opts]
        assert "DIN-0207/typ" in addrs and "DIN-0617/typ" in addrs
        assert not any(o.is_kind for o in opts)

    def test_no_options_without_catalogue(
        self, qtbot: object, session: EditorSession,
    ) -> None:
        from pytestqt.qtbot import QtBot
        assert isinstance(qtbot, QtBot)
        panel = LibraryPanel(session.library)
        qtbot.addWidget(panel)
        # No set_package_families call → the default provider has no catalogue.
        assert panel._choice.options(session.library.get("R")) == []

    def test_multi_option_double_click_opens_dialog_and_places(
        self, qtbot: object, session: EditorSession, monkeypatch: object,
    ) -> None:
        # >1 option → the pop-up opens; the chosen address is placed.
        from pytestqt.qtbot import QtBot
        assert isinstance(qtbot, QtBot)
        self._patch_dialog(monkeypatch, "DIN-0617/typ")
        panel = LibraryPanel(session.library)
        qtbot.addWidget(panel)
        panel.set_package_families(self._packages())
        r_item = self._select_R(panel)
        assert r_item is not None
        received: list[tuple] = []
        panel.component_selected.connect(
            lambda name, pinv, bodyv: received.append((name, pinv, bodyv))
        )
        panel._tree.itemDoubleClicked.emit(r_item, 0)  # type: ignore[arg-type]
        assert received == [("R", None, "DIN-0617/typ")]

    def test_dismissed_dialog_places_nothing(
        self, qtbot: object, session: EditorSession, monkeypatch: object,
    ) -> None:
        # Esc / Cancel on the pop-up → nothing is placed.
        from pytestqt.qtbot import QtBot
        assert isinstance(qtbot, QtBot)
        self._patch_dialog(monkeypatch, None)  # rejected
        panel = LibraryPanel(session.library)
        qtbot.addWidget(panel)
        panel.set_package_families(self._packages())
        r_item = self._select_R(panel)
        assert r_item is not None
        placed: list[tuple] = []
        panel.component_selected.connect(
            lambda name, pinv, bodyv: placed.append((name, pinv, bodyv))
        )
        panel._tree.itemDoubleClicked.emit(r_item, 0)  # type: ignore[arg-type]
        assert placed == []

    def test_single_package_places_with_no_dialog(
        self, qtbot: object, session: EditorSession, monkeypatch: object,
    ) -> None:
        # Exactly one option that is a package → placed directly, no pop-up.
        from pytestqt.qtbot import QtBot
        from protoboard.domain.components.package import (
            PackageFamily, PackageVariant,
        )
        from protoboard.domain.components.package.loader import build_package
        assert isinstance(qtbot, QtBot)
        opened = self._patch_dialog(monkeypatch, "should-not-be-used")

        def _cyl() -> object:
            return build_package(
                {
                    "shape": "cylinder",
                    "dims": {"diameter": 2.0, "length": 5.0}, "bend": 0,
                    "leads": {
                        "direction": [[0, 0, -1], [0, 0, 1]],
                        "body_offset": 0, "board_offset": "solve",
                        "lead_r": 0.25, "lead_length": 8.0,
                    },
                },
                "C",
            )

        panel = LibraryPanel(session.library)
        qtbot.addWidget(panel)
        panel.set_package_families({
            "DIN-0207": PackageFamily(
                family_id="DIN-0207", label="DIN 0207",
                variants=(PackageVariant("typ", "typ", _cyl()),),  # type: ignore[arg-type]
            ),
        })
        r_item = self._select_R(panel)
        assert r_item is not None
        placed: list[tuple] = []
        panel.component_selected.connect(
            lambda name, pinv, bodyv: placed.append((name, pinv, bodyv))
        )
        panel._tree.itemDoubleClicked.emit(r_item, 0)  # type: ignore[arg-type]
        assert placed == [("R", None, "DIN-0207/typ")]
        assert opened == []  # dialog never constructed

    def test_footprints_list_labels_are_symbol_name(
        self, qtbot: object, tmp_path: object,
    ) -> None:
        # A component with a footprints: list labels each option "symbol_name".
        from pathlib import Path
        from pytestqt.qtbot import QtBot
        from protoboard.services.library.loader import load_library
        assert isinstance(qtbot, QtBot) and isinstance(tmp_path, Path)
        (tmp_path / "ic.yaml").write_text(
            "components:\n"
            "  - name: U_DIP14\n"
            "    component_class: ic\n    symbol: U\n"
            "    pins: {count: 14}\n"
            "    footprints:\n"
            "      - name: DIP-14\n        package: {kind: dip_ic}\n"
            "      - name: WDIP-14\n        package: {kind: wdip_ic}\n"
            "    pin_names: {}\n"
        )
        lib = load_library([tmp_path])
        panel = LibraryPanel(lib)
        qtbot.addWidget(panel)
        labels = [o.label for o in panel._choice.options(lib.get("U_DIP14"))]
        assert labels == ["U_DIP-14", "U_WDIP-14"]

    def test_recently_used_footprint_offered_first(
        self, qtbot: object, session: EditorSession, monkeypatch: object,
    ) -> None:
        # A recent pick for this component is what the panel passes to the pop-up
        # as the recent list (rendered on top, ★).
        import pytest as _pytest
        from pytestqt.qtbot import QtBot
        from protoboard.gui import recent_footprints
        assert isinstance(qtbot, QtBot)
        assert isinstance(monkeypatch, _pytest.MonkeyPatch)
        seen = self._patch_dialog(monkeypatch, "DIN-0617/typ")
        monkeypatch.setattr(
            recent_footprints, "load",
            lambda: [recent_footprints.RecentFootprint("R", "DIN-0617/typ", "R_x")],
        )
        panel = LibraryPanel(session.library)
        qtbot.addWidget(panel)
        panel.set_package_families(self._packages())
        r_item = self._select_R(panel)
        assert r_item is not None
        panel._tree.itemDoubleClicked.emit(r_item, 0)  # type: ignore[arg-type]
        assert len(seen) == 1
        recent, _options = seen[0]
        assert [r.address for r in recent] == ["DIN-0617/typ"]


class TestBodyVariantDropdown:
    """The Properties panel offers a package-variant dropdown for TwoPin
    components when a package-family catalogue is provided."""

    def _make_defn_with_default(self) -> object:
        from protoboard.domain.components.definition import (
            ComponentClass, ComponentDefinition, PackageOption,
        )
        from protoboard.domain.components.footprints import SIL
        return ComponentDefinition.from_footprint(
            name="R", footprint=SIL(n=2),
            pin_names={}, component_class=ComponentClass.RESISTOR,
            package_options=(
                PackageOption(name="DIN-0207/typ", ref="DIN-0207/typ"),
            ),
        )

    def _families(self) -> dict:
        from protoboard.domain.components.package import (
            PackageFamily, PackageVariant,
        )
        from protoboard.domain.components.package.loader import build_package

        def _cyl(d: float, length: float) -> object:
            return build_package(
                {
                    "shape": "cylinder",
                    "dims": {"diameter": d, "length": length},
                    "bend": 0,
                    "leads": {
                        "direction": [[0, 0, -1], [0, 0, 1]],
                        "body_offset": 0, "board_offset": "solve",
                        "lead_r": 0.25, "lead_length": 8.0,
                    },
                },
                "C",
            )

        return {
            "DIN-0207": PackageFamily(
                family_id="DIN-0207", label="DIN 0207",
                variants=(
                    PackageVariant("S", "S", _cyl(2.0, 5.0)),  # type: ignore[arg-type]
                    PackageVariant("typ", "typ", _cyl(2.5, 6.5)),  # type: ignore[arg-type]
                ),
            ),
            "DIN-0617": PackageFamily(
                family_id="DIN-0617", label="DIN 0617",
                variants=(PackageVariant("typ", "typ", _cyl(5.0, 14.0)),),  # type: ignore[arg-type]
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
            package_families=self._families(),
        )
        assert len(panel._body_variant_addresses) > 0

    def test_dropdown_lists_default_first(self, qtbot: object) -> None:
        from pytestqt.qtbot import QtBot
        assert isinstance(qtbot, QtBot)
        panel = PropertiesPanel()
        qtbot.addWidget(panel)
        inst = ComponentInstance.new(definition_ref="R", refdes="R1", value="")
        panel.show_component(
            inst, defn=self._make_defn_with_default(),  # type: ignore[arg-type]
            package_families=self._families(),
        )
        # First entry is always "Default (library)" with None address.
        assert panel._body_variant_addresses[0] is None
        # DIN-0207 variants come right after (definition's default family).
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
        defn = ComponentDefinition.from_footprint(
            name="HDR3", footprint=SIL(3),
            pin_names={}, component_class=ComponentClass.CONNECTOR,
        )
        inst = ComponentInstance.new(definition_ref="HDR3", refdes="J1", value="")
        panel.show_component(
            inst, defn=defn, package_families=self._families(),
        )
        assert panel._body_variant_addresses == []

    def test_dropdown_hidden_when_no_catalogue(self, qtbot: object) -> None:
        from protoboard.domain.components.definition import (
            ComponentClass, ComponentDefinition,
        )
        from protoboard.domain.components.footprints import SIL
        from pytestqt.qtbot import QtBot
        assert isinstance(qtbot, QtBot)
        panel = PropertiesPanel()
        qtbot.addWidget(panel)
        inst = ComponentInstance.new(definition_ref="R", refdes="R1", value="")
        defn = ComponentDefinition.from_footprint(
            name="R", footprint=SIL(n=2), pin_names={},
            component_class=ComponentClass.RESISTOR,
        )
        panel.show_component(
            inst, defn=defn,
            package_families={},
        )
        assert panel._body_variant_addresses == []

    def test_changing_dropdown_emits_signal(self, qtbot: object) -> None:
        from pytestqt.qtbot import QtBot
        assert isinstance(qtbot, QtBot)
        panel = PropertiesPanel()
        qtbot.addWidget(panel)
        inst = ComponentInstance.new(
            definition_ref="R", refdes="R1", value="", package_variant=None,
        )
        panel.show_component(
            inst, defn=self._make_defn_with_default(),  # type: ignore[arg-type]
            package_families=self._families(),
        )
        received: list[tuple[uuid.UUID, object]] = []
        panel.package_variant_changed.connect(
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
            package_variant="DIN-0617/typ",
        )
        panel.show_component(
            inst, defn=self._make_defn_with_default(),  # type: ignore[arg-type]
            package_families=self._families(),
        )
        # Combo should pick the DIN-0617/typ entry, not Default.
        idx = panel._body_variant_combo.currentIndex()
        assert panel._body_variant_addresses[idx] == "DIN-0617/typ"


class TestPropertiesParameters:
    """The schema-driven Parameters section: one row per package schema field.

    A *placed* component always has a concrete package (a ``kind`` resolves its
    holes in the picker before placement), so every field is a set value →
    read-only. Read-only is signalled non-colour (a ``(set)`` suffix + a
    non-editable field). Editing-with-overrides is a separate deferred feature.
    """

    def _defn_with_schema_package(self):
        """A ComponentDefinition whose package carries a ParamSchema (all set)."""
        from protoboard.domain.components.definition import ComponentDefinition
        from protoboard.domain.components.footprints import SIL
        from protoboard.domain.components.package.loader import build_package

        merged = {
            "name": "P",
            "shape": "prism",
            "schema": {
                "dims": {
                    "ly": {"type": "float", "required": True},
                    "lx": {"type": "float", "required": True},
                },
            },
            "dims": {"ly": 7.62, "lx": 9.4},
            "leads": {
                "direction": [0, 0, -1],
                "body_offset": {"unit": 2.5, "value": [[1, 0]]},
                "board_offset": "default",
            },
        }
        pkg = build_package(merged, "P")
        return ComponentDefinition(
            name="P", pin_names={1: "1", 2: "2"},
            package=pkg,
        )

    def test_section_shows_one_row_per_field(self, qtbot: object) -> None:
        from pytestqt.qtbot import QtBot
        assert isinstance(qtbot, QtBot)
        panel = PropertiesPanel()
        qtbot.addWidget(panel)
        defn = self._defn_with_schema_package()
        inst = ComponentInstance.new(definition_ref="P", refdes="P1", value="")
        panel.show_component(inst, defn=defn, package_families={})
        # isHidden (not isVisible): the panel isn't shown in the test, so Qt
        # reports the group not-visible regardless of our setVisible(True);
        # isHidden tests our own show/hide call, not parent-tree visibility.
        assert panel._params_grp.isHidden() is False
        assert set(panel._param_widgets) == {"dims.ly", "dims.lx"}

    def test_set_field_is_readonly_with_value(self, qtbot: object) -> None:
        from pytestqt.qtbot import QtBot
        assert isinstance(qtbot, QtBot)
        panel = PropertiesPanel()
        qtbot.addWidget(panel)
        defn = self._defn_with_schema_package()
        inst = ComponentInstance.new(definition_ref="P", refdes="P1", value="")
        panel.show_component(inst, defn=defn, package_families={})
        w = panel._param_widgets["dims.lx"]
        assert w.isReadOnly() is True  # a placed part's fields are set -> RO
        assert w.text() == "9.4"

    def test_hidden_without_schema(self, qtbot: object) -> None:
        from pytestqt.qtbot import QtBot
        assert isinstance(qtbot, QtBot)
        panel = PropertiesPanel()
        qtbot.addWidget(panel)
        inst = ComponentInstance.new(definition_ref="R", refdes="R1", value="")
        panel.show_component(inst, package_families={})
        assert panel._params_grp.isVisible() is False

    def test_clear_hides_section(self, qtbot: object) -> None:
        from pytestqt.qtbot import QtBot
        assert isinstance(qtbot, QtBot)
        panel = PropertiesPanel()
        qtbot.addWidget(panel)
        defn = self._defn_with_schema_package()
        inst = ComponentInstance.new(definition_ref="P", refdes="P1", value="")
        panel.show_component(inst, defn=defn, package_families={})
        panel.clear_component()
        assert panel._params_grp.isVisible() is False
        assert panel._param_widgets == {}
