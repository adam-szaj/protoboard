# SPDX-License-Identifier: MIT
"""Namespaced, category-grouped keyboard actions (keybindings redesign — Stage 1).

This is the *new* keybindings layer that lives alongside the legacy flat
:mod:`protoboard.gui.keybindings` table.  Where the legacy table is a single
``action_id -> shortcut`` dict driving the global ``QAction`` shortcuts, this
layer models actions in **categories** (``ui`` / ``session`` / ``global`` /
``select-tool`` / ``route-tool`` / …) and lets a *tool* declare its own actions,
each of which either carries its own shortcut or *inherits* a generic global
default.

Why a second layer (Stage 1 only): the global ``QAction`` system fires window
shortcuts before the focused board view sees a key, so a tool that needs a key
contextually (a wire-edit fragment claiming ``[W]`` for reroute) must veto the
shortcut.  The veto decision has to follow *current* (user-rebindable) bindings
— a tool may not hard-code ``Qt.Key.Key_W``.  This module is the dynamic source
of truth the veto consults.  The Preferences UI, the on-disk migration of the
~160 legacy global actions, and wiring the global ``QAction``s onto this model
are deliberately later stages.

Resolution is two-level:

* a tool action whose value is a literal shortcut string **overrides** for that
  tool (e.g. ``select-tool.action.reroute: "W"``);
* a tool action whose value is ``None`` **inherits** the generic global default
  for the same bare name — ``select-tool.action.edit: null`` resolves to
  ``global.action.edit``.

A global action may be marked ``final``: no tool binding may claim its shortcut.
Registering a tool binding whose literal shortcut collides with a ``final``
global action is rejected (raises :class:`FinalShortcutError`).
"""

from __future__ import annotations

from dataclasses import dataclass, field

from PySide6.QtCore import Qt
from PySide6.QtGui import QKeySequence

from protoboard.errors import ProtoboardError

__all__ = [
    "KeyAction",
    "KeyMap",
    "FinalShortcutError",
    "GLOBAL_ACTIONS",
    "category_of",
    "name_of",
    "event_to_sequence",
]


class FinalShortcutError(ProtoboardError):
    """A tool tried to bind a shortcut reserved by a ``final`` global action."""


# ---------------------------------------------------------------------------
# Action ids
# ---------------------------------------------------------------------------
#
# Action ids are dotted: ``<category>.action.<name>``.  The category is the
# segment before the first dot; the bare name is the segment after the last
# dot.  ``global.action.<name>`` holds the generic default a tool action can
# inherit by binding the same bare name to ``None``.


def category_of(action_id: str) -> str:
    """The category segment of *action_id* (text before the first dot)."""
    return action_id.split(".", 1)[0]


def name_of(action_id: str) -> str:
    """The bare action name of *action_id* (text after the last dot)."""
    return action_id.rsplit(".", 1)[-1]


def _global_id(name: str) -> str:
    return f"global.action.{name}"


# ---------------------------------------------------------------------------
# KeyAction
# ---------------------------------------------------------------------------


@dataclass(frozen=True)
class KeyAction:
    """One bindable action.

    ``default`` is the shortcut string (``QKeySequence`` syntax — ``"W"``,
    ``"Ctrl+Shift+S"``, ``"Del"``) or ``None`` to inherit the matching generic
    global default.  ``final`` (only meaningful on ``global.*`` actions) marks a
    shortcut no tool may claim.
    """

    id: str
    default: str | None
    display: str = ""
    final: bool = False

    @property
    def category(self) -> str:
        return category_of(self.id)

    @property
    def name(self) -> str:
        return name_of(self.id)


# ---------------------------------------------------------------------------
# Generic global actions (Stage 1 — minimal set: what SelectTool inherits +
# one ``final`` example).  The full ~160-action migration is a later stage.
# ---------------------------------------------------------------------------

GLOBAL_ACTIONS: tuple[KeyAction, ...] = (
    KeyAction(_global_id("delete"), "Del", "Delete"),
    KeyAction(_global_id("cancel"), "Esc", "Cancel"),
    KeyAction(_global_id("edit"), "E", "Edit"),
    KeyAction(_global_id("confirm"), "Return", "Confirm"),
    # Example of a protected shortcut a tool may not re-purpose.
    KeyAction("session.action.save_as", "Ctrl+Shift+S", "Save As", final=True),
)


# ---------------------------------------------------------------------------
# Event → QKeySequence matcher
# ---------------------------------------------------------------------------


def event_to_sequence(key: int, modifiers: Qt.KeyboardModifier) -> QKeySequence:
    """Build the :class:`QKeySequence` a key press represents.

    ``QKeySequence(int(modifiers) | int(key))`` is the canonical construction;
    Qt normalises it so ``Del`` and ``Delete`` compare equal.
    """
    return QKeySequence(int(modifiers.value) | int(key))


# ---------------------------------------------------------------------------
# KeyMap
# ---------------------------------------------------------------------------


