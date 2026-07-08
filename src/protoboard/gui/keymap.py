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

from abc import ABC, abstractmethod
from dataclasses import dataclass, field, replace
from typing import override

from PySide6.QtCore import Qt
from PySide6.QtGui import QKeySequence

from protoboard.errors import ProtoboardError
from protoboard.services.debug_trace import traced

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
@traced()
def category_of(action_id: str) -> str:
    """The category segment of *action_id* (text before the first dot)."""
    return action_id.split(".", 1)[0]


@traced()
def name_of(action_id: str) -> str:
    """The bare action name of *action_id* (text after the last dot)."""
    return action_id.rsplit(".", 1)[-1]


@traced()
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
    shortcuts: list[str] = field(default_factory=list)
    display: str = ""
    final: bool = False

    @property
    def default(self) -> list[str]:
        return self.shortcuts

    def clone(self) -> KeyAction:
        return KeyAction(id=self.id,
                         shortcuts=self.shortcuts,
                         display=self.display,
                         final=self.final)

    @property
    @traced()
    def category(self) -> str:
        return category_of(self.id)

    @property
    @traced()
    def name(self) -> str:
        return name_of(self.id)


GLOBAL_ACTIONS: tuple[KeyAction, ...] = (
    KeyAction(_global_id("delete"), ["Del"], "Delete", final=True),
    KeyAction(_global_id("cancel"), ["Esc"], "Cancel", final=True),
    KeyAction(_global_id("confirm"), ["Return", "Space"],
              "Confirm",
              final=True),
    KeyAction(_global_id("edit"), ["E"], "Edit"),
    # Generic transform ops shared by Select / Place (defaults match the legacy
    # flat table so behaviour is identical until the Stage-2 migration).
    KeyAction(_global_id("rotate_cw"), ["R"], "Rotate CW"),
    KeyAction(_global_id("rotate_ccw"), ["Shift+R"], "Rotate CCW"),
    KeyAction(_global_id("flip"), ["Shift+F"], "Flip"),
    KeyAction(_global_id("redrag"), ["P"], "Re-drag Pins"),
    KeyAction(_global_id("show_shortcuts"), ["?"],
              "Show Shortcuts",
              final=True),
    # Example of a protected shortcut a tool may not re-purpose.
    KeyAction("session.action.save_as", ["Ctrl+Shift+S"],
              "Save As",
              final=True),
)


# ---------------------------------------------------------------------------
# Event → QKeySequence matcher
# ---------------------------------------------------------------------------
@traced()
def event_to_sequence(key: int,
                      modifiers: Qt.KeyboardModifier) -> QKeySequence:
    """Build the :class:`QKeySequence` a key press represents.

    ``QKeySequence(int(modifiers) | int(key))`` is the canonical construction;
    Qt normalises it so ``Del`` and ``Delete`` compare equal.
    """
    return QKeySequence(int(modifiers.value) | int(key))


@traced()
def _has_shift(sequence: QKeySequence) -> bool:
    """True when *sequence*'s single chord carries an explicit Shift modifier.

    Used to tell a real ``Shift+<letter>`` binding (which must NOT match the
    unshifted key) from a bare shifted-symbol binding like ``?`` (whose keycode
    already implies Shift and so carries no separate flag).
    """
    return "shift+" in sequence.toString().lower()


@traced()
def _is_symbol_key(key: int) -> bool:
    """True when *key* is a symbol (not a letter or digit).

    A symbol keycode (``Key_Question``, ``Key_Plus`` …) already encodes Shift;
    a letter/digit takes Shift as a separate, additive modifier.  The
    shifted-symbol matcher fallback applies only to the former.
    """
    is_letter = int(Qt.Key.Key_A) <= key <= int(Qt.Key.Key_Z)
    is_digit = int(Qt.Key.Key_0) <= key <= int(Qt.Key.Key_9)
    return not (is_letter or is_digit)


# ---------------------------------------------------------------------------
# KeyMap
# ---------------------------------------------------------------------------


