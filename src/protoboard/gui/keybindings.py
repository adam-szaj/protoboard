# SPDX-License-Identifier: MIT
"""Configurable keyboard shortcuts registry.

Defaults are defined by the declarative action registry. User overrides are persisted in
~/.config/protoboard-designer/keybindings.yaml (only non-default entries).

Usage::

    kb = KeyBindings.load()
    kb.get(ACTION_TOOL_SELECT)   # → "S"  (or user override)
    kb.set(ACTION_TOOL_SELECT, "Q")  # mutates + persists

GUI integration (future Preferences dialog) only needs to call
``kb.set()`` and re-apply shortcuts to QActions.
"""

from __future__ import annotations

from collections.abc import Iterator, Mapping
from dataclasses import dataclass, field
from pathlib import Path
from typing import cast

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
ACTION_ROTATE_AROUND_POINT = "rotate_around_point"
ACTION_FLIP = "flip"

ACTION_GROUP = "group"
ACTION_UNGROUP = "ungroup"
ACTION_EXIT_GROUP = "exit_group"

ACTION_REDRAG_PINS = "redrag_pins"
ACTION_COMPONENT_MOVE_TO_SIDE = "component_move_to_side"

ACTION_WIRE_SET_INSULATED = "wire_set_insulated"
ACTION_WIRE_SET_BARE = "wire_set_bare"
ACTION_WIRE_MOVE_TO_TOP = "wire_move_to_top"
ACTION_WIRE_MOVE_TO_BOTTOM = "wire_move_to_bottom"
ACTION_WIRE_FLIP = "wire_flip"
ACTION_WIRE_CONVERT_TO_WIRE_PIN = "wire_convert_to_wire_pin"
ACTION_WIRE_CONVERT_FROM_WIRE_PIN = "wire_convert_from_wire_pin"
# Menu surface only — the live [E] keystroke is owned by the namespaced
# ``select-tool.action.edit`` in the KeyMap (see select_tool.py).  This action
# exists so wire-edit has a discoverable Edit ► Design ► Wire menu entry; its
# default shortcut is empty so no QShortcut competes with the tool binding.
ACTION_WIRE_EDIT = "wire_edit"

ACTION_DISCONNECT_PIN = "disconnect_pin"

ACTION_TOOL_SELECT = "tool_select"
ACTION_TOOL_ROUTE = "tool_route"
ACTION_TOOL_CUT = "tool_cut"
ACTION_TOOL_SIMPLIFY = "tool_simplify"

ACTION_MIRROR_H = "mirror_h"
ACTION_MIRROR_V = "mirror_v"
ACTION_CAMERA_TOGGLE = "camera_toggle"
ACTION_FULLSCREEN = "fullscreen"

ACTION_ZOOM_IN = "zoom_in"
ACTION_ZOOM_OUT = "zoom_out"
ACTION_FIT_BOARD = "fit_board"
ACTION_FIT_AREA = "fit_area"
ACTION_FIT_SELECTION = "fit_selection"

ACTION_PREFERENCES = "preferences"

ACTION_WIRE_TOGGLE_INSULATED = "wire_toggle_insulated"
ACTION_WIRE_SIDE_TOP = "wire_side_top"
ACTION_WIRE_SIDE_BOTTOM = "wire_side_bottom"

ACTION_RUN_DRC = "run_drc"
ACTION_AUTOROUTE = "autoroute"
# Modifier variants of Autoroute: force the pre-flight dialog on / off
# regardless of the persisted ``skip_autoroute_dialog`` setting.  Re-
# enabling the dialog used to require digging into Preferences; these
# give a one-key escape hatch in both directions.
ACTION_AUTOROUTE_DIALOG = "autoroute_dialog"
ACTION_AUTOROUTE_NO_DIALOG = "autoroute_no_dialog"
ACTION_SIMPLIFY_WIRE = "simplify_wire"
ACTION_UNWEAVE_WIRES = "unweave_wires"
ACTION_MERGE_WIRES = "merge_wires"

