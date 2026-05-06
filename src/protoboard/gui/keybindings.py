# SPDX-License-Identifier: MIT
"""Configurable keyboard shortcuts registry.

Defaults are defined here. User overrides are persisted in
~/.config/protoboard-designer/keybindings.yaml (only non-default entries).

Usage::

    kb = KeyBindings.load()
    kb.get(ACTION_TOOL_SELECT)   # → "S"  (or user override)
    kb.set(ACTION_TOOL_SELECT, "Q")  # mutates + persists

GUI integration (future Preferences dialog) only needs to call
``kb.set()`` and re-apply shortcuts to QActions.
"""

from __future__ import annotations

from dataclasses import dataclass, field

# ---------------------------------------------------------------------------
# Action IDs
# ---------------------------------------------------------------------------

ACTION_SAVE = "save"
ACTION_SAVE_AS = "save_as"
ACTION_UNDO = "undo"
ACTION_REDO = "redo"
ACTION_QUIT = "quit"

ACTION_SELECT_ALL = "select_all"
ACTION_DESELECT_ALL = "deselect_all"
ACTION_SELECT_ALL_COMPONENTS = "select_all_components"
ACTION_DESELECT_ALL_COMPONENTS = "deselect_all_components"
ACTION_SELECT_ALL_WIRES = "select_all_wires"
ACTION_DESELECT_ALL_WIRES = "deselect_all_wires"

ACTION_DELETE = "delete"
ACTION_ROTATE_CW = "rotate_cw"
ACTION_ROTATE_CCW = "rotate_ccw"
ACTION_FLIP = "flip"

ACTION_GROUP = "group"
ACTION_UNGROUP = "ungroup"
ACTION_EXIT_GROUP = "exit_group"

ACTION_REDRAG_PINS = "redrag_pins"

ACTION_WIRE_SET_INSULATED = "wire_set_insulated"
ACTION_WIRE_SET_BARE = "wire_set_bare"
ACTION_WIRE_MOVE_TO_TOP = "wire_move_to_top"
ACTION_WIRE_MOVE_TO_BOTTOM = "wire_move_to_bottom"

ACTION_DISCONNECT_PIN = "disconnect_pin"

ACTION_TOOL_SELECT = "tool_select"
ACTION_TOOL_ROUTE = "tool_route"
ACTION_TOOL_CUT = "tool_cut"

ACTION_MIRROR_H = "mirror_h"
ACTION_MIRROR_V = "mirror_v"
ACTION_CAMERA_TOGGLE = "camera_toggle"

ACTION_ZOOM_IN = "zoom_in"
ACTION_ZOOM_OUT = "zoom_out"
ACTION_FIT_BOARD = "fit_board"

ACTION_WIRE_TOGGLE_INSULATED = "wire_toggle_insulated"
ACTION_WIRE_SIDE_TOP = "wire_side_top"
ACTION_WIRE_SIDE_BOTTOM = "wire_side_bottom"

ACTION_RUN_DRC = "run_drc"
ACTION_AUTOROUTE = "autoroute"

ACTION_LAYER_1 = "layer_1"
ACTION_LAYER_2 = "layer_2"
ACTION_LAYER_3 = "layer_3"
ACTION_LAYER_4 = "layer_4"
ACTION_LAYER_5 = "layer_5"
ACTION_LAYER_6 = "layer_6"
ACTION_LAYER_7 = "layer_7"

# ---------------------------------------------------------------------------
# Defaults
# ---------------------------------------------------------------------------

