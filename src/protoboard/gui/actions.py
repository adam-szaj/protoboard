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
  changes at runtime when the user flips the board.
* The layer subset toggles share a tuple-driven generator already (see
  ``_LAYER_ENTRIES`` in ``main_window.py``); migrating them into specs is
  optional and tracked under Phase 2 follow-up.
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
        default_toolbar: Initial toolbar group; the user may add or
            remove the action via ``View ► Customize Toolbars…``.
            ``None`` means "not on any toolbar by default."
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
    default_toolbar: ToolbarGroup | None
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
# Specs
# ---------------------------------------------------------------------------

#: Authoritative list.  Phase 0 ships empty so the rest of the codebase
#: stays untouched until Phase 1 migrates the existing actions in one
#: behavioural-no-op PR.
ALL_ACTIONS: tuple[ActionSpec, ...] = ()


# ---------------------------------------------------------------------------
# Derived dictionaries (kept as module-level constants for cheap access)
# ---------------------------------------------------------------------------


def _build_display_names(specs: tuple[ActionSpec, ...]) -> dict[str, str]:
    return {s.id: strip_mnemonic(s.label) for s in specs}


#: Phase 1 will replace ``KB.DISPLAY_NAMES`` with this view.  Until then
#: the dict is empty (no specs yet) and consumers still use
#: ``KB.DISPLAY_NAMES`` directly.
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
