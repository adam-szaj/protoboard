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
from collections.abc import Callable
from dataclasses import dataclass, field

from PySide6.QtCore import Qt
from PySide6.QtGui import QKeySequence
from typing_extensions import override

from protoboard.errors import ProtoboardError
from protoboard.instrumentation.debug_trace import traced
from protoboard.paths import (
    BUILTIN_DEFAULT_KEYBINDINGS_FILE,
    CONFIG_DIR,
    KEYBINDINGS_V2_FILE,
)

__all__ = [
    "KeyAction",
    "KeyMap",
    "FinalShortcutError",
    "GLOBAL_ACTIONS",
    "VIEW_ACTIONS",
    "VIEW_ZOOM_IN",
    "VIEW_ZOOM_OUT",
    "VIEW_PAN",
    "VIEW_CANVAS_MENU",
    "category_of",
    "name_of",
    "event_to_sequence",
    "is_mouse_trigger",
    "mouse_button_chord",
    "wheel_chord",
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


def _shortcut_text(shortcuts: list[str] | str | None) -> str | None:
    """Canonical ``"A|B"`` text of a stored shortcut value; ``None`` = inherit."""
    if shortcuts is None or isinstance(shortcuts, str):
        return shortcuts
    return "|".join(shortcuts)


def _alternatives(shortcuts: list[str] | str | None) -> set[str]:
    """The set of individual triggers of a stored shortcut value."""
    text = _shortcut_text(shortcuts)
    return {a for a in text.split("|") if a} if text else set()


# ---------------------------------------------------------------------------
# Mouse chords (Stage 2)
# ---------------------------------------------------------------------------
#
# A trigger is either a key chord (``QKeySequence`` syntax) or a mouse chord:
# ``[Ctrl+][Alt+][Shift+]<atom>`` with an atom from :data:`_MOUSE_ATOMS`.
# Mouse chords live in the same ``"A|B"`` alternatives strings as key chords;
# the key matcher skips them and :meth:`KeyMap.mouse_matches` handles them.
# Mouse *move* is deliberately not bindable — a stream is not a trigger.

_MOUSE_ATOMS = ("LMB", "RMB", "MMB", "WheelUp", "WheelDown")

# Canonical modifier order (mirrors QKeySequence's text order).
_MOD_PREFIXES: tuple[tuple[Qt.KeyboardModifier, str], ...] = (
    (Qt.KeyboardModifier.ControlModifier, "Ctrl"),
    (Qt.KeyboardModifier.AltModifier, "Alt"),
    (Qt.KeyboardModifier.ShiftModifier, "Shift"),
    (Qt.KeyboardModifier.MetaModifier, "Meta"),
)


def is_mouse_trigger(alt: str) -> bool:
    """True when a single trigger alternative is a mouse chord."""
    return alt.rsplit("+", 1)[-1] in _MOUSE_ATOMS


def _chord(atom: str, modifiers: Qt.KeyboardModifier) -> str:
    parts = [name for mod, name in _MOD_PREFIXES if modifiers & mod]
    parts.append(atom)
    return "+".join(parts)


def mouse_button_chord(
    button: Qt.MouseButton, modifiers: Qt.KeyboardModifier
) -> str | None:
    """The canonical chord of a button press, or None for unbindable buttons."""
    atom = {
        Qt.MouseButton.LeftButton: "LMB",
        Qt.MouseButton.RightButton: "RMB",
        Qt.MouseButton.MiddleButton: "MMB",
    }.get(button)
    return _chord(atom, modifiers) if atom else None


def wheel_chord(angle_delta_y: int, modifiers: Qt.KeyboardModifier) -> str | None:
    """The canonical chord of a wheel tick (None for a pure horizontal one)."""
    if angle_delta_y == 0:
        return None
    return _chord("WheelUp" if angle_delta_y > 0 else "WheelDown", modifiers)


# ---------------------------------------------------------------------------
# View gesture actions (Stage 2) — the base map's mouse bindings.
# ---------------------------------------------------------------------------

VIEW_ZOOM_IN = "view.action.zoom_in"
VIEW_ZOOM_OUT = "view.action.zoom_out"
VIEW_PAN = "view.action.pan"
VIEW_CANVAS_MENU = "view.action.canvas_menu"


# ---------------------------------------------------------------------------
# KeyAction
# ---------------------------------------------------------------------------


@dataclass(frozen=True)
class KeyAction:
    """One bindable action.

    ``shortcuts`` is the declared binding — a list of alternatives, a legacy
    ``"Up|W"`` string (``QKeySequence`` syntax — ``"W"``, ``"Ctrl+Shift+S"``,
    ``"Del"``), or ``None`` to inherit the matching generic global default.
    ``final`` (only meaningful on ``global.*`` actions) marks a shortcut no
    tool may claim.
    """

    id: str
    shortcuts: list[str] | str | None = None
    display: str = ""
    final: bool = False

    @property
    def default(self) -> list[str] | str | None:
        return self.shortcuts

    def __json_repr__(self) -> dict[str, object]:
        return {
            "id": self.id,
            "shortcuts": self.shortcuts,
            "display": self.display,
            "final": self.final,
        }

    def clone(self) -> KeyAction:
        return KeyAction(
            id=self.id, shortcuts=self.shortcuts, display=self.display, final=self.final
        )

    @property
    @traced()
    def category(self) -> str:
        return category_of(self.id)

    @property
    @traced()
    def name(self) -> str:
        return name_of(self.id)


GLOBAL_ACTIONS: tuple[KeyAction, ...] = (
    # Not final: rebinding the global Delete is an asserted user behaviour
    # (see TestDynamicRebind) — tools inherit whatever the user picks.
    KeyAction(_global_id("delete"), ["Del"], "Delete"),
    KeyAction(_global_id("cancel"), ["Esc"], "Cancel", final=True),
    KeyAction(_global_id("confirm"), ["Return", "Space"], "Confirm", final=True),
    KeyAction(_global_id("edit"), ["E"], "Edit"),
    # Generic transform ops shared by Select / Place (defaults match the legacy
    # flat table so behaviour is identical until the Stage-2 migration).
    KeyAction(_global_id("rotate_cw"), ["R"], "Rotate CW"),
    KeyAction(_global_id("rotate_ccw"), ["Shift+R"], "Rotate CCW"),
    KeyAction(_global_id("flip"), ["Alt+R"], "Flip"),
    KeyAction(_global_id("redrag"), ["Ctrl+R"], "Re-drag Pins"),
    KeyAction(_global_id("show_shortcuts"), ["?"], "Show Shortcuts", final=True),
    # Example of a protected shortcut a tool may not re-purpose.
    KeyAction("session.action.save_as", ["Ctrl+Shift+S"], "Save As", final=True),
)


# The board canvas's own mouse gestures, declared on the base map so a tool
# layer can *shadow* them for the span of its activation (e.g. an armed
# rotate claiming the wheel).  Unbound chords fall back to the legacy
# handlers, so rebinding these never bricks the canvas.
VIEW_ACTIONS: tuple[KeyAction, ...] = (
    KeyAction(VIEW_ZOOM_IN, ["WheelUp"], "Zoom In"),
    KeyAction(VIEW_ZOOM_OUT, ["WheelDown"], "Zoom Out"),
    KeyAction(VIEW_PAN, ["MMB", "Ctrl+LMB"], "Pan"),
    KeyAction(VIEW_CANVAS_MENU, ["RMB"], "Canvas Menu"),
)


# ---------------------------------------------------------------------------
# Event → QKeySequence matcher
# ---------------------------------------------------------------------------
@traced()
def event_to_sequence(key: int, modifiers: int | Qt.KeyboardModifier) -> QKeySequence:
    """Build the :class:`QKeySequence` a key press represents.

    ``QKeySequence(int(modifiers) | int(key))`` is the canonical construction;
    Qt normalises it so ``Del`` and ``Delete`` compare equal.  *modifiers*
    may be the raw int mask (the seam's ``KeyInput.native_mods``) or the Qt
    flag — runtime-identical (D6).
    """
    mask = modifiers if isinstance(modifiers, int) else int(modifiers.value)
    return QKeySequence(mask | int(key))


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

    @property
    @abstractmethod
    def overrides_store(self) -> dict[str, str]:
        """The live user-overrides dict (shared by reference across layers)."""
        ...

    @abstractmethod
    def register_tool_actions(self, actions: dict[str, str | None]) -> None: ...

    @abstractmethod
    def resolve(self, action_id: str) -> str: ...

    @abstractmethod
    def resolve_action(self, action_id: str) -> KeyAction | None: ...

    @abstractmethod
    def display_of(self, action_id: str) -> str: ...

    @abstractmethod
    def is_final(self, action_id: str) -> bool: ...

    @abstractmethod
    def matches(
        self, key: int, modifiers: int | Qt.KeyboardModifier, action_id: str
    ) -> bool: ...

    @abstractmethod
    def set_override(
        self, action_id: str, shortcuts: list[str] | str
    ) -> KeyAction | None: ...

    @abstractmethod
    def known_actions(self) -> tuple[KeyAction, ...]: ...

    @abstractmethod
    def __json_repr__(self) -> dict[str, dict[str, object]]: ...

    @abstractmethod
    def actions_in(self, category: str) -> tuple[KeyAction, ...]: ...

    @abstractmethod
    def load_defaults(self) -> None: ...

    @abstractmethod
    def load_overrides(self) -> None: ...

    @abstractmethod
    def save_overrides(self) -> None: ...


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
    # User rebinds, keyed by action id.  Kept apart from ``_actions`` so a
    # tool re-registering its declared actions (every activate()) can never
    # clobber a rebind, and so an override may target an action that is not
    # registered *yet*.
    _overrides: dict[str, str] = field(default_factory=dict)

    def __json_repr__(self) -> dict[str, dict[str, object]]:
        json_repr: dict[str, dict[str, object]] = {}
        if self._parent:
            json_repr = self._parent.__json_repr__()
        for action_id, key_action in self._actions.items():
            json_repr[action_id] = key_action.__json_repr__()
        return json_repr

    # -- layering -----------------------------------------------------------

    @property
    @override
    def overrides_store(self) -> dict[str, str]:
        """The live user-overrides dict (shared by reference across layers)."""
        return self._overrides

    @classmethod
    @traced()
    def layer_over(cls, parent: AbstractKeyMap) -> KeyMap:
        """A fresh layer chained onto *parent*, sharing its overrides store.

        The store is shared *by reference* so a user rebind is honoured no
        matter which layer declares the action — a layer's literal declaration
        would otherwise shadow an override living on the base map (``resolve``
        never reaches the parent once it finds its own declaration).
        """
        km = cls(parent)
        km._overrides = parent.overrides_store
        return km

    # -- registration -------------------------------------------------------
    @traced()
    @override
    def register_tool_actions(self, actions: dict[str, str | None]) -> None:
        """Register a tool's declared actions (``key_actions()`` output).

        *actions* maps ``"<category>.action.<name>" -> shortcut | None``.  A
        ``None`` value inherits the matching ``global.action.<name>`` default; a
        literal shortcut overrides for that tool.  A literal shortcut that
        collides with a ``final`` action's trigger is rejected — unless it is
        declared on that final action id itself, which *extends* the action
        with additional triggers (see :meth:`resolve`).
        """
        for action_id, shortcut in actions.items():
            if shortcut is not None:
                owner = self._final_shortcut_owner(_alternatives(shortcut))
                if owner is not None and owner != action_id:
                    raise FinalShortcutError(
                        f"{action_id!r} cannot bind {shortcut!r}: reserved by "
                        f"final action {owner!r}"
                    )
            display = self._inherited_display(action_id)
            self._actions[action_id] = KeyAction(
                id=action_id, shortcuts=shortcut, display=display
            )

    @traced()
    def _final_shortcut_owner(self, alternatives: set[str]) -> str | None:
        """Id of the ``final`` action owning any of *alternatives*, if one does.

        Walks the whole chain.  Final actions cannot be rebound, so their
        *declared* triggers are their effective ones.
        """
        for action in self._actions.values():
            if action.final and alternatives & _alternatives(action.shortcuts):
                return action.id
        parent = self._parent
        if isinstance(parent, KeyMap):
            return parent._final_shortcut_owner(alternatives)
        return None

    @traced()
    def _inherited_display(self, action_id: str) -> str:
        glob = self.resolve_action(_global_id(name_of(action_id)))
        return glob.display if glob is not None else name_of(action_id).title()

    # -- resolution ---------------------------------------------------------

    def _get_action(self, action_id: str) -> KeyAction | None:
        return self._actions.get(action_id)

    @traced()
    @override
    def resolve(self, action_id: str) -> str:
        """The shortcut string that currently fires *action_id* (``""`` if none).

        Order: user override on this map (taken verbatim; ``""`` means
        explicitly unbound) → the action declared on this map (a ``None``
        binding inherits the generic ``global.action.<name>`` through the
        chain) → the parent map.
        """
        if action_id in self._overrides:
            return self._overrides[action_id]
        action = self._get_action(action_id)
        if action is None:
            return self._parent.resolve(action_id) if self._parent else ""
        text = _shortcut_text(action.shortcuts)
        if text is not None:
            # A layer declaration on a *final* action id extends the action
            # with additional triggers (union, parent first) — the protected
            # base trigger can never be shadowed away, only added to.
            parent = self._parent
            if parent is not None and not action.final and parent.is_final(action_id):
                base = parent.resolve(action_id)
                extra = [
                    a for a in text.split("|") if a and a not in _alternatives(base)
                ]
                return "|".join([base, *extra]) if base else text
            return text
        gid = _global_id(name_of(action_id))
        if gid == action_id:
            return ""  # an unbound global action
        # Restart at *self*, not the parent: an override on the generic
        # global id may live on this map (rebinds land on the leaf).
        return self.resolve(gid)

    @traced()
    def resolve_display(self, action_id: str) -> str:
        """Human-facing binding text: ``|`` alternatives shown as ``"Up / W"``."""
        return " / ".join(a for a in self.resolve(action_id).split("|") if a)

    @traced()
    def is_final(self, action_id: str) -> bool:
        """True when *action_id* is declared ``final`` anywhere in the chain.

        Chain-aware on purpose: a layer that extends a final action registers
        a plain (non-final) copy, which must not launder the protection away.
        """
        action = self._get_action(action_id)
        if action is not None and action.final:
            return True
        parent = self._parent
        return parent.is_final(action_id) if parent is not None else False

    @traced()
    def _non_final_shortcut(self, shortcut: list[str]) -> list[QKeySequence]:
        return []

    @traced()
    def matches(
        self, key: int, modifiers: int | Qt.KeyboardModifier, action_id: str
    ) -> bool:
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
        # Mouse chords in the list belong to :meth:`mouse_matches` — skipped.
        return any(
            self._seq_matches(key, modifiers, alt)
            for alt in shortcut.split("|")
            if alt and not is_mouse_trigger(alt)
        )

    @staticmethod
    @traced()
    def _seq_matches(
        key: int, modifiers: int | Qt.KeyboardModifier, shortcut: str
    ) -> bool:
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
        mods = (
            Qt.KeyboardModifier(modifiers) if isinstance(modifiers, int) else modifiers
        )
        target = QKeySequence(shortcut)
        if event_to_sequence(key, mods) == target:
            return True
        if (
            mods & Qt.KeyboardModifier.ShiftModifier
            and _is_symbol_key(key)
            and not _has_shift(target)
        ):
            without_shift = mods & ~Qt.KeyboardModifier.ShiftModifier
            if event_to_sequence(key, without_shift) == target:
                return True
        return False

    # -- overrides / persistence -------------------------------------------

    @traced()
    @override
    def set_override(
        self, action_id: str, shortcuts: list[str] | str
    ) -> KeyAction | None:
        """Set a user override for *action_id* (does not persist by itself).

        The override sticks even when the action is not registered yet (a
        tool registers its actions on activate) and survives re-registration.
        Returns the declared action if one is known anywhere in the chain.

        A ``final`` action cannot be rebound, and no override may claim a
        ``final`` action's trigger for a different action — both raise
        :class:`FinalShortcutError`.
        """
        if self.is_final(action_id):
            raise FinalShortcutError(f"{action_id!r} is final and cannot be rebound")
        owner = self._final_shortcut_owner(_alternatives(shortcuts))
        if owner is not None:
            raise FinalShortcutError(
                f"{action_id!r} cannot bind {_shortcut_text(shortcuts)!r}: "
                f"reserved by final action {owner!r}"
            )
        self._overrides[action_id] = _shortcut_text(shortcuts) or ""
        return self.resolve_action(action_id)

    @traced()
    def declare(self, actions: tuple[KeyAction, ...]) -> None:
        """Declare fully-specified actions (display / ``final`` kept) here.

        Unlike :meth:`register_tool_actions` this takes ready
        :class:`KeyAction` objects — used for the built-in view gesture set.
        """
        for action in actions:
            self._actions[action.id] = action.clone()

    @traced()
    def lookup(self, key: int, modifiers: Qt.KeyboardModifier) -> tuple[str, ...]:
        """Action ids whose *resolved* triggers match the key press.

        The reverse of :meth:`matches`: walks the chain leaf-first (declaration
        order within a map), so a tool layer's claims come before the base
        map's — that ordering *is* the shadowing rule for dispatch.  Several
        actions may share a trigger (e.g. an armed step and a tool op); the
        caller's state gates pick the live one.
        """
        return self._lookup(lambda action_id: self.matches(key, modifiers, action_id))

    @traced()
    def mouse_matches(self, chord: str, action_id: str) -> bool:
        """True when the canonical mouse *chord* is among the resolved triggers.

        The mouse twin of :meth:`matches` — see :func:`mouse_button_chord` /
        :func:`wheel_chord` for building the chord from a Qt event.
        """
        return chord in _alternatives(self.resolve(action_id))

    @traced()
    def mouse_lookup(self, chord: str) -> tuple[str, ...]:
        """Action ids bound to the mouse *chord*, leaf-first (shadowing order)."""
        return self._lookup(lambda action_id: self.mouse_matches(chord, action_id))

    def _lookup(self, pred: Callable[[str], bool]) -> tuple[str, ...]:
        seen: set[str] = set()
        ids: list[str] = []
        node: AbstractKeyMap | None = self
        while isinstance(node, KeyMap):
            for action_id in node._actions:
                if action_id in seen:
                    continue
                seen.add(action_id)
                if pred(action_id):
                    ids.append(action_id)
            node = node._parent
        return tuple(ids)

    @traced()
    def known_actions(self) -> tuple[KeyAction, ...]:
        """Every declared action, for UI / introspection."""
        return tuple(self._actions.values())

    @traced()
    def actions_in(self, category: str) -> tuple[KeyAction, ...]:
        """Every declared action in *category* (text before the first dot)."""
        return tuple(a for a in self._actions.values() if a.category == category)

    @traced()
    def display_of(self, action_id: str) -> str:
        """The human label for *action_id* (declared display, else the name)."""
        action = self._get_action(action_id)
        if action is not None and action.display:
            return action.display
        return name_of(action_id).replace("_", " ").title()

    # -- disk ---------------------------------------------------------------

    @traced()
    def load_defaults(self) -> None:
        """Register the shipped ``defaults:`` tree, then the user file's.

        The shipped ``BUILTIN_DEFAULT_KEYBINDINGS_FILE`` (packaged with the
        application — same pattern as ``BUILTIN_DEFAULT_TOOLBARS_FILE``) is
        the baseline every :class:`KeyMap` carries from construction
        (:class:`~protoboard.gui.tool_host.ToolHost` calls this on init), so
        a tool's shortcuts resolve headless / in tests with no on-disk config.
        The user's ``KEYBINDINGS_V2_FILE`` ``defaults:`` section (if present)
        is declared on top — a later user override
        (:meth:`load_overrides`, loaded after this) still wins either way.
        Missing file / missing section / corrupt file are no-ops.
        """
        self._load_defaults_tree(BUILTIN_DEFAULT_KEYBINDINGS_FILE)
        self._load_defaults_tree(KEYBINDINGS_V2_FILE)

    def _load_defaults_tree(self, path: object) -> None:
        """Register the ``defaults:`` section of the YAML file at *path*."""
        from pathlib import Path

        assert isinstance(path, Path)
        if not path.exists():
            return
        try:
            from ruamel.yaml import YAML  # type: ignore[import-untyped,unused-ignore]

            yaml = YAML()
            raw = yaml.load(path)
            if not isinstance(raw, dict):
                return
            defaults = raw.get("defaults")
            if not isinstance(defaults, dict):
                return
            for category, entries in defaults.items():
                if not isinstance(category, str) or not isinstance(entries, dict):
                    continue
                actions: dict[str, str | None] = {}
                for name, value in entries.items():
                    if isinstance(name, str) and isinstance(value, str):
                        actions[f"{category}.{name}"] = value
                if actions:
                    self.register_tool_actions(actions)
        except Exception:  # noqa: BLE001 — corrupt file → defaults
            return

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
        if not KEYBINDINGS_V2_FILE.exists():
            return
        try:
            from ruamel.yaml import YAML  # type: ignore[import-untyped,unused-ignore]

            yaml = YAML()
            raw = yaml.load(KEYBINDINGS_V2_FILE)
            if not isinstance(raw, dict):
                return
            # New shape nests overrides under `overrides:` (alongside
            # `defaults:`); an old flat file has no such key, so the whole
            # dict IS the override tree (backward compat).
            tree = raw.get("overrides", raw)
            for category, entries in tree.items():
                if not isinstance(category, str) or not isinstance(entries, dict):
                    continue
                for name, value in entries.items():
                    if not isinstance(name, str) or not isinstance(value, str):
                        continue  # null / non-string → inherit (skip)
                    self._overrides[f"{category}.{name}"] = value
        except Exception:  # noqa: BLE001 — corrupt file → defaults
            return

    @traced()
    def save_overrides(self) -> None:
        """Persist user overrides to the namespaced tree YAML, under ``overrides:``.

        Only entries that differ from the action's declared default are written,
        so the file stays minimal and future default changes propagate.  Any
        existing ``defaults:`` section is read back and re-emitted unchanged.
        """
        from ruamel.yaml import YAML  # type: ignore[import-untyped,unused-ignore]

        tree: dict[str, dict[str, str]] = {}
        for action_id, shortcut in self._overrides.items():
            action = self.resolve_action(action_id)
            if action is not None and _shortcut_text(action.shortcuts) == shortcut:
                continue  # equals declared default → don't persist
            category, _, name = action_id.partition(".")
            if not name:
                continue
            tree.setdefault(category, {})[name] = shortcut

        existing_defaults = None
        if KEYBINDINGS_V2_FILE.exists():
            try:
                raw = YAML().load(KEYBINDINGS_V2_FILE)
                if isinstance(raw, dict):
                    existing_defaults = raw.get("defaults")
            except Exception:  # noqa: BLE001 — corrupt file → don't preserve
                existing_defaults = None

        out: dict[str, object] = {}
        if existing_defaults:
            out["defaults"] = existing_defaults
        out["overrides"] = tree

        CONFIG_DIR.mkdir(parents=True, exist_ok=True)
        yaml = YAML()
        yaml.default_flow_style = False
        with KEYBINDINGS_V2_FILE.open("w", encoding="utf-8") as fh:
            fh.write("# Protoboard Designer — namespaced keyboard shortcuts\n")
            fh.write("# Grouped by category. null = inherit the global default.\n")
            yaml.dump(out, fh)

    @override
    def resolve_action(self, action_id: str) -> KeyAction | None:
        """The declared :class:`KeyAction`, searching up the map chain."""
        action = self._get_action(action_id)
        if action is not None:
            return action
        parent = self._parent
        return parent.resolve_action(action_id) if parent else None


class GlobalKeyMap(KeyMap):
    """Root of the map chain: the built-in generic ``global.action.*`` set.

    A tool action id that reaches this map unregistered falls back to the
    generic global default sharing its bare name — that is the two-level
    inherit rule's last stop.
    """

    @classmethod
    def with_globals(cls) -> KeyMap:
        """A fresh map carrying only the built-in global actions."""
        km = cls()
        for action in GLOBAL_ACTIONS:
            km._actions[action.id] = action
        return km

    @override
    def resolve(self, action_id: str) -> str:
        if action_id in self._overrides or action_id in self._actions:
            return super().resolve(action_id)
        global_id = _global_id(name_of(action_id))
        if global_id == action_id:
            return ""  # unknown global action = unbound
        return self.resolve(global_id)  # tool action → its generic default