DEFAULTS: dict[str, str] = {
    ACTION_SAVE: "Ctrl+S",
    ACTION_SAVE_AS: "Ctrl+Shift+S",
    ACTION_UNDO: "Ctrl+Z",
    ACTION_REDO: "Ctrl+Y",
    ACTION_QUIT: "Ctrl+Q",
    ACTION_SELECT_ALL: "Ctrl+A",
    ACTION_DESELECT_ALL: "Ctrl+Shift+A",
    ACTION_SELECT_ALL_COMPONENTS: "",
    ACTION_DESELECT_ALL_COMPONENTS: "",
    ACTION_SELECT_ALL_WIRES: "",
    ACTION_DESELECT_ALL_WIRES: "",
    ACTION_DELETE: "Del",
    ACTION_ROTATE_CW: "R",
    ACTION_ROTATE_CCW: "Shift+R",
    ACTION_FLIP: "Alt+R",
    ACTION_GROUP: "Ctrl+G",
    ACTION_UNGROUP: "",
    ACTION_EXIT_GROUP: "",
    ACTION_REDRAG_PINS: "Ctrl+R",
    ACTION_WIRE_SET_INSULATED: "",
    ACTION_WIRE_SET_BARE: "",
    ACTION_WIRE_MOVE_TO_TOP: "",
    ACTION_WIRE_MOVE_TO_BOTTOM: "",
    ACTION_DISCONNECT_PIN: "",
    ACTION_TOOL_SELECT: "S",
    ACTION_TOOL_ROUTE: "W",
    ACTION_TOOL_CUT: "C",
    ACTION_MIRROR_H: "H",
    ACTION_MIRROR_V: "V",
    ACTION_CAMERA_TOGGLE: "X",
    ACTION_ZOOM_IN: "Ctrl++",
    ACTION_ZOOM_OUT: "Ctrl+-",
    ACTION_FIT_BOARD: "Ctrl+0",
    ACTION_WIRE_TOGGLE_INSULATED: "I",
    ACTION_WIRE_SIDE_TOP: "T",
    ACTION_WIRE_SIDE_BOTTOM: "B",
    ACTION_RUN_DRC: "F5",
    ACTION_AUTOROUTE: "F6",
    ACTION_LAYER_1: "1",
    ACTION_LAYER_2: "2",
    ACTION_LAYER_3: "3",
    ACTION_LAYER_4: "4",
    ACTION_LAYER_5: "5",
    ACTION_LAYER_6: "6",
    ACTION_LAYER_7: "7",
}

# Human-readable names (for future Preferences dialog).
DISPLAY_NAMES: dict[str, str] = {
    ACTION_SAVE: "Save",
    ACTION_SAVE_AS: "Save As",
    ACTION_UNDO: "Undo",
    ACTION_REDO: "Redo",
    ACTION_QUIT: "Quit",
    ACTION_SELECT_ALL: "Select All",
    ACTION_DESELECT_ALL: "Deselect All",
    ACTION_SELECT_ALL_COMPONENTS: "Select All Components",
    ACTION_DESELECT_ALL_COMPONENTS: "Deselect All Components",
    ACTION_SELECT_ALL_WIRES: "Select All Wires",
    ACTION_DESELECT_ALL_WIRES: "Deselect All Wires",
    ACTION_DELETE: "Delete",
    ACTION_ROTATE_CW: "Rotate CW 90°",
    ACTION_ROTATE_CCW: "Rotate CCW 90°",
    ACTION_FLIP: "Flip (2-pin)",
    ACTION_GROUP: "Group Selection",
    ACTION_UNGROUP: "Ungroup",
    ACTION_EXIT_GROUP: "Exit Group",
    ACTION_REDRAG_PINS: "Re-drag Pins",
    ACTION_WIRE_SET_INSULATED: "Wire: Set Insulated",
    ACTION_WIRE_SET_BARE: "Wire: Set Bare",
    ACTION_WIRE_MOVE_TO_TOP: "Wire: Move to Top Layer",
    ACTION_WIRE_MOVE_TO_BOTTOM: "Wire: Move to Bottom Layer",
    ACTION_DISCONNECT_PIN: "Disconnect Pin from Net",
    ACTION_TOOL_SELECT: "Select Tool",
    ACTION_TOOL_ROUTE: "Route Wire Tool",
    ACTION_TOOL_CUT: "Cut Strip Tool",
    ACTION_MIRROR_H: "Mirror Horizontal",
    ACTION_MIRROR_V: "Mirror Vertical",
    ACTION_CAMERA_TOGGLE: "Toggle Camera Side",
    ACTION_ZOOM_IN: "Zoom In",
    ACTION_ZOOM_OUT: "Zoom Out",
    ACTION_FIT_BOARD: "Fit Board",
    ACTION_WIRE_TOGGLE_INSULATED: "Toggle Wire Insulation",
    ACTION_WIRE_SIDE_TOP: "Wire Side: Top",
    ACTION_WIRE_SIDE_BOTTOM: "Wire Side: Bottom",
    ACTION_RUN_DRC: "Run DRC",
    ACTION_AUTOROUTE: "Autoroute All Nets",
    ACTION_LAYER_1: "Layer 1 (Components Top)",
    ACTION_LAYER_2: "Layer 2 (Components Bottom)",
    ACTION_LAYER_3: "Layer 3 (Wires Top)",
    ACTION_LAYER_4: "Layer 4 (Wires Bottom)",
    ACTION_LAYER_5: "Layer 5 (Labels Top)",
    ACTION_LAYER_6: "Layer 6 (Labels Bottom)",
    ACTION_LAYER_7: "Layer 7 (Board)",
}

