# SPDX-License-Identifier: MIT
"""Walk an :class:`ActionSpec` list and wire the UI plumbing.

The builder is the *only* code that creates a QAction.  Adding a new
user-visible feature now means:

1. Add a single :class:`ActionSpec` entry to
   :data:`protoboard.gui.actions.ALL_ACTIONS`.
2. Add the slot method on ``MainWindow`` (or a registered sub-owner).
3. Done — keybinding, menu placement and toolbar all follow from (1).

``ActionRegistry`` is **per-MainWindow**.  ProtoboardDesigner is a
single-window application; should it gain a second window, each window
gets its own registry constructed against its own slot owners.
"""

from __future__ import annotations

import functools
from collections.abc import Callable, Mapping, Sequence
from dataclasses import dataclass, field
from typing import TYPE_CHECKING, cast

from PySide6.QtCore import Qt
from PySide6.QtGui import QAction, QActionGroup, QIcon, QKeySequence
from PySide6.QtWidgets import QMainWindow, QMenu, QMenuBar, QToolBar

from protoboard.errors import ActionRegistrationError
from protoboard.gui import keybindings as KB
from protoboard.gui.actions import ActionSpec, MenuPath, ToolbarGroup

if TYPE_CHECKING:  # avoid import cycle at runtime
    pass


# ---------------------------------------------------------------------------
# Toolbar customisation hook (placeholder until Phase 4)
# ---------------------------------------------------------------------------


@dataclass(frozen=True, slots=True)
class ToolbarCustomization:
    """User-supplied per-toolbar add/remove diff.

    Phase 4 persists this in ``app_settings.yaml``.  Until then the
    builder accepts ``None`` and applies no overrides.

    Attributes:
        added: ``{group: [action_id, ...]}`` — extra actions to show on
            a toolbar regardless of the spec's ``default_toolbar``.
        removed: ``{group: [action_id, ...]}`` — actions to hide even if
            their spec lists this group as the default.
    """

    added: Mapping[ToolbarGroup, Sequence[str]] = field(default_factory=dict)
    removed: Mapping[ToolbarGroup, Sequence[str]] = field(default_factory=dict)


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
    toolbar_customization: ToolbarCustomization | None = None,
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
        toolbar_customization: Optional user-saved overrides for toolbar
            placement.  ``None`` means "apply spec defaults only."
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
    customization = toolbar_customization or ToolbarCustomization()

    seen_ids: set[str] = set()
    for spec in specs:
        if spec.id in seen_ids:
            raise ActionRegistrationError(
                f"duplicate ActionSpec id: {spec.id!r}"
            )
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

        toolbar_group = _resolve_toolbar_placement(spec, customization)
        if toolbar_group is not None:
            toolbar = _ensure_toolbar(main_window, toolbar_group, registry)
            toolbar.addAction(action)

    _apply_pure_customization_additions(
        main_window, customization, registry,
    )

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
            new_menu = bar.addMenu(title)
        else:
            assert parent_menu is not None
            new_menu = parent_menu.addMenu(title)
        registry.menus[prefix] = new_menu
        parent_menu = new_menu
    assert parent_menu is not None
    return parent_menu


def _ensure_toolbar(
    main_window: QMainWindow,
    group: ToolbarGroup,
    registry: ActionRegistry,
) -> QToolBar:
    """Create the QToolBar for ``group`` once and cache it."""
    cached = registry.toolbars.get(group)
    if cached is not None:
        return cached
    toolbar = main_window.addToolBar(group.capitalize())
    toolbar.setObjectName(f"tb_{group}")
    toolbar.setToolButtonStyle(Qt.ToolButtonStyle.ToolButtonTextBesideIcon)
    registry.toolbars[group] = toolbar
    return toolbar


def _resolve_toolbar_placement(
    spec: ActionSpec,
    customization: ToolbarCustomization,
) -> ToolbarGroup | None:
    """Compose spec default with user add/remove diff.

    Returns the toolbar where the action should appear *now*, or
    ``None`` if it should not appear on any toolbar.  User ``removed``
    overrides the spec default; user ``added`` is applied separately
    by :func:`_apply_pure_customization_additions` because a single
    action could be promoted to a toolbar group it does not natively
    list.
    """
    default = spec.default_toolbar
    if default is None:
        return None
    removed = customization.removed.get(default, ())
    if spec.id in removed:
        return None
    return default


def _apply_pure_customization_additions(
    main_window: QMainWindow,
    customization: ToolbarCustomization,
    registry: ActionRegistry,
) -> None:
    """Honour ``customization.added`` even for actions whose spec has
    ``default_toolbar=None``.  Skips unknown ids silently — the YAML may
    pre-date a spec removal."""
    for group, ids in customization.added.items():
        for action_id in ids:
            action = registry.actions.get(action_id)
            if action is None:
                continue
            toolbar = _ensure_toolbar(main_window, group, registry)
            if action not in toolbar.actions():
                toolbar.addAction(action)


__all__ = [
    "ActionRegistry",
    "ToolbarCustomization",
    "build_actions",
    "resolve_slot",
]
