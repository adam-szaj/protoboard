# SPDX-License-Identifier: MIT
"""Unit tests for the namespaced KeyMap model (keybindings redesign — Stage 1).

Pure logic: two-level resolution (override > declared default > inherited
global), the ``final`` guard, event matching, and tree-YAML round-trip.  No
SelectTool / BoardView here — just the model.
"""

from __future__ import annotations

import pytest
from PySide6.QtCore import Qt

from protoboard.gui.keymap import (
    FinalShortcutError,
    GlobalKeyMap,
    KeyMap,
    category_of,
    name_of,
)

_NOMOD = Qt.KeyboardModifier.NoModifier


def _chain() -> KeyMap:
    """A leaf over the built-in globals — the app's base-map shape."""
    return KeyMap(GlobalKeyMap.with_globals())


def _tool_map() -> KeyMap:
    km = _chain()
    km.register_tool_actions(
        {
            "select-tool.action.edit": None,  # inherit global.action.edit
            "select-tool.action.reroute": "W",  # override
            "select-tool.action.delete": None,  # inherit global.action.delete
        }
    )
    return km


class TestIdHelpers:
    def test_category_and_name(self) -> None:
        assert category_of("select-tool.action.reroute") == "select-tool"
        assert name_of("select-tool.action.reroute") == "reroute"
        assert category_of("global.action.delete") == "global"
        assert name_of("global.action.delete") == "delete"


class TestResolve:
    def test_override_value_wins(self) -> None:
        assert _tool_map().resolve("select-tool.action.reroute") == "W"

    def test_none_inherits_global_default(self) -> None:
        km = _tool_map()
        assert km.resolve("select-tool.action.edit") == "E"  # global edit
        assert km.resolve("select-tool.action.delete") == "Del"  # global delete

    def test_unknown_action_resolves_empty(self) -> None:
        assert _tool_map().resolve("nope.action.x") == ""

    def test_user_override_beats_default(self) -> None:
        km = _tool_map()
        km.set_override("select-tool.action.reroute", "Q")
        assert km.resolve("select-tool.action.reroute") == "Q"

    def test_global_override_propagates_to_inheritors(self) -> None:
        km = _tool_map()
        km.set_override("global.action.delete", "X")
        # The tool action inherits the global → follows the override.
        assert km.resolve("select-tool.action.delete") == "X"

    def test_empty_override_means_unbound(self) -> None:
        km = _tool_map()
        km.set_override("select-tool.action.reroute", "")
        assert km.resolve("select-tool.action.reroute") == ""


class TestMatches:
    def test_matches_resolved_shortcut(self) -> None:
        km = _tool_map()
        assert km.matches(Qt.Key.Key_W.value, _NOMOD, "select-tool.action.reroute")
        assert km.matches(Qt.Key.Key_Delete.value, _NOMOD, "select-tool.action.delete")

    def test_no_match_for_other_key(self) -> None:
        km = _tool_map()
        assert not km.matches(Qt.Key.Key_Q.value, _NOMOD, "select-tool.action.reroute")

    def test_unbound_never_matches(self) -> None:
        km = _tool_map()
        km.set_override("select-tool.action.reroute", "")
        assert not km.matches(Qt.Key.Key_W.value, _NOMOD, "select-tool.action.reroute")


class TestFinal:
    def test_rejects_tool_binding_colliding_with_final(self) -> None:
        km = _chain()  # session.action.save_as = Ctrl+Shift+S, final
        with pytest.raises(FinalShortcutError):
            km.register_tool_actions({"wire.action.simplify": "Ctrl+Shift+S"})

    def test_none_binding_never_rejected(self) -> None:
        km = _chain()
        # None inherits — can't collide with a final shortcut.
        km.register_tool_actions({"wire.action.whatever": None})

    def test_non_final_shortcut_is_allowed(self) -> None:
        km = _chain()
        km.register_tool_actions({"wire.action.simplify": "Ctrl+Shift+Y"})
        assert km.resolve("wire.action.simplify") == "Ctrl+Shift+Y"

    def test_is_final_flag(self) -> None:
        km = _chain()
        assert km.is_final("session.action.save_as") is True
        assert km.is_final("global.action.delete") is False


class TestGlobalTransformActions:
    """The generic rotate/flip/redrag globals two tools inherit (Stage 1)."""

    def test_transform_defaults_match_legacy(self) -> None:
        km = _chain()
        assert km.resolve("global.action.rotate_cw") == "R"
        assert km.resolve("global.action.rotate_ccw") == "Shift+R"
        assert km.resolve("global.action.flip") == "Alt+R"
        assert km.resolve("global.action.redrag") == "Ctrl+R"

    def test_tool_inherits_transform_global(self) -> None:
        km = _chain()
        km.register_tool_actions({"place-tool.action.rotate_cw": None})
        assert km.resolve("place-tool.action.rotate_cw") == "R"
        km.set_override("global.action.rotate_cw", "K")
        assert km.resolve("place-tool.action.rotate_cw") == "K"


class TestPersistence:
    def test_save_then_load_round_trip(self, tmp_path, monkeypatch) -> None:
        import protoboard.paths as paths

        cfg = tmp_path / "config"
        monkeypatch.setattr(paths, "CONFIG_DIR", cfg)
        monkeypatch.setattr(paths, "KEYBINDINGS_V2_FILE", cfg / "keybindings_v2.yaml")

        km = _tool_map()
        km.set_override("select-tool.action.reroute", "Q")
        km.set_override("global.action.delete", "X")
        km.save_overrides()

        km2 = _tool_map()
        km2.load_overrides()
        assert km2.resolve("select-tool.action.reroute") == "Q"
        assert km2.resolve("select-tool.action.delete") == "X"

    def test_default_value_not_persisted(self, tmp_path, monkeypatch) -> None:
        import protoboard.paths as paths

        cfg = tmp_path / "config"
        monkeypatch.setattr(paths, "CONFIG_DIR", cfg)
        f = cfg / "keybindings_v2.yaml"
        monkeypatch.setattr(paths, "KEYBINDINGS_V2_FILE", f)

        km = _tool_map()
        # Override equals the declared default → should not be written.
        km.set_override("select-tool.action.reroute", "W")
        km.save_overrides()
        text = f.read_text() if f.exists() else ""
        assert "reroute" not in text
