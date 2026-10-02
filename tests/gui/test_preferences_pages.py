# SPDX-License-Identifier: MIT
"""Preferences page editing survives changes to page organization."""

from __future__ import annotations

from PySide6.QtWidgets import QInputDialog

from protoboard.controller import app_settings as app_settings_mod
from protoboard.controller.app_settings import AppSettings
from protoboard.controller.view_options.theme import Theme
from protoboard.domain.routing_settings import RoutingSettings
from protoboard.gui.dialogs import preferences_dialog
from protoboard.gui.dialogs.preferences_dialog import _ColorsPage, _RoutingPage


def test_routing_apply_preserves_settings_owned_by_autoroute(qtbot, session) -> None:
    session.routing_settings = RoutingSettings(
        router_name="astar_enhanced",
        optimizer_chain=("unweave",),
        cost_overrides={"bend_bare": 3.0},
    )
    page = _RoutingPage(session, AppSettings())
    qtbot.addWidget(page)
    page._preferred_side.setCurrentText("top")
    page._skip_autoroute_dialog.setChecked(True)
    page.apply()

    assert session.routing_settings.preferred_side == "top"
    assert session.routing_settings.skip_autoroute_dialog is True
    assert session.routing_settings.router_name == "astar_enhanced"
    assert session.routing_settings.optimizer_chain == ("unweave",)
    assert session.routing_settings.cost_overrides == {"bend_bare": 3.0}


def test_save_routing_overrides_updates_shared_presets(
    qtbot, session, tmp_path, monkeypatch
) -> None:
    target = tmp_path / "app_settings.yaml"
    monkeypatch.setattr(app_settings_mod, "_CONFIG_PATH", target)
    monkeypatch.setattr(QInputDialog, "getText", lambda *args: ("Custom", True))
    settings = AppSettings()
    page = _RoutingPage(session, settings)
    qtbot.addWidget(page)
    key, spin = next(iter(page._override_spinboxes.items()))
    spin.setValue(-1.0)
    page._save_overrides_as_preset()
    page.apply()

    assert settings.cost_presets["Custom"][key] == float("inf")
    assert target.exists()
    assert session.routing_settings.cost_preset == "Custom"
    assert session.routing_settings.cost_overrides == {}


def test_colors_apply_edits_across_sections(qtbot, monkeypatch) -> None:
    saved: list[Theme] = []
    monkeypatch.setattr(preferences_dialog, "save_theme", saved.append)
    theme = Theme()
    page = _ColorsPage(theme)
    qtbot.addWidget(page)
    page._widgets["board_bg"].set_color("#ff123456")
    page._widgets["overlay_font_size"].setValue(17.0)
    page._widgets["back_alpha"].setValue(43)
    page._widgets["body_tab_opacity"].setValue(62)
    page._net_color_btns[0].set_color("#ffabcdef")
    page.apply()

    assert theme.board_bg == "#ff123456"
    assert theme.overlay_font_size == 17.0
    assert theme.back_alpha == 0.43
    assert theme.body_tab_opacity == 0.62
    assert theme.net_colors[0] == "#ffabcdef"
    assert saved == [theme]


def test_colors_restore_defaults_restores_edited_controls(qtbot, monkeypatch) -> None:
    monkeypatch.setattr(preferences_dialog, "save_theme", lambda theme: None)
    theme = Theme(board_bg="#ff123456", overlay_font_size=17.0, back_alpha=0.43)
    page = _ColorsPage(theme)
    qtbot.addWidget(page)
    page._reset()
    page.apply()
    defaults = Theme()

    assert theme.board_bg == defaults.board_bg.lower()
    assert theme.overlay_font_size == defaults.overlay_font_size
    assert theme.back_alpha == round(defaults.back_alpha * 100) / 100
    assert theme.net_colors == [color.lower() for color in defaults.net_colors]
