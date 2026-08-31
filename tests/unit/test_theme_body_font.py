# SPDX-License-Identifier: MIT
"""Tests for Theme body_print_max_height default."""

from __future__ import annotations

from pathlib import Path

from protoboard.controller.view_options.theme import DEFAULT_THEME, Theme
from protoboard.services.preferences.theme_yaml import load_theme, save_theme


def test_body_print_max_height_default() -> None:
    assert Theme().body_print_max_height == DEFAULT_THEME.body_print_max_height


def test_theme_yaml_round_trip_preserves_custom_values(tmp_path: Path) -> None:
    path = tmp_path / "theme.yaml"
    theme = Theme(board_bg="#FF010203", overlay_font_size=19.5)

    save_theme(theme, path)

    loaded = load_theme(path)
    assert loaded.board_bg == "#FF010203"
    assert loaded.overlay_font_size == 19.5


def test_invalid_theme_yaml_falls_back_to_default(tmp_path: Path) -> None:
    path = tmp_path / "theme.yaml"
    path.write_text("- not\n- a\n- mapping\n", encoding="utf-8")

    assert load_theme(path) is DEFAULT_THEME
