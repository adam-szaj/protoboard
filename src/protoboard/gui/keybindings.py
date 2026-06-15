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
ACTION_WIRE_FLIP = "wire_flip"
ACTION_WIRE_CONVERT_TO_WIRE_PIN = "wire_convert_to_wire_pin"
ACTION_WIRE_CONVERT_FROM_WIRE_PIN = "wire_convert_from_wire_pin"

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
# Legacy alias retained for any saved-preferences files that still
# reference the old id.  Display name + slot mapping below resolve
# through ACTION_MERGE_WIRES.
ACTION_MERGE_WIRE_FRAGMENTS = ACTION_MERGE_WIRES

ACTION_HIGHLIGHT_TOGGLE = "highlight_toggle"   # toggle highlight on/off
ACTION_HIGHLIGHT_CYCLE  = "highlight_cycle"    # cycle through relation classes
ACTION_CLEAR_HIGHLIGHT  = "clear_highlight"    # drop persistent stack entries

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
    ACTION_UNGROUP: "Ctrl+Shift+G",
    ACTION_EXIT_GROUP: "",
    ACTION_REDRAG_PINS: "Ctrl+R",
    ACTION_WIRE_SET_INSULATED: "Ctrl+Shift+I",
    ACTION_WIRE_SET_BARE: "Ctrl+Shift+B",
    ACTION_WIRE_MOVE_TO_TOP: "Ctrl+T",
    ACTION_WIRE_MOVE_TO_BOTTOM: "Ctrl+B",
    ACTION_WIRE_FLIP: "Ctrl+F",
    ACTION_WIRE_CONVERT_TO_WIRE_PIN: "",
    ACTION_WIRE_CONVERT_FROM_WIRE_PIN: "",
    ACTION_DISCONNECT_PIN: "",
    ACTION_TOOL_SELECT: "S",
    ACTION_TOOL_ROUTE: "W",
    ACTION_TOOL_CUT: "C",
    ACTION_TOOL_SIMPLIFY: "Shift+S",
    ACTION_MIRROR_H: "H",
    ACTION_MIRROR_V: "V",
    ACTION_CAMERA_TOGGLE: "X",
    ACTION_FULLSCREEN: "F11",
    ACTION_ZOOM_IN: "Ctrl++",
    ACTION_ZOOM_OUT: "Ctrl+-",
    ACTION_FIT_BOARD: "Ctrl+0",
    ACTION_FIT_AREA: "Ctrl+Shift+0",
    ACTION_FIT_SELECTION: "Ctrl+Shift+F",
    ACTION_PREFERENCES: "Ctrl+,",
    ACTION_WIRE_TOGGLE_INSULATED: "I",
    ACTION_WIRE_SIDE_TOP: "T",
    ACTION_WIRE_SIDE_BOTTOM: "B",
    ACTION_RUN_DRC: "F5",
    ACTION_AUTOROUTE: "F6",
    ACTION_AUTOROUTE_DIALOG: "Alt+F6",
    ACTION_AUTOROUTE_NO_DIALOG: "Shift+F6",
    ACTION_SIMPLIFY_WIRE: "",
    ACTION_UNWEAVE_WIRES: "",
    ACTION_MERGE_WIRES: "",
    ACTION_HIGHLIGHT_TOGGLE: "",
    ACTION_HIGHLIGHT_CYCLE: "`",
    ACTION_CLEAR_HIGHLIGHT: "Ctrl+Shift+H",
    ACTION_FILTER_TOGGLE_COMPONENTS: "Alt+1",
    ACTION_FILTER_TOGGLE_WIRES: "Alt+2",
    ACTION_FILTER_TOGGLE_GROUPS: "Alt+3",
    ACTION_RELATION_NONE: "",
    ACTION_RELATION_LOGICAL_NET: "",
    ACTION_RELATION_PHYSICAL_NET: "",
    ACTION_RELATION_UNCONNECTED: "",
    ACTION_RELATION_NEIGHBORS: "",
    ACTION_RELATION_CYCLE_BACK: "Shift+`",
    ACTION_LAYER_1: "1",
    ACTION_LAYER_2: "2",
    ACTION_LAYER_3: "3",
    ACTION_LAYER_4: "4",
    ACTION_LAYER_5: "5",
    ACTION_LAYER_6: "6",
    ACTION_LAYER_7: "7",
    ACTION_LAYER_8: "8",
    ACTION_LAYER_9: "9",
    ACTION_LAYER_10: "0",
    ACTION_LAYERS_RESET: "",
    ACTION_IMPORT_KICAD_NET: "",
    ACTION_EXPORT_KICAD_NET: "",
    ACTION_EXPORT_SVG: "",
    ACTION_EXPORT_PDF: "",
    ACTION_EXPORT_PDF_TEMPLATE: "",
    ACTION_EXPORT_PNG: "",
    ACTION_EXPORT_BOM: "",
    ACTION_CLOSE_PROJECT: "",
    ACTION_BOARD_RESIZE: "",
    ACTION_BOARD_SPREAD: "",
    ACTION_GEN_SHORTCUT_CONFIG: "",
    ACTION_FOCUS_LIBRARY: "A",
    ACTION_LOCK_TOGGLE: "",
    ACTION_NET_NEW: "",
    ACTION_NET_RENAME: "",
    ACTION_NET_SET_COLOR: "",
    ACTION_NET_CLEAR_COLOR: "",
    ACTION_NET_TOGGLE_GLOBAL: "",
    ACTION_NET_DELETE: "",
    ACTION_PIN_ASSIGN_NET: "",
    ACTION_GROUP_RENAME: "",
    ACTION_GROUP_EXPORT_AUTO: "",
    ACTION_GROUP_EXPORT_ALL: "",
    ACTION_GROUP_EXPORT_CLEAR: "",
    ACTION_GROUP_AUTOROUTE: "",
    ACTION_CUSTOMIZE_TOOLBARS: "",
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
    ACTION_WIRE_FLIP: "Wire: Flip to Opposite Layer",
    ACTION_WIRE_CONVERT_TO_WIRE_PIN: "Wire: Convert Endpoint to Wire-pin",
    ACTION_WIRE_CONVERT_FROM_WIRE_PIN: "Wire: Convert Wire-pin Back to Endpoint",
    ACTION_DISCONNECT_PIN: "Disconnect Pin from Net",
    ACTION_TOOL_SELECT: "Select Tool",
    ACTION_TOOL_ROUTE: "Route Wire Tool",
    ACTION_TOOL_CUT: "Cut Strip Tool",
    ACTION_TOOL_SIMPLIFY: "Simplify Wire Tool",
    ACTION_MIRROR_H: "Mirror Horizontal",
    ACTION_MIRROR_V: "Mirror Vertical",
    ACTION_CAMERA_TOGGLE: "Toggle Camera Side",
    ACTION_FULLSCREEN: "Toggle Fullscreen",
    ACTION_ZOOM_IN: "Zoom In",
    ACTION_ZOOM_OUT: "Zoom Out",
    ACTION_FIT_BOARD: "Fit Board",
    ACTION_FIT_AREA: "Fit Area",
    ACTION_FIT_SELECTION: "Fit Selection",
    ACTION_PREFERENCES: "Preferences",
    ACTION_WIRE_TOGGLE_INSULATED: "Toggle Wire Insulation",
    ACTION_WIRE_SIDE_TOP: "Wire Side: Top",
    ACTION_WIRE_SIDE_BOTTOM: "Wire Side: Bottom",
    ACTION_RUN_DRC: "Run DRC",
    ACTION_AUTOROUTE: "Autoroute All Nets",
    ACTION_AUTOROUTE_DIALOG: "Autoroute (force dialog)",
    ACTION_AUTOROUTE_NO_DIALOG: "Autoroute (skip dialog)",
    ACTION_SIMPLIFY_WIRE: "Simplify Selected Wires",
    ACTION_UNWEAVE_WIRES: "Unweave Selected Wires",
    ACTION_MERGE_WIRES: "Merge Wires",
    ACTION_HIGHLIGHT_TOGGLE: "Highlight Toggle",
    ACTION_HIGHLIGHT_CYCLE: "Cycle Relation Class",
    ACTION_CLEAR_HIGHLIGHT: "Clear Persistent Highlight",
    ACTION_FILTER_TOGGLE_COMPONENTS: "Filter: Toggle Components",
    ACTION_FILTER_TOGGLE_WIRES: "Filter: Toggle Wires",
    ACTION_FILTER_TOGGLE_GROUPS: "Filter: Toggle Groups",
    ACTION_RELATION_NONE: "Relation: None",
    ACTION_RELATION_LOGICAL_NET: "Relation: Same Logical Net",
    ACTION_RELATION_PHYSICAL_NET: "Relation: Same Physical Net",
    ACTION_RELATION_UNCONNECTED: "Relation: Unconnected Pins",
    ACTION_RELATION_NEIGHBORS: "Relation: Neighbors",
    ACTION_RELATION_CYCLE_BACK: "Cycle Relation Class (Back)",
    ACTION_LAYER_1: "Layer 1 (Board)",
    ACTION_LAYER_2: "Layer 2 (Components Top)",
    ACTION_LAYER_3: "Layer 3 (Components Bottom)",
    ACTION_LAYER_4: "Layer 4 (Wires Top)",
    ACTION_LAYER_5: "Layer 5 (Wires Bottom)",
    ACTION_LAYERS_RESET: "Reset All Layers",
    ACTION_IMPORT_KICAD_NET: "Import KiCAD .net…",
    ACTION_EXPORT_KICAD_NET: "Export KiCAD .net…",
    ACTION_EXPORT_SVG: "Export SVG…",
    ACTION_EXPORT_PDF: "Export PDF…",
    ACTION_EXPORT_PDF_TEMPLATE: "Export Template PDF (A4, 4 panels)…",
    ACTION_EXPORT_PNG: "Export PNG…",
    ACTION_EXPORT_BOM: "Export BOM CSV…",
    ACTION_CLOSE_PROJECT: "Close Project",
    ACTION_BOARD_RESIZE: "Resize Board…",
    ACTION_BOARD_SPREAD: "Spread Components",
    ACTION_GEN_SHORTCUT_CONFIG: "Generate Shortcut Config File",
    ACTION_FOCUS_LIBRARY: "Add Component (focus Library)",
    ACTION_LOCK_TOGGLE: "Lock / Unlock Selected",
    ACTION_NET_NEW: "Net: New…",
    ACTION_NET_RENAME: "Net: Rename…",
    ACTION_NET_SET_COLOR: "Net: Set Color…",
    ACTION_NET_CLEAR_COLOR: "Net: Clear Color",
    ACTION_NET_TOGGLE_GLOBAL: "Net: Toggle Global",
    ACTION_NET_DELETE: "Net: Delete",
    ACTION_PIN_ASSIGN_NET: "Pin: Assign to Net…",
    ACTION_GROUP_RENAME: "Group: Rename Active…",
    ACTION_GROUP_EXPORT_AUTO: "Group: Export Auto (external nets)",
    ACTION_GROUP_EXPORT_ALL: "Group: Export All Pins",
    ACTION_GROUP_EXPORT_CLEAR: "Group: Clear Exported Pins",
    ACTION_GROUP_AUTOROUTE: "Group: Autoroute Internal Connections",
    ACTION_LAYER_6: "Layer 6 (Labels)",
    ACTION_LAYER_7: "Layer 7 (Pin Names)",
    ACTION_LAYER_8: "Layer 8 (Connections)",
    ACTION_LAYER_9: "Layer 9 (Pin Pads)",
    ACTION_LAYER_10: "Layer 10 (Pin Modifiers)",
    ACTION_CUSTOMIZE_TOOLBARS: "Customize Toolbars…",
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
