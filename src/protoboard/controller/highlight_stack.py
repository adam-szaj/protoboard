# SPDX-License-Identifier: MIT
"""``HighlightStackBase`` — Qt-free highlight-stack logic.

The stack holds :class:`~protoboard.controller.highlight_entry.HighlightEntry`
values in three precedence tiers (persistent > transient > auto) and exposes
the pure push/pop/clear/query operations.  It carries **no Qt dependency**: the
"the active entry changed" side effect is a :meth:`_changed` hook that the base
leaves a no-op.  The GUI subclass
(:class:`protoboard.gui.highlight_state.HighlightStack`, a ``QObject``)
overrides ``_changed`` to emit its ``changed`` signal so ``BoardView`` can
re-apply opacity dimming; a headless session shares the same behaviour with the
signal simply doing nothing.

Precedence (top of stack wins within a tier):
    persistent > transient > auto

Tiers
-----
- **persistent** — user actions / explicit highlights (``push`` / ``pop`` /
  ``clear``).
- **transient** — tool activations (``push_transient`` / ``pop_transient``);
  removed when the tool deactivates, restoring the previous highlight.
- **auto** — the weakest tier, addressed by ``source_id`` (``push_auto`` /
  ``clear_auto``); at most one entry per source, and only active when no
  persistent or transient entry exists.
"""

from __future__ import annotations

from protoboard.controller.highlight_entry import HighlightEntry


class HighlightStackBase:
    """Pure highlight-stack state — see the module docstring.

    Subclass and override :meth:`_changed` to react when the active entry may
    have changed (the GUI subclass emits a Qt signal).
    """

    def __init__(self) -> None:
        self._stack: list[HighlightEntry] = []

    # ------------------------------------------------------------------
    # Side-effect hook
    # ------------------------------------------------------------------

    def _changed(self) -> None:
        """Called after any mutation that may change the active entry.

        No-op in the Qt-free base; the GUI subclass emits its ``changed``
        signal here.
        """

    # ------------------------------------------------------------------
    # Public API
    # ------------------------------------------------------------------

    def active(self) -> HighlightEntry | None:
        """Return the active entry under the precedence rule, or None."""
        # Scan from top of stack within each tier.
        for predicate in (
            lambda e: not e.transient and not e.auto,  # persistent
            lambda e: e.transient,  # transient
            lambda e: e.auto,  # auto
        ):
            for entry in reversed(self._stack):
                if predicate(entry):
                    return entry
        return None

    def push(self, entry: HighlightEntry) -> None:
        """Push a persistent entry (user action / explicit highlight)."""
        new = HighlightEntry(
            comp_ids=entry.comp_ids,
            wire_ids=entry.wire_ids,
            net_id=entry.net_id,
            profile=entry.profile,
            label=entry.label,
            transient=False,  # force non-transient
            auto=False,
        )
        self._stack.append(new)
        self._changed()

    def push_transient(self, entry: HighlightEntry) -> None:
        """Push a transient entry (tool activation).

        Transient entries are removed by pop_transient() when the tool
        deactivates.  They take precedence over auto entries but lose to
        persistent ones.
        """
        new = HighlightEntry(
            comp_ids=entry.comp_ids,
            wire_ids=entry.wire_ids,
            net_id=entry.net_id,
            profile=entry.profile,
            label=entry.label,
            transient=True,
            auto=False,
        )
        self._stack.append(new)
        self._changed()

    def push_auto(self, entry: HighlightEntry, *, source_id: str) -> None:
        """Push (or replace) an auto entry keyed by ``source_id``.

        Auto entries are the weakest tier — they only become active when no
        persistent or transient entry exists.  At most one auto entry per
        source_id; pushing again from the same source replaces the previous.
        """
        # Drop existing auto entry from this source.
        self._stack = [
            e for e in self._stack if not (e.auto and e.source_id == source_id)
        ]
        new = HighlightEntry(
            comp_ids=entry.comp_ids,
            wire_ids=entry.wire_ids,
            net_id=entry.net_id,
            profile=entry.profile,
            label=entry.label,
            transient=False,
            auto=True,
            source_id=source_id,
        )
        self._stack.append(new)
        self._changed()

    def clear_auto(self, source_id: str) -> None:
        """Remove the auto entry pushed by ``source_id`` (if any)."""
        before = len(self._stack)
        self._stack = [
            e for e in self._stack if not (e.auto and e.source_id == source_id)
        ]
        if len(self._stack) != before:
            self._changed()

    def pop_transient(self) -> None:
        """Remove the topmost transient entry (tool deactivation)."""
        for i in range(len(self._stack) - 1, -1, -1):
            if self._stack[i].transient:
                self._stack.pop(i)
                self._changed()
                return

    def pop(self) -> None:
        """Remove the topmost persistent entry (user undo highlight)."""
        for i in range(len(self._stack) - 1, -1, -1):
            e = self._stack[i]
            if not e.transient and not e.auto:
                self._stack.pop(i)
                self._changed()
                return

    def clear(self) -> None:
        """Remove all persistent entries (keep transient and auto ones)."""
        had_persistent = any(not e.transient and not e.auto for e in self._stack)
        self._stack = [e for e in self._stack if e.transient or e.auto]
        if had_persistent:
            self._changed()

    def clear_all(self) -> None:
        """Remove all entries including transient and auto ones."""
        if self._stack:
            self._stack.clear()
            self._changed()

    def is_empty(self) -> bool:
        return len(self._stack) == 0

    def has_persistent(self) -> bool:
        return any(not e.transient and not e.auto for e in self._stack)
