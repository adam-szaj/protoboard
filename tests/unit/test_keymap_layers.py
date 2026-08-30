# SPDX-License-Identifier: MIT
"""Per-tool KeyMap layer semantics (docs/keymap-per-tool-layer.md).

Pure model tests: layering (``KeyMap.layer_over``), the shared overrides
store, shadowing vs. the additive union on ``final`` actions, and the
``_Tool.make_key_map`` factory's best-effort registration.
"""

from __future__ import annotations

import pytest
from PySide6.QtCore import Qt

from protoboard.gui.board_view_tool import _DefaultTool
from protoboard.gui.keymap import (
    FinalShortcutError,
    GlobalKeyMap,
    KeyMap,
)

_NOMOD = Qt.KeyboardModifier.NoModifier


def _base() -> KeyMap:
    """The app's base-map shape: a leaf over the built-in globals."""
    return KeyMap(GlobalKeyMap.with_globals())


class TestLayerOver:
    def test_layer_declaration_shadows_parent(self) -> None:
        base = _base()
        base.register_tool_actions({"x-tool.action.zap": "Z"})
        layer = KeyMap.layer_over(base)
        layer.register_tool_actions({"x-tool.action.zap": "Y"})
        assert layer.resolve("x-tool.action.zap") == "Y"
        # The base itself is untouched — pop restores the old binding.
        assert base.resolve("x-tool.action.zap") == "Z"

    def test_undeclared_falls_through_to_parent(self) -> None:
        base = _base()
        base.register_tool_actions({"view.action.zoom_in": "Ctrl++"})
        layer = KeyMap.layer_over(base)
        assert layer.resolve("view.action.zoom_in") == "Ctrl++"
        assert layer.resolve("global.action.edit") == "E"

    def test_user_override_beats_layer_declaration(self) -> None:
        # The trap layer_over exists for: a fresh layer's literal declaration
        # must not shadow a user rebind living on the base map.
        base = _base()
        base.set_override("a-tool.action.reroute", "Q")
        layer = KeyMap.layer_over(base)
        layer.register_tool_actions({"a-tool.action.reroute": "W"})
        assert layer.resolve("a-tool.action.reroute") == "Q"

    def test_override_set_via_layer_lands_in_shared_store(self) -> None:
        base = _base()
        layer = KeyMap.layer_over(base)
        layer.register_tool_actions({"a-tool.action.reroute": "W"})
        layer.set_override("a-tool.action.reroute", "Q")
        # Visible from the base (persistence lives there) …
        assert base.overrides_store["a-tool.action.reroute"] == "Q"
        # … and from a *future* layer of the same tool.
        relayer = KeyMap.layer_over(base)
        relayer.register_tool_actions({"a-tool.action.reroute": "W"})
        assert relayer.resolve("a-tool.action.reroute") == "Q"


class TestFinalOnLayers:
    def test_layer_may_not_claim_final_trigger(self) -> None:
        layer = KeyMap.layer_over(_base())
        with pytest.raises(FinalShortcutError):
            layer.register_tool_actions({"x-tool.action.boom": "Esc"})

    def test_layer_extends_final_action_with_new_trigger(self) -> None:
        layer = KeyMap.layer_over(_base())
        layer.register_tool_actions({"global.action.cancel": "Q"})
        # Union, base trigger first — Esc can never be shadowed away.
        assert layer.resolve("global.action.cancel") == "Esc|Q"
        assert layer.matches(Qt.Key.Key_Escape.value, _NOMOD, "global.action.cancel")
        assert layer.matches(Qt.Key.Key_Q.value, _NOMOD, "global.action.cancel")

    def test_extended_final_action_stays_final(self) -> None:
        layer = KeyMap.layer_over(_base())
        layer.register_tool_actions({"global.action.cancel": "Q"})
        assert layer.is_final("global.action.cancel") is True

    def test_final_action_cannot_be_rebound(self) -> None:
        with pytest.raises(FinalShortcutError):
            _base().set_override("global.action.cancel", "Q")

    def test_override_may_not_claim_final_trigger(self) -> None:
        with pytest.raises(FinalShortcutError):
            _base().set_override("x-tool.action.boom", "Esc")


