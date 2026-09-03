# SPDX-License-Identifier: MIT
"""Walk an :class:`ActionSpec` list and wire the UI plumbing.

The builder is the *only* code that creates a QAction.  Adding a new
user-visible feature now means:

1. Add a single :class:`ActionSpec` entry to
   :data:`protoboard.gui.actions.ALL_ACTIONS`.
2. Add the slot method on ``MainWindow`` (or a registered sub-owner).
3. (Optional) Drop the action into
   :data:`protoboard.paths.BUILTIN_DEFAULT_TOOLBARS_FILE` if it should
   appear on a toolbar out of the box.

``ActionRegistry`` is **per-MainWindow**.  ProtoboardDesigner is a
single-window application; should it gain a second window, each window
gets its own registry constructed against its own slot owners.

Toolbar layout is **full-state** in the user's
``~/.config/protoboard/toolbars.yaml`` — there is no "default placement
embedded in the spec" path any more.  The builder receives the
already-loaded layout dict and walks it, looking up action ids in the
registry it just built and dropping them onto the corresponding
QToolBar.  Empty groups create no toolbar at all (cleaner UX than an
empty bar).
"""

from __future__ import annotations

import functools
from collections.abc import Callable, Mapping, Sequence
from dataclasses import dataclass, field
from typing import cast

from PySide6.QtCore import Qt
from PySide6.QtGui import QAction, QActionGroup, QIcon, QKeySequence
from PySide6.QtWidgets import QMainWindow, QMenu, QMenuBar, QToolBar

from protoboard.errors import ActionRegistrationError
from protoboard.gui import keybindings as KB
from protoboard.gui.actions import ActionSpec, MenuPath, ToolbarGroup

# ---------------------------------------------------------------------------
# Registry
# ---------------------------------------------------------------------------


@dataclass
class ActionRegistry:
    """Everything the builder produces.

    ``actions`` and ``handlers`` are kept as **mutable dicts** so the
    existing ``MainWindow._actions`` / ``_action_handlers`` callsites can
    continue to alias them without copying.  The other fields are
    internal — callers typically reach for ``registry.actions[id]``.
    """

    actions: dict[str, QAction] = field(default_factory=dict)
    handlers: dict[str, Callable[[], None]] = field(default_factory=dict)
    menus: dict[MenuPath, QMenu] = field(default_factory=dict)
    toolbars: dict[ToolbarGroup, QToolBar] = field(default_factory=dict)
    action_groups: dict[str, QActionGroup] = field(default_factory=dict)


# ---------------------------------------------------------------------------
# Slot resolution
# ---------------------------------------------------------------------------


def resolve_slot(
    owners: Mapping[str, object],
    slot_name: str,
    bound_args: tuple[object, ...],
    *,
    default_owner_key: str = "mw",
) -> Callable[[], None]:
    """Resolve ``slot_name`` against ``owners`` and partial-apply args.

    ``slot_name`` may be a bare method name (resolved on
    ``owners[default_owner_key]``) or a dotted path
    ``"<owner>.<method>"`` resolved against ``owners[<owner>]``.

    Raises:
        ActionRegistrationError: when the owner or method is missing,
            or the resolved attribute is not callable.
    """
    if "." in slot_name:
        owner_key, method_name = slot_name.split(".", 1)
    else:
        owner_key, method_name = default_owner_key, slot_name

    owner = owners.get(owner_key)
    if owner is None:
        raise ActionRegistrationError(
            f"slot_name {slot_name!r}: no registered owner {owner_key!r}"
        )
    method = getattr(owner, method_name, None)
    if method is None or not callable(method):
        raise ActionRegistrationError(
            f"slot_name {slot_name!r}: owner {owner_key!r} has no "
            f"callable attribute {method_name!r}"
        )
    callback = cast(Callable[..., None], method)
    if bound_args:
        return cast(Callable[[], None], functools.partial(callback, *bound_args))
    return cast(Callable[[], None], callback)


# ---------------------------------------------------------------------------
# Builder
# ---------------------------------------------------------------------------