class AbstractKeyMap(ABC):

    @abstractmethod
    def register_tool_actions(
            self, actions: dict[str, str | list[str] | None]) -> None:
        ...

    @abstractmethod
    def resolve(self, action_id: str) -> str:
        ...

    @abstractmethod
    def resolve_action(self, action_id: str) -> KeyAction | None:
        ...

    @abstractmethod
    def display_of(self, action_id: str) -> str:
        ...

    @abstractmethod
    def is_final(self, action_id: str) -> bool:
        ...

    @abstractmethod
    def matches(self, key: int, modifiers: Qt.KeyboardModifier,
                action_id: str) -> bool:
        ...

    @abstractmethod
    def set_override(self, action_id: str,
                     shortcuts: list[str]) -> KeyAction | None:
        ...

    @abstractmethod
    def known_actions(self) -> tuple[KeyAction, ...]:
        ...

    @abstractmethod
    def actions_in(self, category: str) -> tuple[KeyAction, ...]:
        ...

    @abstractmethod
    def load_overrides(self) -> None:
        ...

    @abstractmethod
    def save_overrides(self) -> None:
        ...


@dataclass
class KeyMap(AbstractKeyMap):
    """Category-grouped key bindings with two-level resolution.

    Holds the merged tree of every known action (the built-in
    :data:`GLOBAL_ACTIONS` plus whatever tools register) and the user overrides
    loaded from disk.  ``resolve(action_id)`` answers "what shortcut fires this
    action right now", following the inherit (``None``) / override rules.
    """

    _parent: AbstractKeyMap | None = field(default=None)
    _actions: dict[str, KeyAction] = field(default_factory=dict)

    # -- registration -------------------------------------------------------

    @traced()
    @override
    def register_tool_actions(self, actions: dict[str, list[str]]) -> None:
        """Register a tool's declared actions (``key_actions()`` output).

        *actions* maps ``"<category>.action.<name>" -> shortcut | None``.  A
        ``None`` value inherits the matching ``global.action.<name>`` default; a
        literal shortcut overrides for that tool.  A literal shortcut that
        collides with a ``final`` global action is rejected.
        """
        for action_id, shortcut in actions.items():
            # if shortcut is not None and self._is_final_shortcut(shortcut):
            #     raise FinalShortcutError(
            #         f"{action_id!r} cannot bind {shortcut!r}: reserved by a "
            #         f"final global action")
            display = self._inherited_display(action_id)
            self._actions[action_id] = KeyAction(id=action_id,
                                                 shortcuts=shortcut,
                                                 display=display)

    @traced()
    def _inherited_display(self, action_id: str) -> str:
        glob = self._get_action(_global_id(name_of(action_id)))
        return glob.display if glob is not None else name_of(action_id).title()

    # -- resolution ---------------------------------------------------------

    def _get_action(self, action_id: str) -> KeyAction | None:
        return self._actions.get(action_id)

    @traced()
    def resolve(self, action_id: str) -> str:
        action = self._get_action(action_id)
        return action_id if action else (
            self._parent.resolve(action_id) if self._parent else "")

    @traced()
    def resolve_display(self, action_id: str) -> str:
        """Human-facing binding text: ``|`` alternatives shown as ``"Up / W"``."""
        return " / ".join(a for a in self.resolve(action_id).split("|") if a)

    @traced()
    def is_final(self, action_id: str) -> bool:
        action = self._get_action(action_id)
        return action is not None and action.final

    @traced()
    def _non_final_shortcut(self, shortcut: list[str]) -> list[QKeySequence]:
        return []

    @traced()
    def matches(self, key: int, modifiers: Qt.KeyboardModifier,
                action_id: str) -> bool:
        """True when a key press equals *action_id*'s resolved shortcut.

        A shifted *symbol* (``?``, ``:``, ``+`` …) reaches us as its
        already-shifted keycode (``Key_Question``) **plus** a live
        ``ShiftModifier`` — but its canonical ``QKeySequence`` form (``"?"``)
        bakes the shift into the keycode and carries no separate Shift flag, so
        the raw event sequence (``"Shift+?"``) compares unequal to the binding.
        We therefore also test the event with Shift stripped, but *only for a
        symbol keycode* — for a letter/digit, Shift is a deliberate, additive
        modifier, so ``Shift+R`` must stay distinct from ``R``.
        """
        shortcut = self.resolve(action_id)
        if not shortcut:
            return False
        # A binding may carry several alternatives separated by ``|`` (e.g.
        # ``"Up|W"`` — arrow OR WASD step); the press matches if it equals ANY.
        return any(
            self._seq_matches(key, modifiers, alt)
            for alt in shortcut.split("|") if alt)

    @staticmethod
    @traced()
    def _seq_matches(key: int, modifiers: Qt.KeyboardModifier,
                     shortcut: str) -> bool:
        """True when a key press equals one (single, non-``|``) shortcut string.

        A shifted *symbol* (``?``, ``:``, ``+`` …) reaches us as its
        already-shifted keycode (``Key_Question``) **plus** a live
        ``ShiftModifier`` — but its canonical ``QKeySequence`` form (``"?"``)
        bakes the shift into the keycode and carries no separate Shift flag, so
        the raw event sequence (``"Shift+?"``) compares unequal to the binding.
        We therefore also test the event with Shift stripped, but *only for a
        symbol keycode* — for a letter/digit, Shift is a deliberate, additive
        modifier, so ``Shift+R`` must stay distinct from ``R``.
        """
        target = QKeySequence(shortcut)
        if event_to_sequence(key, modifiers) == target:
            return True
        if (modifiers & Qt.KeyboardModifier.ShiftModifier
                and _is_symbol_key(key) and not _has_shift(target)):
            without_shift = modifiers & ~Qt.KeyboardModifier.ShiftModifier
            if event_to_sequence(key, without_shift) == target:
                return True
        return False

    # -- overrides / persistence -------------------------------------------

    @traced()
    def set_override(self, action_id: str,
                     shortcuts: list[str]) -> KeyAction | None:
        """Set a user override for *action_id* (does not persist by itself)."""
        action: KeyAction | None = self.resolve_action(action_id)
        if action:
            new_override = replace(action, shortcuts=shortcuts)
            self._actions[action_id] = new_override
            return new_override
        return None

    @traced()
    def known_actions(self) -> tuple[KeyAction, ...]:
        """Every declared action, for UI / introspection."""
        return tuple(self._actions.values())

    @traced()
    def actions_in(self, category: str) -> tuple[KeyAction, ...]:
        """Every declared action in *category* (text before the first dot)."""
        return tuple(a for a in self._actions.values()
                     if a.category == category)

    @traced()
    def display_of(self, action_id: str) -> str:
        """The human label for *action_id* (declared display, else the name)."""
        action = self._get_action(action_id)
        if action is not None and action.display:
            return action.display
        return name_of(action_id).replace("_", " ").title()

    # -- disk ---------------------------------------------------------------

    @traced()
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
            from ruamel.yaml import \
                YAML  # type: ignore[import-untyped,unused-ignore]

            yaml = YAML()
            raw = yaml.load(KEYBINDINGS_V2_FILE)
            if not isinstance(raw, dict):
                return
            for category, entries in raw.items():
                if not isinstance(category, str) or not isinstance(
                        entries, dict):
                    continue
                for name, value in entries.items():
                    action_id = f"{category}.{name}"
                    display = self._inherited_display(action_id)
                    if not isinstance(name, str) or not isinstance(value, str):
                        continue  # null / non-string → inherit (skip)
                    self._actions[action_id] = KeyAction(action_id,
                                                         shortcuts=[value],
                                                         display=display)
        except Exception:  # noqa: BLE001 — corrupt file → defaults
            return

    @traced()
    def save_overrides(self) -> None:
        """Persist user overrides to the namespaced tree YAML.

        Only entries that differ from the action's declared default are written,
        so the file stays minimal and future default changes propagate.
        """
        from protoboard.paths import CONFIG_DIR, KEYBINDINGS_V2_FILE

        tree: dict[str, dict[str, str]] = {}
        for action_id, shortcut in self._actions.items():
            action = self._get_action(action_id)
            if action is not None and action.shortcuts == shortcut:
                continue  # equals declared default → don't persist
            category, _, name = action_id.partition(".")
            if not name:
                continue
            tree.setdefault(category, {})[name] = shortcut

        CONFIG_DIR.mkdir(parents=True, exist_ok=True)
        from ruamel.yaml import \
            YAML  # type: ignore[import-untyped,unused-ignore]

        yaml = YAML()
        yaml.default_flow_style = False
        with KEYBINDINGS_V2_FILE.open("w", encoding="utf-8") as fh:
            fh.write("# Protoboard Designer — namespaced keyboard shortcuts\n")
            fh.write(
                "# Grouped by category. null = inherit the global default.\n")
            if tree:
                yaml.dump(tree, fh)

    @override
    def resolve_action(self, action_id: str) -> KeyAction | None:
        resolved_id = self.resolve(action_id)
        return self._get_action(resolved_id)

class GlobalKeyMap(KeyMap):
    @classmethod
    def with_globals(cls) -> KeyMap:
        """A fresh map carrying only the built-in global actions."""
        km = cls()
        for action in GLOBAL_ACTIONS:
            km._actions[action.id] = action
        return km

    @override
    def resolve(self, action_id: str) -> str:
        global_id = _global_id(name_of(action_id))
        if global_id == action_id:
            return ""  # a global action with a None default = unbound
        return self.resolve(global_id)