ACTION_HIGHLIGHT_TOGGLE = "highlight_toggle"  # toggle highlight on/off
ACTION_HIGHLIGHT_CYCLE = "highlight_cycle"  # cycle through relation classes
ACTION_CLEAR_HIGHLIGHT = "clear_highlight"  # drop persistent stack entries

# Selection filter toggles — which object types may be selected.
ACTION_FILTER_TOGGLE_COMPONENTS = "filter_toggle_components"
ACTION_FILTER_TOGGLE_WIRES = "filter_toggle_wires"
ACTION_FILTER_TOGGLE_GROUPS = "filter_toggle_groups"

# Relation class direct-set — empty by default so users can opt in.
ACTION_RELATION_NONE = "relation_none"
ACTION_RELATION_LOGICAL_NET = "relation_logical_net"
ACTION_RELATION_PHYSICAL_NET = "relation_physical_net"
ACTION_RELATION_UNCONNECTED = "relation_unconnected"
ACTION_RELATION_NEIGHBORS = "relation_neighbors"
ACTION_RELATION_CYCLE_BACK = "relation_cycle_back"

ACTION_LAYER_1 = "layer_1"
ACTION_LAYER_2 = "layer_2"
ACTION_LAYER_3 = "layer_3"
ACTION_LAYER_4 = "layer_4"
ACTION_LAYER_5 = "layer_5"
ACTION_LAYER_6 = "layer_6"
ACTION_LAYER_7 = "layer_7"
ACTION_LAYER_8 = "layer_8"
ACTION_LAYER_9 = "layer_9"
ACTION_LAYER_10 = "layer_10"
ACTION_LAYERS_RESET = "layers_reset"

# File menu — previously anonymous addAction("…", slot) entries promoted
# to first-class action-ids so they can be rebound via Preferences.
ACTION_IMPORT_KICAD_NET = "import_kicad_net"
ACTION_EXPORT_KICAD_NET = "export_kicad_net"
ACTION_EXPORT_SVG = "export_svg"
ACTION_EXPORT_PDF = "export_pdf"
ACTION_EXPORT_PDF_TEMPLATE = "export_pdf_template"
ACTION_EXPORT_PNG = "export_png"
ACTION_EXPORT_BOM = "export_bom"
ACTION_CLOSE_PROJECT = "close_project"

# Board edit — previously anonymous menu entries.
ACTION_BOARD_RESIZE = "board_resize"
ACTION_BOARD_SPREAD = "board_spread"

# Settings / devtools.
ACTION_GEN_SHORTCUT_CONFIG = "gen_shortcut_config"

# Component creation — focus the Library panel so the user can pick
# a part to place.  Default shortcut "A" matches the KiCAD convention.
ACTION_FOCUS_LIBRARY = "focus_library"

# Lock / Unlock selected components.  Toggle: if every component in
# the selection is already locked, unlock them all; otherwise lock the
# whole selection.  No default shortcut.
ACTION_LOCK_TOGGLE = "lock_toggle"

# Net operations.  These delegate to the Nets editor panel — top-menu
# entries raise the panel and invoke the panel's slot so the user can
# rename / colour / toggle global / delete the currently selected net,
# or create a new one.  No default shortcuts.
ACTION_NET_NEW = "net_new"
ACTION_NET_RENAME = "net_rename"
ACTION_NET_SET_COLOR = "net_set_color"
ACTION_NET_CLEAR_COLOR = "net_clear_color"
ACTION_NET_TOGGLE_GLOBAL = "net_toggle_global"
ACTION_NET_DELETE = "net_delete"
ACTION_PIN_ASSIGN_NET = "pin_assign_net"

# Group operations that act on the active (entered) group.  Rename and
# exported-pin management used to live only in the right-click menu;
# Autoroute Group is new (recursive, internal-only routing).
ACTION_GROUP_RENAME = "group_rename"
ACTION_GROUP_EXPORT_AUTO = "group_export_auto"
ACTION_GROUP_EXPORT_ALL = "group_export_all"
ACTION_GROUP_EXPORT_CLEAR = "group_export_clear"
ACTION_GROUP_AUTOROUTE = "group_autoroute"

