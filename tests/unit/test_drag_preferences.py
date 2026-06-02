# SPDX-License-Identifier: MIT
"""Tests for protoboard.gui.drag_preferences and AppSettings drag fields.

Covers:
* defaults from AppSettings,
* DragPreferences.from_app_settings,
* DragSessionOverrides.effective(base),
* AppSettings YAML round-trip for drag fields,
* invalid YAML values fall back to defaults.
"""

from __future__ import annotations

from pathlib import Path

import pytest
from ruamel.yaml import YAML  # type: ignore[import-untyped]

from protoboard.gui import app_settings as app_settings_mod
from protoboard.gui.app_settings import AppSettings, load_app_settings, save_app_settings
from protoboard.gui.drag_preferences import (
    DRAG_CONFLICT_VALUES,
    DragPreferences,
    DragSessionOverrides,
)


@pytest.fixture
def isolated_app_settings(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> Path:
    """Redirect AppSettings persistence to a temp file."""
    target = tmp_path / "app_settings.yaml"
    monkeypatch.setattr(app_settings_mod, "_CONFIG_PATH", target)
    return target


# ---------------------------------------------------------------------------
# AppSettings defaults
# ---------------------------------------------------------------------------


class TestAppSettingsDragDefaults:
    def test_defaults_are_conservative(self) -> None:
        s = AppSettings()
        assert s.drag_free_wire_conflict == "auto-remove"
        assert s.drag_stationary_group_wire_conflict == "ask-remove"
        assert s.drag_cross_group_disconnect == "ask-reroute"
        assert s.drag_default_lock_internal_wires is False

    def test_default_values_are_in_allowed_set(self) -> None:
        s = AppSettings()
        for v in (
            s.drag_free_wire_conflict,
            s.drag_stationary_group_wire_conflict,
            s.drag_cross_group_disconnect,
        ):
            assert v in DRAG_CONFLICT_VALUES


# ---------------------------------------------------------------------------
# DragPreferences.from_app_settings
# ---------------------------------------------------------------------------


class TestDragPreferencesFromAppSettings:
    def test_field_mapping(self) -> None:
        s = AppSettings()
        s.drag_free_wire_conflict = "ask-remove"
        s.drag_stationary_group_wire_conflict = "block"
        s.drag_cross_group_disconnect = "auto-reroute"
        s.drag_default_lock_internal_wires = True

        prefs = DragPreferences.from_app_settings(s)
        assert prefs.free_wire == "ask-remove"
        assert prefs.stationary_group_wire == "block"
        assert prefs.cross_group_disconnect == "auto-reroute"
        assert prefs.default_lock_internal_wires is True


# ---------------------------------------------------------------------------
# DragSessionOverrides.effective
# ---------------------------------------------------------------------------


class TestDragSessionOverrides:
    def test_empty_overrides_returns_base(self) -> None:
        base = DragPreferences(
            free_wire="auto-remove",
            stationary_group_wire="ask-remove",
            cross_group_disconnect="ask-reroute",
            default_lock_internal_wires=False,
        )
        overrides = DragSessionOverrides()
        assert overrides.effective(base) == base

    def test_one_override_replaces_one_field(self) -> None:
        base = DragPreferences(
            free_wire="auto-remove",
            stationary_group_wire="ask-remove",
            cross_group_disconnect="ask-reroute",
            default_lock_internal_wires=False,
        )
        overrides = DragSessionOverrides(cross_group_disconnect="auto-reroute")
        eff = overrides.effective(base)
        assert eff.cross_group_disconnect == "auto-reroute"
        # Untouched fields preserved.
        assert eff.free_wire == "auto-remove"
        assert eff.stationary_group_wire == "ask-remove"
        assert eff.default_lock_internal_wires is False

    def test_all_overrides_replace_all_fields(self) -> None:
        base = DragPreferences(
            free_wire="auto-remove",
            stationary_group_wire="ask-remove",
            cross_group_disconnect="ask-reroute",
            default_lock_internal_wires=False,
        )
        overrides = DragSessionOverrides(
            free_wire="block",
            stationary_group_wire="auto-remove",
            cross_group_disconnect="auto-reroute",
        )
        eff = overrides.effective(base)
        assert eff.free_wire == "block"
        assert eff.stationary_group_wire == "auto-remove"
        assert eff.cross_group_disconnect == "auto-reroute"
        # default_lock_internal_wires is NOT a session-override-able field.
        assert eff.default_lock_internal_wires is False

    def test_clear_resets_all_overrides(self) -> None:
        overrides = DragSessionOverrides(
            free_wire="block",
            stationary_group_wire="auto-remove",
            cross_group_disconnect="auto-reroute",
        )
        overrides.clear()
        assert overrides.free_wire is None
        assert overrides.stationary_group_wire is None
        assert overrides.cross_group_disconnect is None


# ---------------------------------------------------------------------------
# AppSettings YAML round-trip
# ---------------------------------------------------------------------------


class TestAppSettingsYamlRoundTrip:
    def test_drag_fields_persisted_and_loaded(
        self, isolated_app_settings: Path,
    ) -> None:
        s = AppSettings()
        s.drag_free_wire_conflict = "ask-remove"
        s.drag_stationary_group_wire_conflict = "auto-reroute"
        s.drag_cross_group_disconnect = "block"
        s.drag_default_lock_internal_wires = True

        save_app_settings(s)
        loaded = load_app_settings()
        assert loaded.drag_free_wire_conflict == "ask-remove"
        assert loaded.drag_stationary_group_wire_conflict == "auto-reroute"
        assert loaded.drag_cross_group_disconnect == "block"
        assert loaded.drag_default_lock_internal_wires is True

    def test_yaml_uses_drag_block_structure(
        self, isolated_app_settings: Path,
    ) -> None:
        s = AppSettings()
        save_app_settings(s)
        data = YAML().load(isolated_app_settings)
        assert "drag" in data
        assert data["drag"]["free_wire_conflict"] == "auto-remove"
        assert data["drag"]["stationary_group_wire_conflict"] == "ask-remove"
        assert data["drag"]["cross_group_disconnect"] == "ask-reroute"
        assert data["drag"]["default_lock_internal_wires"] is False

    def test_invalid_yaml_value_falls_back_to_default(
        self, isolated_app_settings: Path,
    ) -> None:
        # Write garbage YAML and confirm load falls back to defaults
        # for the bad field while preserving the valid one.
        isolated_app_settings.write_text(
            "drag:\n"
            "  free_wire_conflict: not-a-valid-value\n"
            "  stationary_group_wire_conflict: ask-remove\n"
            "  cross_group_disconnect: block\n"
            "  default_lock_internal_wires: true\n",
            encoding="utf-8",
        )
        loaded = load_app_settings()
        # Bad value → default.
        assert loaded.drag_free_wire_conflict == "auto-remove"
        # Good values preserved.
        assert loaded.drag_stationary_group_wire_conflict == "ask-remove"
        assert loaded.drag_cross_group_disconnect == "block"
        assert loaded.drag_default_lock_internal_wires is True

    def test_missing_drag_block_uses_defaults(
        self, isolated_app_settings: Path,
    ) -> None:
        # YAML without `drag:` section — defaults apply.
        isolated_app_settings.write_text(
            "auto_accept_placement: true\n",
            encoding="utf-8",
        )
        loaded = load_app_settings()
        assert loaded.drag_free_wire_conflict == "auto-remove"
        assert loaded.drag_stationary_group_wire_conflict == "ask-remove"
        assert loaded.drag_cross_group_disconnect == "ask-reroute"
        assert loaded.drag_default_lock_internal_wires is False

    def test_partial_drag_block_keeps_unspecified_defaults(
        self, isolated_app_settings: Path,
    ) -> None:
        isolated_app_settings.write_text(
            "drag:\n  cross_group_disconnect: auto-reroute\n",
            encoding="utf-8",
        )
        loaded = load_app_settings()
        assert loaded.drag_cross_group_disconnect == "auto-reroute"
        assert loaded.drag_free_wire_conflict == "auto-remove"
        assert loaded.drag_stationary_group_wire_conflict == "ask-remove"
        assert loaded.drag_default_lock_internal_wires is False
