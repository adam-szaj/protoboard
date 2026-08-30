# SPDX-License-Identifier: MIT
"""Declarative action registry — single source of truth for every UI action.

Each :class:`ActionSpec` carries everything needed to wire a feature into
menu, toolbar, keybinding registry and the shortcut-override YAML.  The
companion :mod:`protoboard.gui.action_builder` walks this list once at
``MainWindow`` construction and produces QActions + menu items + toolbar
buttons + QActionGroups.

Why a separate module from :mod:`protoboard.gui.keybindings`?

* ``keybindings.py`` owns shortcut *persistence* (YAML round-trip).
  ``actions.py`` owns the UI *shape* (label, menu path, toolbar group).
  Separating the two keeps imports tiny and lets tests build a registry
  without spinning up a ``MainWindow``.
* Specs reference slots by **string name**, not bound method, so the list
  can live as a module-level constant without circular imports.

Dynamic items intentionally NOT in this list (built directly in
``main_window.py``):

* ``File ► Open Recent`` submenu — populated from `RecentProjects` state.
* ``View ► Camera ► Top / Bottom`` — exclusive radio group whose label
  changes at runtime when the user flips the board; the configurable
  ``camera_toggle`` action IS in the list.
Toolbar placement is **not** carried by the spec.  Layout lives in
``~/.config/protoboard/toolbars.yaml`` (shipped default at
``default_toolbars.yaml`` in the package root) — see
``src/protoboard/gui/toolbar_config.py`` for the loader.  This keeps
the spec list focused on identity and slot wiring; toolbar placement
is a pure UX concern owned by the user.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Literal

from protoboard.gui import keybindings as KB

# ---------------------------------------------------------------------------
# Typed primitives
# ---------------------------------------------------------------------------

#: A hierarchical menu path.  ``("Edit", "Design", "Wire")`` resolves to
#: ``Edit ► Design ► Wire``.  Submenus are created on demand by the
#: builder — repeated paths reuse the same QMenu.
MenuPath = tuple[str, ...]

#: Logical toolbar groups.  Adding a new toolbar is a single edit here
#: + matching builder default.  ``Literal`` gives mypy enforcement.
ToolbarGroup = Literal[
    "tools",
    "view",
    "zoom",
    "wire",
    "selection",
    "analysis",
    "component",
]


@dataclass(frozen=True, slots=True)
class ActionSpec:
    """Everything needed to wire one user-visible action.

    Attributes:
        id: Action identifier — must match one of the ``KB.ACTION_*``
            constants.  Used as the lookup key in the QAction registry,
            the YAML shortcut overrides, and the toolbar-customisation
            persistence.
        label: Human-visible menu label.  May contain a single ``&``
            mnemonic prefix.  The stripped form is exposed as
            ``DISPLAY_NAMES[id]`` for Preferences/UX consumers.
        slot_name: Method name on the resolved owner object.  Plain
            ``"_save"`` resolves on the default owner; ``"select_tool._foo"``
            resolves on the registered ``select_tool`` owner.  No bound
            methods stored — that would force per-instance specs.
        menu_path: Where the action lives in the main menu, or ``None``
            for intentional orphans (accessible only via shortcut or
            context menu).  Orphans MUST have a one-line ``# orphan: …``
            justification next to the spec entry — Phase 3 enforces this.
        checkable: True for toggle-style QActions (e.g. layer visibility).
        enabled: Initial enabled state.  Most actions stay enabled and
            rely on context updates from session signals.
        icon: Theme icon name (``QIcon.fromTheme(name)``) or ``None`` for
            text-only.  Phase 4 falls back to first-letter text for
            icon-less actions added to a user-customised toolbar.
        tooltip: Optional ``setToolTip`` string.  Falls back to label.
        action_group: Name of an exclusive QActionGroup.  All specs that
            share the same string become a radio set.  Reserved names:
            ``"tool"`` (Select / Route / Cut / Simplify),
            ``"wire_side"`` (Top / Bottom), ``"camera"`` (Top / Bottom).
        separator_before: Insert a ``addSeparator()`` in the host menu
            just before this action.  Use sparingly — only when grouping
            cannot be expressed by submenus.
        shortcut_args: Positional arguments captured into a
            ``functools.partial`` when binding the slot.  Replaces the
            ``lambda: self._foo("top")`` closures that haunted the old
            registry and made late-binding bugs trivial to hit.
    """

    id: str
    label: str
    slot_name: str
    menu_path: MenuPath | None
    checkable: bool = False
    enabled: bool = True
    icon: str | None = None
    tooltip: str | None = None
    action_group: str | None = None
    separator_before: bool = False
    shortcut_args: tuple[str | int | bool, ...] = ()


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def strip_mnemonic(label: str) -> str:
    """Drop Qt mnemonic markers from a label for display in Preferences.

    Qt uses ``&`` to mark the Alt-mnemonic letter.  ``&&`` is a literal
    ampersand.  We strip single ``&`` and collapse ``&&`` to ``&``.
    """
    out: list[str] = []
    i = 0
    while i < len(label):
        ch = label[i]
        if ch == "&":
            if i + 1 < len(label) and label[i + 1] == "&":
                out.append("&")
                i += 2
                continue
            i += 1
            continue
        out.append(ch)
        i += 1
    return "".join(out)


# ---------------------------------------------------------------------------
# Menu paths — centralised for readability
# ---------------------------------------------------------------------------

_FILE = ("File",)
_EDIT = ("Edit",)
_EDIT_DESIGN_COMPONENT = ("Edit", "Design", "Component")
_EDIT_DESIGN_WIRE = ("Edit", "Design", "Wire")
_EDIT_DESIGN_NET = ("Edit", "Design", "Net")
_EDIT_DESIGN_GROUP = ("Edit", "Design", "Group")
_EDIT_DESIGN_GROUP_EXPORTED = ("Edit", "Design", "Group", "Exported Pins")
_EDIT_BOARD = ("Edit", "Board")
_EDIT_GENERATE = ("Edit", "Generate")
_SELECT = ("Select",)
_VIEW = ("View",)
_VIEW_LAYERS = ("View", "Layers")
_ANALYSIS = ("Analysis",)


# ---------------------------------------------------------------------------
# Specs — authoritative list (matches KB.DEFAULTS keys minus wire-pin
# convert actions which Phase 2 will add).
# ---------------------------------------------------------------------------

ALL_ACTIONS: tuple[ActionSpec, ...] = (
    # ------------------------------------------------------------------
    # File menu
    # ------------------------------------------------------------------
    ActionSpec(id=KB.ACTION_SAVE, label="&Save", slot_name="_save", menu_path=_FILE),
    ActionSpec(
        id=KB.ACTION_SAVE_AS, label="Save &As…", slot_name="_save_as", menu_path=_FILE
    ),
    # Open Recent submenu is built dynamically — not represented as a spec.
    ActionSpec(
        id=KB.ACTION_IMPORT_KICAD_NET,
        label="Import KiCAD .net…",
        slot_name="_import_kicad_net",
        menu_path=_FILE,
        separator_before=True,
    ),
    ActionSpec(
        id=KB.ACTION_EXPORT_KICAD_NET,
        label="Export KiCAD .net…",
        slot_name="_export_kicad_net",
        menu_path=_FILE,
    ),
    ActionSpec(
        id=KB.ACTION_EXPORT_SVG,
        label="Export SVG…",
        slot_name="_export_svg",
        menu_path=_FILE,
        separator_before=True,
    ),
    ActionSpec(
        id=KB.ACTION_EXPORT_PDF,
        label="Export PDF…",
        slot_name="_export_pdf",
        menu_path=_FILE,
    ),
    ActionSpec(
        id=KB.ACTION_EXPORT_PDF_TEMPLATE,
        label="Export Template PDF (A4)…",
        slot_name="_export_pdf_template",
        menu_path=_FILE,
    ),
    ActionSpec(
        id=KB.ACTION_EXPORT_PNG,
        label="Export PNG…",
        slot_name="_export_png",
        menu_path=_FILE,
    ),
    ActionSpec(
        id=KB.ACTION_EXPORT_BOM,
        label="Export BOM CSV…",
        slot_name="_export_bom",
        menu_path=_FILE,
    ),
    ActionSpec(
        id=KB.ACTION_CLOSE_PROJECT,
        label="Close Project",
        slot_name="_close_project",
        menu_path=_FILE,
        separator_before=True,
    ),
    ActionSpec(
        id=KB.ACTION_QUIT,
        label="&Quit",
        slot_name="close",
        menu_path=_FILE,
        separator_before=True,
    ),
    # ------------------------------------------------------------------
    # Edit menu — top level
    # ------------------------------------------------------------------
    ActionSpec(
        id=KB.ACTION_UNDO,
        label="&Undo",
        slot_name="_undo",
        menu_path=_EDIT,
        enabled=False,
    ),
    ActionSpec(
        id=KB.ACTION_REDO,
        label="&Redo",
        slot_name="_redo",
        menu_path=_EDIT,
        enabled=False,
    ),
    # Edit ► Design ► Component
    ActionSpec(
        id=KB.ACTION_FOCUS_LIBRARY,
        label="&Add Component…",
        slot_name="_focus_library_for_add",
        menu_path=_EDIT_DESIGN_COMPONENT,
    ),
    ActionSpec(
        id=KB.ACTION_ROTATE_CW,
        label="Rotate &CW",
        slot_name="_rotate_cw",
        menu_path=_EDIT_DESIGN_COMPONENT,
        separator_before=True,
    ),
    ActionSpec(
        id=KB.ACTION_ROTATE_CCW,
        label="Rotate CC&W",
        slot_name="_rotate_ccw",
        menu_path=_EDIT_DESIGN_COMPONENT,
    ),
    ActionSpec(
        id=KB.ACTION_ROTATE_AROUND_POINT,
        label="Rotate Around &Point",
        slot_name="_rotate_around_point",
        menu_path=_EDIT_DESIGN_COMPONENT,
    ),
    ActionSpec(
        id=KB.ACTION_FLIP,
        label="&Flip",
        slot_name="_flip_selection",
        menu_path=_EDIT_DESIGN_COMPONENT,
    ),
    ActionSpec(
        id=KB.ACTION_COMPONENT_MOVE_TO_SIDE,
        label="Move to Other &Side",
        slot_name="_move_component_to_side",
        menu_path=_EDIT_DESIGN_COMPONENT,
    ),
    ActionSpec(
        id=KB.ACTION_REDRAG_PINS,
        label="Re-drag &Pins",
        slot_name="_redrag_pins",
        menu_path=_EDIT_DESIGN_COMPONENT,
        separator_before=True,
    ),
    ActionSpec(
        id=KB.ACTION_LOCK_TOGGLE,
        label="&Lock / Unlock",
        slot_name="_lock_toggle",
        menu_path=_EDIT_DESIGN_COMPONENT,
        separator_before=True,
    ),
    ActionSpec(
        id=KB.ACTION_DELETE,
        label="&Delete",
        slot_name="_delete_selection",
        menu_path=_EDIT_DESIGN_COMPONENT,
        separator_before=True,
    ),
    # Edit ► Design ► Wire
    ActionSpec(
        id=KB.ACTION_TOOL_ROUTE,
        label="Route &Wire",
        slot_name="_tool_route",
        menu_path=_EDIT_DESIGN_WIRE,
        checkable=True,
        action_group="tool",
    ),
    ActionSpec(
        id=KB.ACTION_WIRE_TOGGLE_INSULATED,
        label="&Insulated",
        slot_name="_toggle_wire_insulated",
        menu_path=_EDIT_DESIGN_WIRE,
        checkable=True,
        separator_before=True,
    ),
    ActionSpec(
        id=KB.ACTION_WIRE_SIDE_TOP,
        label="Side: &Top",
        slot_name="_set_wire_side",
        menu_path=_EDIT_DESIGN_WIRE,
        checkable=True,
        action_group="wire_side",
        shortcut_args=("top",),
    ),
    ActionSpec(
        id=KB.ACTION_WIRE_SIDE_BOTTOM,
        label="Side: &Bottom",
        slot_name="_set_wire_side",
        menu_path=_EDIT_DESIGN_WIRE,
        checkable=True,
        action_group="wire_side",
        shortcut_args=("bottom",),
    ),
    ActionSpec(
        id=KB.ACTION_WIRE_SET_INSULATED,
        label="Wire: Set &Insulated",
        slot_name="_set_selected_wire_insulated",
        menu_path=_EDIT_DESIGN_WIRE,
        separator_before=True,
        shortcut_args=(True,),
    ),
    ActionSpec(
        id=KB.ACTION_WIRE_SET_BARE,
        label="Wire: Set &Bare",
        slot_name="_set_selected_wire_insulated",
        menu_path=_EDIT_DESIGN_WIRE,
        shortcut_args=(False,),
    ),
    ActionSpec(
        id=KB.ACTION_WIRE_MOVE_TO_TOP,
        label="Wire: Move to &Top Layer",
        slot_name="_move_selected_wires_to_side",
        menu_path=_EDIT_DESIGN_WIRE,
        shortcut_args=("top",),
    ),
    ActionSpec(
        id=KB.ACTION_WIRE_MOVE_TO_BOTTOM,
        label="Wire: Move to &Bottom Layer",
        slot_name="_move_selected_wires_to_side",
        menu_path=_EDIT_DESIGN_WIRE,
        shortcut_args=("bottom",),
    ),
    ActionSpec(
        id=KB.ACTION_WIRE_FLIP,
        label="Wire: &Flip to Opposite Layer",
        slot_name="_wire_flip_action",
        menu_path=_EDIT_DESIGN_WIRE,
    ),
    # Menu surface for wire-edit.  No shortcut here (default empty in keybindings)
    # — the live [E] keystroke is owned by the namespaced select-tool.action.edit;
    # the slot just delegates to the active SelectTool's arm_wire_edit().
    ActionSpec(
        id=KB.ACTION_WIRE_EDIT,
        label="Wire: &Edit Path",
        slot_name="_wire_edit_action",
        menu_path=_EDIT_DESIGN_WIRE,
    ),
    ActionSpec(
        id=KB.ACTION_TOOL_SIMPLIFY,
        label="Simplify &Wire Tool",
        slot_name="_tool_simplify",
        menu_path=_EDIT_DESIGN_WIRE,
        checkable=True,
        action_group="tool",
        separator_before=True,
    ),
    ActionSpec(
        id=KB.ACTION_SIMPLIFY_WIRE,
        label="Simplify Selected Wires",
        slot_name="_simplify_selected_wires",
        menu_path=_EDIT_DESIGN_WIRE,
    ),
    ActionSpec(
        id=KB.ACTION_UNWEAVE_WIRES,
        label="Unweave Selected Wires",
        slot_name="_unweave_selected_wires",
        menu_path=_EDIT_DESIGN_WIRE,
    ),
    ActionSpec(
        id=KB.ACTION_MERGE_WIRES,
        label="Merge Wires",
        slot_name="_merge_wires_action",
        menu_path=_EDIT_DESIGN_WIRE,
    ),
    # Edit ► Design ► Net
    ActionSpec(
        id=KB.ACTION_NET_NEW,
        label="Net: New…",
        slot_name="_net_new",
        menu_path=_EDIT_DESIGN_NET,
    ),
    ActionSpec(
        id=KB.ACTION_NET_RENAME,
        label="Net: Rename…",
        slot_name="_net_rename",
        menu_path=_EDIT_DESIGN_NET,
        separator_before=True,
    ),
    ActionSpec(
        id=KB.ACTION_NET_SET_COLOR,
        label="Net: Set Color…",
        slot_name="_net_set_color",
        menu_path=_EDIT_DESIGN_NET,
    ),
    ActionSpec(
        id=KB.ACTION_NET_CLEAR_COLOR,
        label="Net: Clear Color",
        slot_name="_net_clear_color",
        menu_path=_EDIT_DESIGN_NET,
    ),
    ActionSpec(
        id=KB.ACTION_NET_TOGGLE_GLOBAL,
        label="Net: Toggle Global",
        slot_name="_net_toggle_global",
        menu_path=_EDIT_DESIGN_NET,
    ),
    ActionSpec(
        id=KB.ACTION_NET_DELETE,
        label="Net: Delete",
        slot_name="_net_delete",
        menu_path=_EDIT_DESIGN_NET,
    ),
    ActionSpec(
        id=KB.ACTION_PIN_ASSIGN_NET,
        label="Pin: Assign to Net…",
        slot_name="_pin_assign_net",
        menu_path=_EDIT_DESIGN_NET,
        separator_before=True,
    ),
    ActionSpec(
        id=KB.ACTION_DISCONNECT_PIN,
        label="&Disconnect Pin from Net…",
        slot_name="_disconnect_pin",
        menu_path=_EDIT_DESIGN_NET,
    ),
    # Edit ► Design ► Group
    ActionSpec(
        id=KB.ACTION_GROUP,
        label="&Group Selection",
        slot_name="_group_selection",
        menu_path=_EDIT_DESIGN_GROUP,
    ),
    ActionSpec(
        id=KB.ACTION_UNGROUP,
        label="&Ungroup",
        slot_name="_ungroup_selection",
        menu_path=_EDIT_DESIGN_GROUP,
    ),
    ActionSpec(
        id=KB.ACTION_EXIT_GROUP,
        label="&Exit Group",
        slot_name="_exit_group",
        menu_path=_EDIT_DESIGN_GROUP,
    ),
    ActionSpec(
        id=KB.ACTION_GROUP_RENAME,
        label="Group: Rename Active…",
        slot_name="_group_rename",
        menu_path=_EDIT_DESIGN_GROUP,
        separator_before=True,
    ),
    # Edit ► Design ► Group ► Exported Pins
    ActionSpec(
        id=KB.ACTION_GROUP_EXPORT_AUTO,
        label="Group: Export Auto (external nets)",
        slot_name="_group_export_auto",
        menu_path=_EDIT_DESIGN_GROUP_EXPORTED,
    ),
    ActionSpec(
        id=KB.ACTION_GROUP_EXPORT_ALL,
        label="Group: Export All Pins",
        slot_name="_group_export_all",
        menu_path=_EDIT_DESIGN_GROUP_EXPORTED,
    ),
    ActionSpec(
        id=KB.ACTION_GROUP_EXPORT_CLEAR,
        label="Group: Clear Exported Pins",
        slot_name="_group_export_clear",
        menu_path=_EDIT_DESIGN_GROUP_EXPORTED,
        separator_before=True,
    ),
    ActionSpec(
        id=KB.ACTION_GROUP_AUTOROUTE,
        label="Group: Autoroute Internal Connections",
        slot_name="_group_autoroute",
        menu_path=_EDIT_DESIGN_GROUP,
        separator_before=True,
    ),
    # Edit ► Board
    ActionSpec(
        id=KB.ACTION_BOARD_RESIZE,
        label="Resize Board…",
        slot_name="_resize_board",
        menu_path=_EDIT_BOARD,
    ),
    ActionSpec(
        id=KB.ACTION_TOOL_CUT,
        label="&Cut Strip",
        slot_name="_tool_cut",
        menu_path=_EDIT_BOARD,
        checkable=True,
        action_group="tool",
    ),
    ActionSpec(
        id=KB.ACTION_BOARD_SPREAD,
        label="Spread Components",
        slot_name="_spread_components",
        menu_path=_EDIT_BOARD,
    ),
    ActionSpec(
        id=KB.ACTION_PREFERENCES,
        label="&Preferences…",
        slot_name="_open_preferences",
        menu_path=_EDIT,
        separator_before=True,
    ),
    # Edit ► Generate
    ActionSpec(
        id=KB.ACTION_GEN_SHORTCUT_CONFIG,
        label="Generate Shortcut Config File",
        slot_name="_generate_shortcut_config",
        menu_path=_EDIT_GENERATE,
    ),
    # ------------------------------------------------------------------
    # Select menu
    # ------------------------------------------------------------------
    ActionSpec(
        id=KB.ACTION_TOOL_SELECT,
        label="&Select",
        slot_name="_tool_select",
        menu_path=_SELECT,
        checkable=True,
        action_group="tool",
    ),
    ActionSpec(
        id=KB.ACTION_SELECT_ALL,
        label="Select &All",
        slot_name="_select_all",
        menu_path=_SELECT,
        separator_before=True,
    ),
    ActionSpec(
        id=KB.ACTION_DESELECT_ALL,
        label="&Deselect All",
        slot_name="_deselect_all",
        menu_path=_SELECT,
    ),
    ActionSpec(
        id=KB.ACTION_SELECT_ALL_COMPONENTS,
        label="Select All &Components",
        slot_name="_select_all_components",
        menu_path=_SELECT,
        separator_before=True,
    ),
    ActionSpec(
        id=KB.ACTION_DESELECT_ALL_COMPONENTS,
        label="Deselect All C&omponents",
        slot_name="_deselect_all_components",
        menu_path=_SELECT,
    ),
    ActionSpec(
        id=KB.ACTION_SELECT_ALL_WIRES,
        label="Select All &Wires",
        slot_name="_select_all_wires",
        menu_path=_SELECT,
        separator_before=True,
    ),
    ActionSpec(
        id=KB.ACTION_DESELECT_ALL_WIRES,
        label="Deselect All Wi&res",
        slot_name="_deselect_all_wires",
        menu_path=_SELECT,
    ),
    ActionSpec(
        id=KB.ACTION_CLEAR_HIGHLIGHT,
        label="Clear &Highlight",
        slot_name="_clear_highlight",
        menu_path=_SELECT,
        enabled=False,
        separator_before=True,
    ),
    # ------------------------------------------------------------------
    # View menu — Camera Top/Bottom are dynamic, NOT in spec list
    # ------------------------------------------------------------------
    ActionSpec(
        id=KB.ACTION_CAMERA_TOGGLE,
        label="Camera: To&ggle",
        slot_name="_set_camera",
        menu_path=_VIEW,
        shortcut_args=("toggle",),
    ),
    ActionSpec(
        id=KB.ACTION_MIRROR_H,
        label="Mirror &Horizontal",
        slot_name="_mirror_view",
        menu_path=_VIEW,
        checkable=True,
        separator_before=True,
    ),
    ActionSpec(
        id=KB.ACTION_MIRROR_V,
        label="Mirror &Vertical",
        slot_name="_mirror_view_v",
        menu_path=_VIEW,
        checkable=True,
    ),
    # View ► Layers — layer cycling per field.  ``shortcut_args`` carries
    # the field name so the slot no longer needs a closure.
    ActionSpec(
        id=KB.ACTION_LAYER_1,
        label="Board",
        slot_name="_cycle_layer",
        menu_path=_VIEW_LAYERS,
        shortcut_args=("board",),
    ),
    ActionSpec(
        id=KB.ACTION_LAYER_2,
        label="Components Top",
        slot_name="_cycle_layer",
        menu_path=_VIEW_LAYERS,
        shortcut_args=("components_top",),
    ),
    ActionSpec(
        id=KB.ACTION_LAYER_3,
        label="Components Bottom",
        slot_name="_cycle_layer",
        menu_path=_VIEW_LAYERS,
        shortcut_args=("components_bottom",),
    ),
    ActionSpec(
        id=KB.ACTION_LAYER_4,
        label="Wires Top",
        slot_name="_cycle_layer",
        menu_path=_VIEW_LAYERS,
        shortcut_args=("wires_top",),
    ),
    ActionSpec(
        id=KB.ACTION_LAYER_5,
        label="Wires Bottom",
        slot_name="_cycle_layer",
        menu_path=_VIEW_LAYERS,
        shortcut_args=("wires_bottom",),
    ),
    ActionSpec(
        id=KB.ACTION_LAYER_6,
        label="Labels",
        slot_name="_cycle_layer",
        menu_path=_VIEW_LAYERS,
        shortcut_args=("labels",),
    ),
    ActionSpec(
        id=KB.ACTION_LAYER_7,
        label="Pin Names",
        slot_name="_cycle_layer",
        menu_path=_VIEW_LAYERS,
        shortcut_args=("meta_pin_labels",),
    ),
    ActionSpec(
        id=KB.ACTION_LAYER_8,
        label="Connections",
        slot_name="_cycle_layer",
        menu_path=_VIEW_LAYERS,
        shortcut_args=("ratsnest",),
    ),
    ActionSpec(
        id=KB.ACTION_LAYER_9,
        label="Pin Pads",
        slot_name="_cycle_layer",
        menu_path=_VIEW_LAYERS,
        shortcut_args=("pin_pads",),
    ),
    ActionSpec(
        id=KB.ACTION_LAYER_10,
        label="Pin Modifiers",
        slot_name="_cycle_layer",
        menu_path=_VIEW_LAYERS,
        shortcut_args=("pin_modifiers",),
    ),
    ActionSpec(
        id=KB.ACTION_LAYERS_RESET,
        label="Reset All Layers",
        slot_name="_reset_layers",
        menu_path=_VIEW_LAYERS,
        separator_before=True,
    ),
    ActionSpec(
        id=KB.ACTION_ZOOM_IN,
        label="Zoom &In",
        slot_name="_zoom_in",
        menu_path=_VIEW,
        separator_before=True,
    ),
    ActionSpec(
        id=KB.ACTION_ZOOM_OUT, label="Zoom &Out", slot_name="_zoom_out", menu_path=_VIEW
    ),
    ActionSpec(
        id=KB.ACTION_FIT_BOARD,
        label="&Fit Board",
        slot_name="_fit_board",
        menu_path=_VIEW,
    ),
    ActionSpec(
        id=KB.ACTION_FIT_AREA, label="Fit &Area", slot_name="_fit_area", menu_path=_VIEW
    ),
    ActionSpec(
        id=KB.ACTION_FIT_SELECTION,
        label="Fit &Selection",
        slot_name="_fit_selection",
        menu_path=_VIEW,
    ),
    ActionSpec(
        id=KB.ACTION_FULLSCREEN,
        label="&Full Screen",
        slot_name="_toggle_fullscreen",
        menu_path=_VIEW,
        separator_before=True,
    ),
    ActionSpec(
        id=KB.ACTION_CUSTOMIZE_TOOLBARS,
        label="&Customize Toolbars…",
        slot_name="_open_toolbar_customize",
        menu_path=_VIEW,
        separator_before=True,
    ),
    # ------------------------------------------------------------------
    # Analysis menu
    # ------------------------------------------------------------------
    ActionSpec(
        id=KB.ACTION_RUN_DRC,
        label="Run &DRC",
        slot_name="_run_drc",
        menu_path=_ANALYSIS,
    ),
    ActionSpec(
        id=KB.ACTION_AUTOROUTE,
        label="&Autoroute Nets… [Inspector selection or all]",
        slot_name="_autoroute",
        menu_path=_ANALYSIS,
    ),
    # orphan: modifier variants of Autoroute (F6).  Alt+F6 forces the
    # pre-flight dialog and Shift+F6 forces skipping it, both regardless
    # of the persisted skip_autoroute_dialog setting.  Not in the menu —
    # they'd just duplicate the Autoroute entry; discoverable via the
    # keybindings list / tooltip.
    ActionSpec(
        id=KB.ACTION_AUTOROUTE_DIALOG,
        label="Autoroute (force dialog)",
        slot_name="_autoroute_force_dialog",
        menu_path=None,
    ),
    # orphan: companion shortcut for autoroute without the pre-flight dialog.
    ActionSpec(
        id=KB.ACTION_AUTOROUTE_NO_DIALOG,
        label="Autoroute (skip dialog)",
        slot_name="_autoroute_skip_dialog",
        menu_path=None,
    ),
    # ------------------------------------------------------------------
    # Orphans — accessible only via shortcut (or panel widget).  Each
    # carries a one-line `# orphan: ...` justification.
    # ------------------------------------------------------------------
    # orphan: lives in the SelectionFilterWidget toolbar widget; the
    # shortcut also lets keyboard users toggle without focusing the panel.
    ActionSpec(
        id=KB.ACTION_FILTER_TOGGLE_COMPONENTS,
        label="Filter: Toggle Components",
        slot_name="_toggle_filter",
        menu_path=None,
        shortcut_args=("components",),
    ),
    # orphan: same as above.
    ActionSpec(
        id=KB.ACTION_FILTER_TOGGLE_WIRES,
        label="Filter: Toggle Wires",
        slot_name="_toggle_filter",
        menu_path=None,
        shortcut_args=("wires",),
    ),
    # orphan: same as above.
    ActionSpec(
        id=KB.ACTION_FILTER_TOGGLE_GROUPS,
        label="Filter: Toggle Groups",
        slot_name="_toggle_filter",
        menu_path=None,
        shortcut_args=("groups",),
    ),
    # orphan: relation cycling lives in HighlightModeWidget; the
    # backtick shortcut is the keyboard fast-path users learn first.
    ActionSpec(
        id=KB.ACTION_HIGHLIGHT_CYCLE,
        label="Cycle Relation Class",
        slot_name="_cycle_relation",
        menu_path=None,
        shortcut_args=(True,),
    ),
    ActionSpec(
        id=KB.ACTION_RELATION_CYCLE_BACK,
        label="Cycle Relation Class (Back)",
        slot_name="_cycle_relation",
        menu_path=None,
        shortcut_args=(False,),
    ),
    # orphan: direct relation-pick shortcuts for power users; no menu
    # because HighlightModeWidget exposes the same set as a combobox.
    ActionSpec(
        id=KB.ACTION_RELATION_NONE,
        label="Relation: None",
        slot_name="_set_relation_by_name",
        menu_path=None,
        shortcut_args=("none",),
    ),
    ActionSpec(
        id=KB.ACTION_RELATION_LOGICAL_NET,
        label="Relation: Same Logical Net",
        slot_name="_set_relation_by_name",
        menu_path=None,
        shortcut_args=("same_logical_net",),
    ),
    # orphan: direct relation-pick shortcut exposed by HighlightModeWidget.
    ActionSpec(
        id=KB.ACTION_RELATION_PHYSICAL_NET,
        label="Relation: Same Physical Net",
        slot_name="_set_relation_by_name",
        menu_path=None,
        shortcut_args=("same_physical_net",),
    ),
    # orphan: direct relation-pick shortcut exposed by HighlightModeWidget.
    ActionSpec(
        id=KB.ACTION_RELATION_UNCONNECTED,
        label="Relation: Unconnected Pins",
        slot_name="_set_relation_by_name",
        menu_path=None,
        shortcut_args=("unconnected_pins",),
    ),
    # orphan: direct relation-pick shortcut exposed by HighlightModeWidget.
    ActionSpec(
        id=KB.ACTION_RELATION_NEIGHBORS,
        label="Relation: Neighbors",
        slot_name="_set_relation_by_name",
        menu_path=None,
        shortcut_args=("neighbors",),
    ),
    # orphan: highlight on/off snapshot toggle — fires when user wants
    # to peek the un-highlighted view; no good menu home.
    ActionSpec(
        id=KB.ACTION_HIGHLIGHT_TOGGLE,
        label="Highlight Toggle",
        slot_name="_highlight_toggle",
        menu_path=None,
    ),
    # Wire-pin conversion — primary entry point is the wire PPM (right
    # click on an endpoint).  Top-menu entries below operate on the
    # currently selected wire(s); status-bar messages explain when the
    # selection isn't eligible.
    ActionSpec(
        id=KB.ACTION_WIRE_CONVERT_TO_WIRE_PIN,
        label="Wire: Convert Endpoint to &Wire-pin",
        slot_name="_convert_endpoint_to_wire_pin_action",
        menu_path=_EDIT_DESIGN_WIRE,
    ),
    ActionSpec(
        id=KB.ACTION_WIRE_CONVERT_FROM_WIRE_PIN,
        label="Wire: Convert Wire-pin &Back to Endpoint",
        slot_name="_convert_wire_pin_to_endpoint_action",
        menu_path=_EDIT_DESIGN_WIRE,
    ),
)


# ---------------------------------------------------------------------------
# Derived dictionaries (kept as module-level constants for cheap access)
# ---------------------------------------------------------------------------


def _build_display_names(specs: tuple[ActionSpec, ...]) -> dict[str, str]:
    return {s.id: strip_mnemonic(s.label) for s in specs}


#: Phase 1 derives display names directly from the spec list.  The
#: ``KB.DISPLAY_NAMES`` dict in ``keybindings.py`` stays as a fallback
#: for any spec missing from the list (currently the two wire-pin
#: convert actions which Phase 2 will add).
DISPLAY_NAMES_FROM_SPECS: dict[str, str] = _build_display_names(ALL_ACTIONS)


__all__ = [
    "ActionSpec",
    "MenuPath",
    "ToolbarGroup",
    "ALL_ACTIONS",
    "DISPLAY_NAMES_FROM_SPECS",
    "strip_mnemonic",
    "KB",
]
