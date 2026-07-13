# SPDX-License-Identifier: MIT
"""Tests for render-throttle persistence in AppSettings.

The three throttle knobs (``cursor_label_throttle_ms``,
``rubber_band_throttle_ms``, ``zoom_refresh_ms``) round-trip
through ``app_settings.yaml`` like the rest of the preferences.
"""

from __future__ import annotations

import pytest

from protoboard.controller.app_settings import (
    AppSettings,
    load_app_settings,
    save_app_settings,
)


@pytest.fixture(autouse=True)
def _isolate_settings_file(tmp_path, monkeypatch):
    """Redirect both load and save to a fresh path per test."""
    target = tmp_path / "app_settings.yaml"
    monkeypatch.setattr("protoboard.controller.app_settings._CONFIG_PATH", target)
    return target


class TestDefaults:
    def test_defaults_are_sane(self) -> None:
        s = AppSettings()
        assert s.cursor_label_throttle_ms == 33
        assert s.rubber_band_throttle_ms == 33
        assert s.zoom_refresh_ms == 80


class TestRoundTrip:
    def test_custom_values_roundtrip(self) -> None:
        original = AppSettings(
            cursor_label_throttle_ms=120,
            rubber_band_throttle_ms=60,
            zoom_refresh_ms=200,
        )
        save_app_settings(original)
        loaded = load_app_settings()
        assert loaded.cursor_label_throttle_ms == 120
        assert loaded.rubber_band_throttle_ms == 60
        assert loaded.zoom_refresh_ms == 200

    def test_missing_performance_section_uses_defaults(self, _isolate_settings_file):
        _isolate_settings_file.write_text("default_projects_dir: ~/projects\n")
        loaded = load_app_settings()
        # The other field came through; throttles stayed at defaults.
        assert loaded.cursor_label_throttle_ms == 33
        assert loaded.rubber_band_throttle_ms == 33
        assert loaded.zoom_refresh_ms == 80

    def test_show_selection_bbox_roundtrip(self) -> None:
        assert AppSettings().show_selection_bbox is True  # default on
        save_app_settings(AppSettings(show_selection_bbox=False))
        assert load_app_settings().show_selection_bbox is False


class TestWireDefaults:
    def test_defaults_are_bare_and_bottom(self) -> None:
        s = AppSettings()
        assert s.wire_default_insulated is False
        assert s.wire_default_side == "bottom"

    def test_roundtrip(self) -> None:
        save_app_settings(
            AppSettings(wire_default_insulated=True, wire_default_side="top")
        )
        loaded = load_app_settings()
        assert loaded.wire_default_insulated is True
        assert loaded.wire_default_side == "top"

    def test_missing_wire_section_uses_defaults(self, _isolate_settings_file):
        _isolate_settings_file.write_text("default_projects_dir: ~/projects\n")
        loaded = load_app_settings()
        assert loaded.wire_default_insulated is False
        assert loaded.wire_default_side == "bottom"

    def test_garbage_side_ignored(self, _isolate_settings_file):
        _isolate_settings_file.write_text("wire:\n  default_side: sideways\n")
        loaded = load_app_settings()
        # An invalid side falls back to the default rather than cementing.
        assert loaded.wire_default_side == "bottom"


class TestClamping:
    def test_garbage_value_clamped(self, _isolate_settings_file):
        """A hand-edited YAML with an out-of-range value cannot lock
        the UI — the loader clamps to [0, 2000]."""
        _isolate_settings_file.write_text(
            "performance:\n  cursor_label_throttle_ms: 99999\n"
        )
        loaded = load_app_settings()
        assert loaded.cursor_label_throttle_ms == 2000

    def test_negative_value_clamped_to_zero(self, _isolate_settings_file):
        _isolate_settings_file.write_text(
            "performance:\n  rubber_band_throttle_ms: -50\n"
        )
        loaded = load_app_settings()
        assert loaded.rubber_band_throttle_ms == 0

    def test_non_numeric_value_ignored(self, _isolate_settings_file):
        _isolate_settings_file.write_text(
            "performance:\n  zoom_refresh_ms: 'fast please'\n"
        )
        loaded = load_app_settings()
        # Default preserved.
        assert loaded.zoom_refresh_ms == 80