@dataclass
class KeyMap:
    """Category-grouped key bindings with two-level resolution.

    Holds the merged tree of every known action (the built-in
    :data:`GLOBAL_ACTIONS` plus whatever tools register) and the user overrides
    loaded from disk.  ``resolve(action_id)`` answers "what shortcut fires this
    action right now", following the inherit (``None``) / override rules.
    """

    #: action_id -> KeyAction (the declared actions, defaults + final flags).
    _actions: dict[str, KeyAction] = field(default_factory=dict)
    #: action_id -> shortcut string, user overrides loaded from disk.  An entry
    #: of ``""`` means "explicitly unbound"; a missing entry falls back to the
    #: action's declared default.
    _overrides: dict[str, str] = field(default_factory=dict)

    # -- construction -------------------------------------------------------

    @classmethod
    def with_globals(cls) -> KeyMap:
        """A fresh map carrying only the built-in global actions."""
        km = cls()
        for action in GLOBAL_ACTIONS:
            km._actions[action.id] = action
        return km

    # -- registration -------------------------------------------------------

    def register_tool_actions(
        self, actions: dict[str, str | None]
    ) -> None:
        """Register a tool's declared actions (``key_actions()`` output).

        *actions* maps ``"<category>.action.<name>" -> shortcut | None``.  A
        ``None`` value inherits the matching ``global.action.<name>`` default; a
        literal shortcut overrides for that tool.  A literal shortcut that
        collides with a ``final`` global action is rejected.
        """
        for action_id, shortcut in actions.items():
            if shortcut is not None and self._is_final_shortcut(shortcut):
                raise FinalShortcutError(
                    f"{action_id!r} cannot bind {shortcut!r}: reserved by a "
                    f"final global action"
                )
            display = self._inherited_display(action_id)
            self._actions[action_id] = KeyAction(
                id=action_id, default=shortcut, display=display
            )

    def _inherited_display(self, action_id: str) -> str:
        glob = self._actions.get(_global_id(name_of(action_id)))
        return glob.display if glob is not None else name_of(action_id).title()

    # -- resolution ---------------------------------------------------------

    def resolve(self, action_id: str) -> str:
        """The shortcut string that currently fires *action_id* ('' if none).

        Order: user override on this action (if present, taken verbatim — ``""``
        means explicitly unbound) → the action's declared default → when that
        default is ``None``, the *resolved* generic ``global.action.<name>``
        (so a user override on the global binding propagates to every tool
        action that inherits it).
        """
        if action_id in self._overrides:
            return self._overrides[action_id]
        action = self._actions.get(action_id)
        if action is None:
            return ""
        if action.default is not None:
            return action.default
        # Inherit: resolve the matching global action (unless this *is* it).
        global_id = _global_id(name_of(action_id))
        if global_id == action_id:
            return ""  # a global action with a None default = unbound
        return self.resolve(global_id)

    def is_final(self, action_id: str) -> bool:
        action = self._actions.get(action_id)
        return action is not None and action.final

    def _is_final_shortcut(self, shortcut: str) -> bool:
        target = QKeySequence(shortcut)
        if target.isEmpty():
            return False
        for action in self._actions.values():
            if not (action.final and action.default):
                continue
            if QKeySequence(action.default) == target:
                return True
        return False

    # -- matching -----------------------------------------------------------

    def matches(
        self, key: int, modifiers: Qt.KeyboardModifier, action_id: str
    ) -> bool:
        """True when a key press equals *action_id*'s resolved shortcut."""
        shortcut = self.resolve(action_id)
        if not shortcut:
            return False
        return event_to_sequence(key, modifiers) == QKeySequence(shortcut)

    # -- overrides / persistence -------------------------------------------

    def set_override(self, action_id: str, shortcut: str) -> None:
        """Set a user override for *action_id* (does not persist by itself)."""
        self._overrides[action_id] = shortcut

    def known_actions(self) -> tuple[KeyAction, ...]:
        """Every declared action, for UI / introspection."""
        return tuple(self._actions.values())

    # -- disk ---------------------------------------------------------------

    def load_overrides(self) -> None:
        """Read user overrides from the namespaced tree YAML, if present.

        Shape (categories nest one level; the inner key is ``action.<name>``)::

            global:
              action.delete: "Del"
            select-tool:
              action.reroute: "Q"     # override
              action.edit: null       # inherit global.action.edit

        A ``null`` value is *not* an override — it means "inherit", which is the
        no-entry default anyway, so we skip it.  Corrupt files fall back to
        defaults silently.
        """
        from protoboard.paths import KEYBINDINGS_V2_FILE

        if not KEYBINDINGS_V2_FILE.exists():
            return
        try:
            from ruamel.yaml import YAML  # type: ignore[import-untyped,unused-ignore]

            yaml = YAML()
            raw = yaml.load(KEYBINDINGS_V2_FILE)
            if not isinstance(raw, dict):
                return
            for category, entries in raw.items():
                if not isinstance(category, str) or not isinstance(entries, dict):
                    continue
                for name, value in entries.items():
                    if not isinstance(name, str) or not isinstance(value, str):
                        continue  # null / non-string → inherit (skip)
                    self._overrides[f"{category}.{name}"] = value
        except Exception:  # noqa: BLE001 — corrupt file → defaults
            return

    def save_overrides(self) -> None:
        """Persist user overrides to the namespaced tree YAML.

        Only entries that differ from the action's declared default are written,
        so the file stays minimal and future default changes propagate.
        """
        from protoboard.paths import CONFIG_DIR, KEYBINDINGS_V2_FILE

        tree: dict[str, dict[str, str]] = {}
        for action_id, shortcut in self._overrides.items():
            action = self._actions.get(action_id)
            if action is not None and action.default == shortcut:
                continue  # equals declared default → don't persist
            category, _, name = action_id.partition(".")
            if not name:
                continue
            tree.setdefault(category, {})[name] = shortcut

        CONFIG_DIR.mkdir(parents=True, exist_ok=True)
        from ruamel.yaml import YAML  # type: ignore[import-untyped,unused-ignore]

        yaml = YAML()
        yaml.default_flow_style = False
        with KEYBINDINGS_V2_FILE.open("w", encoding="utf-8") as fh:
            fh.write("# Protoboard Designer — namespaced keyboard shortcuts\n")
            fh.write("# Grouped by category. null = inherit the global default.\n")
            if tree:
                yaml.dump(tree, fh)
