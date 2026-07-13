# SPDX-License-Identifier: MIT
"""HighlightStack — persistent highlight state independent of active tool.

Architecture
------------
- HighlightEntry: one entry on the stack (highlighted items + visual profile).
- HighlightStack: a QObject that emits `changed` whenever the active entry
  changes.  BoardView subscribes to `changed` and calls _apply_highlight_opacity.
- Transient entries (pushed by tools like RouteTool) sit on top of the stack
  but are popped automatically when the tool deactivates, restoring the
  previous user-defined highlight.
- Auto entries (pushed by SelectTool to reflect a relation class) are the
  weakest tier and only become active when neither persistent nor transient
  entries exist.  They are addressed by source_id so a fresh auto push from
  the same source replaces the previous one.

Precedence (top of stack wins within a tier):
    persistent > transient > auto
- The stack is held by EditorSession so all GUI components share it.

Profiles
--------
PROFILE_DIMMED_1: light dimming — use during drag/rotate/route where context
    matters but focus is on the highlighted items.
PROFILE_DIMMED_2: strong dimming — use for explicit "highlight selection"
    where non-highlighted items should fade into background.
"""

from __future__ import annotations

from PySide6.QtCore import QObject, Signal

# HighlightEntry + the pre-defined profiles are pure data (Qt-free) and live
# in controller/highlight_entry.py; re-exported here for existing GUI
# importers (single source of truth per that module's docstring).
from protoboard.controller.highlight_entry import (
    PROFILE_DIMMED_1 as PROFILE_DIMMED_1,
)
from protoboard.controller.highlight_entry import (
    PROFILE_DIMMED_2 as PROFILE_DIMMED_2,
)
from protoboard.controller.highlight_entry import (
    PROFILE_ROUTE_ACTIVE as PROFILE_ROUTE_ACTIVE,
)
from protoboard.controller.highlight_entry import (
    HighlightEntry as HighlightEntry,
)


# ---------------------------------------------------------------------------
# HighlightStack
# ---------------------------------------------------------------------------

class HighlightStack(QObject):
    """Stack of highlight entries with three precedence tiers.

    The active entry returned by ``active()`` is the topmost entry of the
    highest non-empty tier (persistent > transient > auto).  Within a tier
    the topmost entry wins.
    """

    changed = Signal()  # emitted whenever the active entry changes

    def __init__(self, parent: QObject | None = None) -> None:
        super().__init__(parent)
        self._stack: list[HighlightEntry] = []

    # ------------------------------------------------------------------
    # Public API
    # ------------------------------------------------------------------

    def active(self) -> HighlightEntry | None:
        """Return the active entry under the precedence rule, or None."""
        # Scan from top of stack within each tier.
        for predicate in (
            lambda e: not e.transient and not e.auto,  # persistent
            lambda e: e.transient,                      # transient
            lambda e: e.auto,                           # auto
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
        self.changed.emit()

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
        self.changed.emit()

    def push_auto(self, entry: HighlightEntry, *, source_id: str) -> None:
        """Push (or replace) an auto entry keyed by ``source_id``.

        Auto entries are the weakest tier — they only become active when no
        persistent or transient entry exists.  At most one auto entry per
        source_id; pushing again from the same source replaces the previous.
        """
        # Drop existing auto entry from this source.
        self._stack = [
            e for e in self._stack
            if not (e.auto and e.source_id == source_id)
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
        self.changed.emit()

    def clear_auto(self, source_id: str) -> None:
        """Remove the auto entry pushed by ``source_id`` (if any)."""
        before = len(self._stack)
        self._stack = [
            e for e in self._stack
            if not (e.auto and e.source_id == source_id)
        ]
        if len(self._stack) != before:
            self.changed.emit()

    def pop_transient(self) -> None:
        """Remove the topmost transient entry (tool deactivation)."""
        for i in range(len(self._stack) - 1, -1, -1):
            if self._stack[i].transient:
                self._stack.pop(i)
                self.changed.emit()
                return

    def pop(self) -> None:
        """Remove the topmost persistent entry (user undo highlight)."""
        for i in range(len(self._stack) - 1, -1, -1):
            e = self._stack[i]
            if not e.transient and not e.auto:
                self._stack.pop(i)
                self.changed.emit()
                return

    def clear(self) -> None:
        """Remove all persistent entries (keep transient and auto ones)."""
        had_persistent = any(not e.transient and not e.auto for e in self._stack)
        self._stack = [e for e in self._stack if e.transient or e.auto]
        if had_persistent:
            self.changed.emit()

    def clear_all(self) -> None:
        """Remove all entries including transient and auto ones."""
        if self._stack:
            self._stack.clear()
            self.changed.emit()

    def is_empty(self) -> bool:
        return len(self._stack) == 0

    def has_persistent(self) -> bool:
        return any(not e.transient and not e.auto for e in self._stack)
