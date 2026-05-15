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
- The stack is held by EditorSession so all GUI components share it.

Profiles
--------
PROFILE_DIMMED_1: light dimming — use during drag/rotate/route where context
    matters but focus is on the highlighted items.
PROFILE_DIMMED_2: strong dimming — use for explicit "highlight selection"
    where non-highlighted items should fade into background.
"""

from __future__ import annotations

import uuid
from dataclasses import dataclass, field

from PySide6.QtCore import QObject, Signal

from protoboard.gui.interaction_mode import HighlightProfile


# ---------------------------------------------------------------------------
# Pre-defined profiles
# ---------------------------------------------------------------------------

PROFILE_DIMMED_1 = HighlightProfile(
    pad_marker_rgba=(255, 220, 0, 160),
    pad_marker_radius=0.28,
    unconnected_rgba=(255, 140, 0, 180),
    dim_components=0.45,
    dim_wires=0.45,
)

PROFILE_DIMMED_2 = HighlightProfile(
    pad_marker_rgba=(255, 220, 0, 160),
    pad_marker_radius=0.28,
    unconnected_rgba=(255, 140, 0, 180),
    dim_components=0.12,
    dim_wires=0.12,
)


# ---------------------------------------------------------------------------
# HighlightEntry
# ---------------------------------------------------------------------------

@dataclass
class HighlightEntry:
    """One entry on the highlight stack."""

    # UUIDs of highlighted components and/or wires.
    comp_ids: frozenset[uuid.UUID] = field(default_factory=frozenset)
    wire_ids: frozenset[uuid.UUID] = field(default_factory=frozenset)
    # Net ID used for opacity calculation (which comps/wires belong to the net).
    net_id: uuid.UUID | None = None
    # Visual parameters.
    profile: HighlightProfile = field(default_factory=HighlightProfile)
    # Human-readable label for debugging / future UI.
    label: str = ""
    # Transient entries are pushed by tools and popped on tool deactivation.
    # They do NOT persist across tool switches.
    transient: bool = False


# ---------------------------------------------------------------------------
# HighlightStack
# ---------------------------------------------------------------------------

class HighlightStack(QObject):
    """Persistent stack of highlight entries.

    The topmost entry (``active()``) determines what BoardView dims.
    Transient entries (from tools) sit on top of persistent user entries.
    """

    changed = Signal()  # emitted whenever the active entry changes

    def __init__(self, parent: QObject | None = None) -> None:
        super().__init__(parent)
        self._stack: list[HighlightEntry] = []

    # ------------------------------------------------------------------
    # Public API
    # ------------------------------------------------------------------

    def active(self) -> HighlightEntry | None:
        """Return the topmost entry, or None if the stack is empty."""
        return self._stack[-1] if self._stack else None

    def push(self, entry: HighlightEntry) -> None:
        """Push a persistent entry (user action / explicit highlight)."""
        entry = HighlightEntry(
            comp_ids=entry.comp_ids,
            wire_ids=entry.wire_ids,
            net_id=entry.net_id,
            profile=entry.profile,
            label=entry.label,
            transient=False,  # force non-transient
        )
        self._stack.append(entry)
        self.changed.emit()

    def push_transient(self, entry: HighlightEntry) -> None:
        """Push a transient entry (tool activation).

        Transient entries are removed by pop_transient() when the tool
        deactivates. They always sit above any persistent entries.
        """
        entry = HighlightEntry(
            comp_ids=entry.comp_ids,
            wire_ids=entry.wire_ids,
            net_id=entry.net_id,
            profile=entry.profile,
            label=entry.label,
            transient=True,
        )
        self._stack.append(entry)
        self.changed.emit()

    def pop_transient(self) -> None:
        """Remove the topmost transient entry (tool deactivation)."""
        if self._stack and self._stack[-1].transient:
            self._stack.pop()
            self.changed.emit()

    def pop(self) -> None:
        """Remove the topmost persistent entry (user undo highlight)."""
        # Only pop non-transient entries via this method.
        for i in range(len(self._stack) - 1, -1, -1):
            if not self._stack[i].transient:
                self._stack.pop(i)
                self.changed.emit()
                return

    def clear(self) -> None:
        """Remove all persistent entries (keep transient ones on top)."""
        had_persistent = any(not e.transient for e in self._stack)
        self._stack = [e for e in self._stack if e.transient]
        if had_persistent:
            self.changed.emit()

    def clear_all(self) -> None:
        """Remove all entries including transient ones."""
        if self._stack:
            self._stack.clear()
            self.changed.emit()

    def is_empty(self) -> bool:
        return len(self._stack) == 0

    def has_persistent(self) -> bool:
        return any(not e.transient for e in self._stack)