# ---------------------------------------------------------------------------
# Config path
# ---------------------------------------------------------------------------

from protoboard.paths import CONFIG_DIR as _CONFIG_DIR, KEYBINDINGS_FILE as _KEYBINDINGS_PATH

# ---------------------------------------------------------------------------
# KeyBindings class
# ---------------------------------------------------------------------------


@dataclass
class KeyBindings:
    """Merged shortcut table: defaults + user overrides.

    Only non-default entries are written to disk so the file stays minimal
    and future default changes automatically propagate to users who haven't
    customised those actions.
    """

    _bindings: dict[str, str] = field(default_factory=lambda: dict(DEFAULTS))

    # ------------------------------------------------------------------
    # Load / save
    # ------------------------------------------------------------------

    @classmethod
    def load(cls) -> "KeyBindings":
        """Load from disk, falling back to defaults on any error."""
        bindings = dict(DEFAULTS)
        if _KEYBINDINGS_PATH.exists():
            try:
                from ruamel.yaml import YAML  # type: ignore[import-untyped,unused-ignore]

                yaml = YAML()
                raw = yaml.load(_KEYBINDINGS_PATH)
                if isinstance(raw, dict):
                    for key, val in raw.items():
                        if isinstance(key, str) and isinstance(val, str):
                            bindings[key] = val
            except Exception:
                pass  # corrupt file → use defaults silently
        return cls(_bindings=bindings)

    def save(self) -> None:
        """Persist only non-default entries to disk."""
        overrides = {
            k: v for k, v in self._bindings.items() if DEFAULTS.get(k) != v
        }
        _CONFIG_DIR.mkdir(parents=True, exist_ok=True)
        from ruamel.yaml import YAML  # type: ignore[import-untyped,unused-ignore]

        yaml = YAML()
        yaml.default_flow_style = False
        with _KEYBINDINGS_PATH.open("w", encoding="utf-8") as fh:
            fh.write("# Protoboard Designer — keyboard shortcut overrides\n")
            fh.write("# Format: action_id: \"Shortcut\"  (empty string = no shortcut)\n")
            fh.write("# Available action IDs and defaults: see keybindings.py\n")
            if overrides:
                yaml.dump(overrides, fh)

    def generate_full_config(self) -> None:
        """Write a complete keybindings.yaml with all actions and their current values."""
        _CONFIG_DIR.mkdir(parents=True, exist_ok=True)
        from ruamel.yaml import YAML  # type: ignore[import-untyped,unused-ignore]
        from ruamel.yaml.comments import CommentedMap  # type: ignore[import-untyped,unused-ignore]

        cm: CommentedMap = CommentedMap()
        for action_id, default_shortcut in DEFAULTS.items():
            cm[action_id] = self._bindings.get(action_id, default_shortcut)
            display = DISPLAY_NAMES.get(action_id, action_id)
            cm.yaml_set_comment_before_after_key(
                action_id, before=f" {display} (default: \"{default_shortcut}\")"
            )

        yaml = YAML()
        yaml.default_flow_style = False
        with _KEYBINDINGS_PATH.open("w", encoding="utf-8") as fh:
            fh.write("# Protoboard Designer — keyboard shortcut overrides\n")
            fh.write("# Edit this file and restart the application to apply changes.\n")
            fh.write("# Set a value to \"\" to disable the shortcut.\n\n")
            yaml.dump(cm, fh)

    # ------------------------------------------------------------------
    # Access
    # ------------------------------------------------------------------

    def get(self, action_id: str) -> str:
        """Return the shortcut string for *action_id*, or '' if none."""
        return self._bindings.get(action_id, DEFAULTS.get(action_id, ""))

    def set(self, action_id: str, shortcut: str) -> None:
        """Set shortcut for *action_id* and persist to disk immediately."""
        self._bindings[action_id] = shortcut
        self.save()

    def reset_to_defaults(self) -> None:
        """Restore all bindings to defaults and remove the config file."""
        self._bindings = dict(DEFAULTS)
        if _KEYBINDINGS_PATH.exists():
            _KEYBINDINGS_PATH.unlink()
