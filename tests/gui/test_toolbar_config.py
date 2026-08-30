# SPDX-License-Identifier: MIT
"""Tests for :mod:`protoboard.gui.toolbar_config` — YAML round-trip,
fallback behaviour and unknown-group handling."""

from __future__ import annotations

from pathlib import Path

from protoboard.gui.toolbar_config import (
    ensure_user_toolbars_file_exists,
    load_default_toolbar_config,
    load_toolbar_config,
    save_toolbar_config,
)

_VALID_YAML = """
view:
  - camera_toggle
  - mirror_h
wire:
  - wire_toggle_insulated
"""

_MALFORMED_YAML = "not: a: valid: yaml: tree"

_UNKNOWN_GROUP_YAML = """
ghost:
  - some_action
view:
  - camera_toggle
"""


class TestLoadToolbarConfig:
    def test_loads_user_file_when_present(self, tmp_path: Path) -> None:
        user = tmp_path / "toolbars.yaml"
        default = tmp_path / "default_toolbars.yaml"
        user.write_text(_VALID_YAML)
        # default also present — user wins.
        default.write_text("view: [zoom_in]\n")

        out = load_toolbar_config(user_path=user, default_path=default)
        assert out == {
            "view": ["camera_toggle", "mirror_h"],
            "wire": ["wire_toggle_insulated"],
        }

    def test_falls_back_to_default_when_user_missing(self, tmp_path: Path) -> None:
        user = tmp_path / "toolbars.yaml"
        default = tmp_path / "default_toolbars.yaml"
        default.write_text(_VALID_YAML)

        out = load_toolbar_config(user_path=user, default_path=default)
        assert out["view"] == ["camera_toggle", "mirror_h"]

    def test_empty_when_neither_source_exists(self, tmp_path: Path) -> None:
        out = load_toolbar_config(
            user_path=tmp_path / "u.yaml",
            default_path=tmp_path / "d.yaml",
        )
        assert out == {}

    def test_malformed_yaml_falls_back_to_default(self, tmp_path: Path) -> None:
        user = tmp_path / "u.yaml"
        default = tmp_path / "d.yaml"
        user.write_text(_MALFORMED_YAML)
        default.write_text(_VALID_YAML)

        out = load_toolbar_config(user_path=user, default_path=default)
        assert "view" in out
        assert out["view"] == ["camera_toggle", "mirror_h"]

    def test_unknown_group_silently_dropped(self, tmp_path: Path) -> None:
        user = tmp_path / "u.yaml"
        user.write_text(_UNKNOWN_GROUP_YAML)

        out = load_toolbar_config(user_path=user, default_path=tmp_path / "d.yaml")
        # The 'ghost' group is dropped; the 'view' group survives.
        assert "ghost" not in out
        assert out.get("view") == ["camera_toggle"]


class TestSaveToolbarConfig:
    def test_round_trip(self, tmp_path: Path) -> None:
        user = tmp_path / "u.yaml"
        config = {
            "view": ["camera_toggle", "mirror_h"],
            "wire": [],
            "tools": ["tool_select"],
        }
        save_toolbar_config(config, user_path=user)  # type: ignore[arg-type]
        out = load_toolbar_config(user_path=user, default_path=tmp_path / "d.yaml")
        assert out["view"] == ["camera_toggle", "mirror_h"]
        assert out.get("wire", []) == []
        assert out["tools"] == ["tool_select"]

    def test_save_writes_every_known_group(self, tmp_path: Path) -> None:
        # A sparse input should still emit every known group with an
        # empty list — the file documents the full surface.
        user = tmp_path / "u.yaml"
        save_toolbar_config({"view": ["camera_toggle"]}, user_path=user)  # type: ignore[arg-type]
        text = user.read_text(encoding="utf-8")
        for group in (
            "tools",
            "selection",
            "view",
            "zoom",
            "wire",
            "component",
            "analysis",
        ):
            assert f"{group}:" in text


class TestBootstrap:
    def test_copies_default_on_first_start(self, tmp_path: Path) -> None:
        user = tmp_path / "u.yaml"
        default = tmp_path / "d.yaml"
        default.write_text(_VALID_YAML)
        ensure_user_toolbars_file_exists(user_path=user, default_path=default)
        assert user.exists()
        assert user.read_text() == _VALID_YAML

    def test_no_overwrite_when_user_file_exists(self, tmp_path: Path) -> None:
        user = tmp_path / "u.yaml"
        default = tmp_path / "d.yaml"
        user.write_text("user_edited: true\n")
        default.write_text(_VALID_YAML)
        ensure_user_toolbars_file_exists(user_path=user, default_path=default)
        # User file untouched.
        assert user.read_text() == "user_edited: true\n"

    def test_no_default_no_crash(self, tmp_path: Path) -> None:
        # Bootstrap should not raise when neither file is present —
        # the application still has to start.
        user = tmp_path / "u.yaml"
        default = tmp_path / "d.yaml"
        ensure_user_toolbars_file_exists(user_path=user, default_path=default)
        assert not user.exists()


class TestLoadDefaults:
    def test_load_default_returns_shipped_config(self) -> None:
        # The shipped default file ships with the package — it must load
        # without raising and contain known groups.
        out = load_default_toolbar_config()
        # The exact contents may evolve; the spec is: at least one group
        # present, all keys in KNOWN_GROUPS.
        assert isinstance(out, dict)
        from protoboard.gui.toolbar_config import KNOWN_GROUPS

        assert set(out.keys()) <= KNOWN_GROUPS
