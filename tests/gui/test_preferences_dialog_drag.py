# SPDX-License-Identifier: MIT
"""Tests for the Drag tab in PreferencesDialog."""

from __future__ import annotations

from pathlib import Path

import pytest

from protoboard.gui import app_settings as app_settings_mod
from protoboard.gui.app_settings import AppSettings
from protoboard.gui.dialogs.preferences_dialog import _DragPage


@pytest.fixture
def isolated_app_settings(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path,
) -> Path:
    target = tmp_path / "app_settings.yaml"
    monkeypatch.setattr(app_settings_mod, "_CONFIG_PATH", target)
    return target


class TestDragPage:
    def test_combos_reflect_initial_settings(
        self, isolated_app_settings: Path, qtbot,
    ) -> None:
        s = AppSettings()
        s.drag_free_wire_conflict = "ask-remove"
        s.drag_stationary_group_wire_conflict = "block"
        s.drag_cross_group_disconnect = "auto-reroute"
        page = _DragPage(s)
        qtbot.addWidget(page)
        assert page._free_combo.currentData() == "ask-remove"
        assert page._sgroup_combo.currentData() == "block"
        assert page._disconnect_combo.currentData() == "auto-reroute"

    def test_apply_persists_combos_to_settings(
        self, isolated_app_settings: Path, qtbot,
    ) -> None:
        s = AppSettings()
        page = _DragPage(s)
        qtbot.addWidget(page)
        # Change selections via item data lookup.
        for combo, target in (
            (page._free_combo, "ask-remove"),
            (page._sgroup_combo, "auto-reroute"),
            (page._disconnect_combo, "block"),
        ):
            for i in range(combo.count()):
                if combo.itemData(i) == target:
                    combo.setCurrentIndex(i)
                    break
        page._lock_default_cb.setChecked(True)
        page.apply()
        assert s.drag_free_wire_conflict == "ask-remove"
        assert s.drag_stationary_group_wire_conflict == "auto-reroute"
        assert s.drag_cross_group_disconnect == "block"
        assert s.drag_default_lock_internal_wires is True

    def test_all_combo_options_available(
        self, isolated_app_settings: Path, qtbot,
    ) -> None:
        page = _DragPage(AppSettings())
        qtbot.addWidget(page)
        for combo in (
            page._free_combo, page._sgroup_combo, page._disconnect_combo,
        ):
            values = {combo.itemData(i) for i in range(combo.count())}
            assert values == {
                "auto-remove",
                "ask-remove",
                "auto-reroute",
                "ask-reroute",
                "block",
            }