def build_actions(
    main_window: QMainWindow,
    specs: Sequence[ActionSpec],
    keybindings: KB.KeyBindings,
    *,
    slot_owners: Mapping[str, object],
    toolbar_layout: Mapping[ToolbarGroup, Sequence[str]] | None = None,
    menu_bar: QMenuBar | None = None,
) -> ActionRegistry:
    """Create QActions for every spec and wire them into menus + toolbars.

    Args:
        main_window: Parent of QActions and toolbars.  Receives
            ``addAction(...)`` calls so shortcuts work even when no menu
            is visible.
        specs: Authoritative spec list (usually ``ALL_ACTIONS``).
        keybindings: Source of shortcut strings.  User overrides are
            already merged in by ``KB.KeyBindings.load``.
        slot_owners: ``{owner_key: object}`` — at minimum ``{"mw": main_window}``.
            Sub-tools (SelectTool, RouteTool) register themselves under
            their own keys so dotted slot names resolve there.
        toolbar_layout: Full-state ``{group: [action_id, ...]}`` mapping
            loaded from ``toolbars.yaml``.  ``None`` means "build no
            toolbars at all" (used by tests that only care about the
            menu / shortcut side).
        menu_bar: Where to attach top-level menus.  Defaults to
            ``main_window.menuBar()``.  Tests pass an explicit bar.

    Returns:
        Fully wired :class:`ActionRegistry`.

    Raises:
        ActionRegistrationError: on duplicate ``ActionSpec.id`` or
            slot resolution failure.
    """
    registry = ActionRegistry()
    bar = menu_bar if menu_bar is not None else main_window.menuBar()

    seen_ids: set[str] = set()
    for spec in specs:
        if spec.id in seen_ids:
            raise ActionRegistrationError(f"duplicate ActionSpec id: {spec.id!r}")
        seen_ids.add(spec.id)

        slot = resolve_slot(slot_owners, spec.slot_name, spec.shortcut_args)

        action = QAction(spec.label, main_window)
        action.setCheckable(spec.checkable)
        action.setEnabled(spec.enabled)
        if spec.tooltip is not None:
            action.setToolTip(spec.tooltip)
        if spec.icon is not None:
            action.setIcon(QIcon.fromTheme(spec.icon))
        shortcut_str = keybindings.get(spec.id)
        if shortcut_str:
            action.setShortcut(QKeySequence(shortcut_str))

        action.triggered.connect(lambda _checked=False, cb=slot: cb())
        main_window.addAction(action)

        registry.actions[spec.id] = action
        registry.handlers[spec.id] = slot

        if spec.action_group is not None:
            group = registry.action_groups.get(spec.action_group)
            if group is None:
                group = QActionGroup(main_window)
                group.setExclusive(True)
                registry.action_groups[spec.action_group] = group
            group.addAction(action)

        if spec.menu_path is not None:
            menu = _ensure_menu(bar, spec.menu_path, registry)
            if spec.separator_before:
                menu.addSeparator()
            menu.addAction(action)

    if toolbar_layout is not None:
        _apply_toolbar_layout(main_window, toolbar_layout, registry)

    return registry


# ---------------------------------------------------------------------------
# Internal helpers
# ---------------------------------------------------------------------------


def _ensure_menu(
    bar: QMenuBar,
    path: MenuPath,
    registry: ActionRegistry,
) -> QMenu:
    """Walk ``path``, creating nested QMenus on demand.  Cached in
    ``registry.menus`` so repeated specs reuse the same submenu."""
    if not path:
        raise ActionRegistrationError("menu_path must be non-empty when set")

    cached = registry.menus.get(path)
    if cached is not None:
        return cached

    parent_menu: QMenu | None = None
    for depth in range(1, len(path) + 1):
        prefix = path[:depth]
        existing = registry.menus.get(prefix)
        if existing is not None:
            parent_menu = existing
            continue
        title = prefix[-1]
        if depth == 1:
            # Top-level menus get an automatic mnemonic on the first
            # letter so Alt+<letter> opens the menu from the closed
            # state (e.g. Alt+S → Select).  Without this, the menu bar
            # is only navigable via mouse or F10-then-arrows.  Keeps
            # the registry key stable (callers index by the raw path).
            display_title = title if "&" in title else "&" + title
            new_menu = bar.addMenu(display_title)
        else:
            assert parent_menu is not None
            new_menu = parent_menu.addMenu(title)
        registry.menus[prefix] = new_menu
        parent_menu = new_menu
    assert parent_menu is not None
    return parent_menu


def ensure_toolbar(
    main_window: QMainWindow,
    group: ToolbarGroup,
    registry: ActionRegistry,
) -> QToolBar:
    """Create the QToolBar for ``group`` once and cache it.  Every
    toolbar is created movable + floatable; user controls visibility
    and ordering via the Customize Toolbars dialog or by editing
    ``toolbars.yaml`` directly."""
    cached = registry.toolbars.get(group)
    if cached is not None:
        return cached
    toolbar = main_window.addToolBar(group.capitalize())
    toolbar.setObjectName(f"tb_{group}")
    toolbar.setMovable(True)
    toolbar.setFloatable(True)
    toolbar.setToolButtonStyle(Qt.ToolButtonStyle.ToolButtonTextBesideIcon)
    registry.toolbars[group] = toolbar
    return toolbar


def _apply_toolbar_layout(
    main_window: QMainWindow,
    layout: Mapping[ToolbarGroup, Sequence[str]],
    registry: ActionRegistry,
) -> None:
    """Realise the full-state toolbar layout on top of an already-built
    ``ActionRegistry``.  Unknown action ids are silently skipped — the
    YAML may pre-date a spec removal or rename; the loader logs each
    occurrence at warning level."""
    for group, action_ids in layout.items():
        if not action_ids:
            continue
        toolbar = ensure_toolbar(main_window, group, registry)
        for aid in action_ids:
            action = registry.actions.get(aid)
            if action is None:
                continue
            toolbar.addAction(action)


__all__ = [
    "ActionRegistry",
    "build_actions",
    "ensure_toolbar",
    "resolve_slot",
]
