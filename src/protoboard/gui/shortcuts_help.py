# SPDX-License-Identifier: MIT
"""Shared "what shortcuts are available right now" helper.

Two UI surfaces consume the same data — the ``[?]`` shortcut overlay and the
right-click *tool actions* submenu.  Both ask: for the active tool (and the
generic globals it inherits), which actions are bound, to what key, and how do
I trigger one?  This module answers that against the live
:class:`~protoboard.gui.keymap.KeyMap`, so both surfaces stay in sync with the
user's current bindings without duplicating logic.
"""

from __future__ import annotations

from dataclasses import dataclass

from PySide6.QtGui import QKeySequence

from protoboard.gui import keybindings as KB
from protoboard.gui.actions import ALL_ACTIONS, strip_mnemonic
from protoboard.gui.keymap import KeyMap, category_of

__all__ = [
    "ShortcutRow",
    "tool_shortcut_rows",
    "app_shortcut_sections",
    "shortcut_to_key_mods",
]


@dataclass(frozen=True)
class ShortcutRow:
    """One displayable shortcut: ``action_id``, label and resolved key string.

    ``shortcut`` is the current ``QKeySequence`` string (e.g. ``"W"``,
    ``"Ctrl+R"``); empty when the action is unbound.
    """

    action_id: str
    label: str
    shortcut: str


def tool_shortcut_rows(keymap: KeyMap, action_ids: list[str]) -> list[ShortcutRow]:
    """Rows for *action_ids*, resolved + labelled through *keymap*.

    Unbound actions (no resolved shortcut) are included with an empty string so
    the overlay can still show them; the menu can choose to skip those.  Order
    follows *action_ids*.
    """
    rows: list[ShortcutRow] = []
    for action_id in action_ids:
        rows.append(
            ShortcutRow(
                action_id=action_id,
                label=keymap.display_of(action_id),
                shortcut=keymap.resolve_display(action_id),
            )
        )
    return rows


def app_shortcut_sections(
    keybindings: object, *, bound_only: bool = True
) -> list[tuple[str, list[ShortcutRow]]]:
    """The real application shortcuts, grouped by top-level menu.

    Drives the ``[?]`` overlay's global section from the authoritative
    :data:`~protoboard.gui.actions.ALL_ACTIONS` registry + the live
    :class:`~protoboard.gui.keybindings.KeyBindings` (so user overrides show),
    instead of a hand-maintained id list.  Grouped by ``menu_path[0]``
    (File / Edit / Select / View / Analysis); orphan actions (no menu path) go
    under "Other".  ``bound_only`` drops actions with no shortcut (the common
    case for the overlay — a reference of *active* keys, not every action).

    *keybindings* is duck-typed: anything with ``get(action_id) -> str`` works
    (the real ``KeyBindings`` or a test stub).
    """
    get = getattr(keybindings, "get", None)

    def shortcut_of(action_id: str) -> str:
        if callable(get):
            return str(get(action_id))
        return KB.DEFAULTS.get(action_id, "")

    grouped: dict[str, list[ShortcutRow]] = {}
    order: list[str] = []
    for spec in ALL_ACTIONS:
        sc = shortcut_of(spec.id)
        if bound_only and not sc:
            continue
        heading = spec.menu_path[0] if spec.menu_path else "Other"
        if heading not in grouped:
            grouped[heading] = []
            order.append(heading)
        grouped[heading].append(
            ShortcutRow(
                action_id=spec.id,
                label=strip_mnemonic(spec.label),
                shortcut=sc,
            )
        )
    return [(h, grouped[h]) for h in order]


def category_label(action_id: str) -> str:
    """A title-cased category heading from an action id (``select-tool`` → …)."""
    return category_of(action_id).replace("-", " ").replace("_", " ").title()


def shortcut_to_key_mods(shortcut: str) -> tuple[int, int] | None:
    """Decompose a ``QKeySequence`` string into ``(key, modifiers)`` ints.

    Lets a menu item synthesise the exact ``QKeyEvent`` for an action and feed
    it back through the tool's ``on_key`` — reusing the real dispatch path
    rather than duplicating each action's behaviour.  ``None`` for an empty /
    unparseable shortcut.
    """
    if not shortcut:
        return None
    # A multi-bind string (``"Up|W"``) → synthesise the first (primary) bind.
    primary = shortcut.split("|")[0]
    if not primary:
        return None
    seq = QKeySequence(primary)
    if seq.isEmpty():
        return None
    combo = seq[0]  # type: ignore[index]  # PySide6 stubs miss __getitem__
    return int(combo.key()), int(combo.keyboardModifiers().value)