class TestLookup:
    """Reverse lookup: event → action ids, leaf-first (the shadowing order)."""

    def test_layer_claims_come_before_base(self) -> None:
        base = _base()
        base.register_tool_actions({"view.action.zoom": "K"})
        layer = KeyMap.layer_over(base)
        layer.register_tool_actions({"x-tool.action.spin": "K"})
        assert layer.lookup(Qt.Key.Key_K.value, _NOMOD) == (
            "x-tool.action.spin",
            "view.action.zoom",
        )

    def test_lookup_follows_overrides(self) -> None:
        base = _base()
        base.register_tool_actions({"view.action.zoom": "K"})
        layer = KeyMap.layer_over(base)
        layer.register_tool_actions({"x-tool.action.spin": "K"})
        layer.set_override("x-tool.action.spin", "M")
        assert layer.lookup(Qt.Key.Key_K.value, _NOMOD) == ("view.action.zoom",)
        assert layer.lookup(Qt.Key.Key_M.value, _NOMOD) == ("x-tool.action.spin",)

    def test_lookup_matches_any_alternative(self) -> None:
        layer = KeyMap.layer_over(_base())
        layer.register_tool_actions({"x-tool.action.step_up": "Up|W"})
        assert layer.lookup(Qt.Key.Key_W.value, _NOMOD) == ("x-tool.action.step_up",)
        assert layer.lookup(Qt.Key.Key_Up.value, _NOMOD) == ("x-tool.action.step_up",)

    def test_lookup_unbound_key_is_empty(self) -> None:
        assert KeyMap.layer_over(_base()).lookup(Qt.Key.Key_F35.value, _NOMOD) == ()


class TestMouseChords:
    """Stage 2: mouse chords live in the same alternatives strings as keys."""

    def test_chord_builders(self) -> None:
        from protoboard.gui.keymap import mouse_button_chord, wheel_chord

        ctrl = Qt.KeyboardModifier.ControlModifier
        assert mouse_button_chord(Qt.MouseButton.LeftButton, ctrl) == "Ctrl+LMB"
        assert mouse_button_chord(Qt.MouseButton.RightButton, _NOMOD) == "RMB"
        assert mouse_button_chord(Qt.MouseButton.MiddleButton, _NOMOD) == "MMB"
        assert wheel_chord(120, _NOMOD) == "WheelUp"
        assert wheel_chord(-40, _NOMOD) == "WheelDown"
        assert wheel_chord(0, _NOMOD) is None

    def _base_with_view(self) -> KeyMap:
        from protoboard.gui.keymap import VIEW_ACTIONS

        base = _base()
        base.declare(VIEW_ACTIONS)
        return base

    def test_layer_shadows_view_gesture(self) -> None:
        from protoboard.gui.keymap import VIEW_ZOOM_IN

        layer = KeyMap.layer_over(self._base_with_view())
        layer.register_tool_actions({"x-tool.action.wheel_rotate": "WheelUp|WheelDown"})
        assert layer.mouse_lookup("WheelUp") == (
            "x-tool.action.wheel_rotate",
            VIEW_ZOOM_IN,
        )

    def test_final_action_gains_mouse_trigger(self) -> None:
        # The user-facing point of additive-final: RMB can fire cancel in a
        # tool without Esc ever losing the job.
        from protoboard.gui.keymap import VIEW_CANVAS_MENU

        layer = KeyMap.layer_over(self._base_with_view())
        layer.register_tool_actions({"global.action.cancel": "RMB"})
        assert layer.resolve("global.action.cancel") == "Esc|RMB"
        assert layer.mouse_matches("RMB", "global.action.cancel")
        assert layer.matches(Qt.Key.Key_Escape.value, _NOMOD, "global.action.cancel")
        assert layer.mouse_lookup("RMB") == ("global.action.cancel", VIEW_CANVAS_MENU)

    def test_key_matcher_skips_mouse_alternatives(self) -> None:
        layer = KeyMap.layer_over(self._base_with_view())
        layer.register_tool_actions({"x-tool.action.zap": "Z|WheelUp"})
        assert layer.matches(Qt.Key.Key_Z.value, _NOMOD, "x-tool.action.zap")
        assert layer.mouse_matches("WheelUp", "x-tool.action.zap")


class _FakeTool(_DefaultTool):
    def key_actions(self) -> dict[str, str | None]:
        return {
            "fake-tool.action.zap": "Z",
            "fake-tool.action.bad": "Esc",  # collides with final cancel
            "fake-tool.action.edit": None,  # inherits global.action.edit
        }


class TestMakeKeyMapFactory:
    def test_registers_declared_actions(self) -> None:
        layer = _FakeTool().make_key_map(_base())
        assert isinstance(layer, KeyMap)
        assert layer.resolve("fake-tool.action.zap") == "Z"
        assert layer.resolve("fake-tool.action.edit") == "E"

    def test_final_collision_skipped_not_fatal(self) -> None:
        layer = _FakeTool().make_key_map(_base())  # must not raise
        assert isinstance(layer, KeyMap)
        # The offending entry is dropped; Esc keeps meaning cancel.
        assert layer.resolve("fake-tool.action.bad") == ""
        assert layer.matches(Qt.Key.Key_Escape.value, _NOMOD, "global.action.cancel")

    def test_layer_shares_parent_store(self) -> None:
        base = _base()
        base.set_override("fake-tool.action.zap", "K")
        layer = _FakeTool().make_key_map(base)
        assert isinstance(layer, KeyMap)
        assert layer.resolve("fake-tool.action.zap") == "K"