# View ► Customize Toolbars — opens a dialog that edits toolbars.yaml.
ACTION_CUSTOMIZE_TOOLBARS = "customize_toolbars"


class _ActionMetadata(Mapping[str, str]):
    """Read-only metadata view backed by the declarative action registry."""

    def _values(self) -> dict[str, str]:
        from protoboard.gui.actions import ALL_ACTIONS, strip_mnemonic

        return {
            spec.id: self._value(spec, strip_mnemonic(spec.label))
            for spec in ALL_ACTIONS
        }

    def _value(self, spec: object, display: str) -> str:
        raise NotImplementedError

    def __getitem__(self, key: str) -> str:
        return self._values()[key]

    def __iter__(self) -> Iterator[str]:
        return iter(self._values())

    def __len__(self) -> int:
        return len(self._values())


class _DefaultShortcuts(_ActionMetadata):
    def _value(self, spec: object, display: str) -> str:
        return cast(str, getattr(spec, "default_shortcut"))


class _DisplayNames(_ActionMetadata):
    def _value(self, spec: object, display: str) -> str:
        return display


DEFAULTS: Mapping[str, str] = _DefaultShortcuts()
DISPLAY_NAMES: Mapping[str, str] = _DisplayNames()

# ---------------------------------------------------------------------------
# Config path
# ---------------------------------------------------------------------------

from protoboard.paths import (  # noqa: E402
    CONFIG_DIR as _CONFIG_DIR,
)
from protoboard.paths import (  # noqa: E402
    KEYBINDINGS_FILE as _KEYBINDINGS_PATH,
)


def config_path() -> Path:
    """Return the active shortcut configuration path."""
    return _KEYBINDINGS_PATH


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
    def load(cls) -> KeyBindings:
        """Load from disk, falling back to defaults on any error."""
        bindings = dict(DEFAULTS)
        if _KEYBINDINGS_PATH.exists():
            try:
                from ruamel.yaml import (
                    YAML,  # type: ignore[import-untyped,unused-ignore]
                )

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
        overrides = {k: v for k, v in self._bindings.items() if DEFAULTS.get(k) != v}
        _CONFIG_DIR.mkdir(parents=True, exist_ok=True)
        from ruamel.yaml import YAML  # type: ignore[import-untyped,unused-ignore]

        yaml = YAML()
        yaml.default_flow_style = False
        with _KEYBINDINGS_PATH.open("w", encoding="utf-8") as fh:
            fh.write("# Protoboard Designer — keyboard shortcut overrides\n")
            fh.write('# Format: action_id: "Shortcut"  (empty string = no shortcut)\n')
            fh.write("# Available action IDs and defaults: see actions.py\n")
            if overrides:
                yaml.dump(overrides, fh)

    def generate_full_config(self) -> None:
        """Write a complete keybindings.yaml with all actions and their current values."""  # noqa: E501
        _CONFIG_DIR.mkdir(parents=True, exist_ok=True)
        from ruamel.yaml import YAML  # type: ignore[import-untyped,unused-ignore]
        from ruamel.yaml.comments import (
            CommentedMap,  # type: ignore[import-untyped,unused-ignore]
        )

        cm: CommentedMap = CommentedMap()
        for action_id, default_shortcut in DEFAULTS.items():
            cm[action_id] = self._bindings.get(action_id, default_shortcut)
            display = DISPLAY_NAMES.get(action_id, action_id)
            cm.yaml_set_comment_before_after_key(
                action_id, before=f' {display} (default: "{default_shortcut}")'
            )

        yaml = YAML()
        yaml.default_flow_style = False
        with _KEYBINDINGS_PATH.open("w", encoding="utf-8") as fh:
            fh.write("# Protoboard Designer — keyboard shortcut overrides\n")
            fh.write("# Edit this file and restart the application to apply changes.\n")
            fh.write('# Set a value to "" to disable the shortcut.\n\n')
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
