# SPDX-License-Identifier: MIT
"""Drag-time conflict-resolution preferences.

Effective preferences for a drag operation come from two sources:

1. Persistent app settings (``AppSettings.drag_*``) — set via
   PreferencesDialog, persisted in ``~/.config/protoboard/app_settings.yaml``.
2. Per-session overrides — set by the user checking
   "Don't ask again for this session" in a soft-conflict dialog.  Reset
   on app restart.

This module wraps both behind :class:`DragPreferences` (the merged
read-only view consumed by the drag pipeline) and :class:`DragSessionOverrides`
(the mutable per-session cache owned by ``SelectTool``).
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import TYPE_CHECKING

if TYPE_CHECKING:
    from protoboard.gui.app_settings import AppSettings


__all__ = [
    "DRAG_CONFLICT_VALUES",
    "DragPreferences",
    "DragSessionOverrides",
]


#: All allowed values for any drag-conflict preference field.
DRAG_CONFLICT_VALUES: frozenset[str] = frozenset({
    "auto-remove",
    "ask-remove",
    "auto-reroute",
    "ask-reroute",
    "block",
})


@dataclass(frozen=True)
class DragPreferences:
    """Effective per-drag preferences (app settings + session overrides).

    Every field is one of :data:`DRAG_CONFLICT_VALUES`.  Consumers
    branch on the value to decide whether to drop silently, open a
    dialog, dispatch a reroute, or refuse the commit.
    """

    free_wire: str
    stationary_group_wire: str
    cross_group_disconnect: str
    default_lock_internal_wires: bool

    @classmethod
    def from_app_settings(cls, s: "AppSettings") -> "DragPreferences":
        return cls(
            free_wire=s.drag_free_wire_conflict,
            stationary_group_wire=s.drag_stationary_group_wire_conflict,
            cross_group_disconnect=s.drag_cross_group_disconnect,
            default_lock_internal_wires=s.drag_default_lock_internal_wires,
        )


@dataclass
class DragSessionOverrides:
    """Per-session "Don't ask again for this session" cache.

    Stored on :class:`SelectTool` for the app's lifetime.  When a user
    picks an action in an ask-* dialog with the opt-out checkbox set,
    the chosen action lands here and short-circuits future dialogs of
    the same category until the app restarts.
    """

    free_wire: str | None = None
    stationary_group_wire: str | None = None
    cross_group_disconnect: str | None = None

    def effective(self, base: DragPreferences) -> DragPreferences:
        """Return base with any session overrides applied.

        Each override either replaces the base preference (if set) or
        leaves it untouched.  Invariant: returned preferences carry
        only resolved/non-ask values when an override has been recorded
        for that category — the override IS the user's chosen action,
        not a meta-preference.
        """
        return DragPreferences(
            free_wire=self.free_wire or base.free_wire,
            stationary_group_wire=(
                self.stationary_group_wire or base.stationary_group_wire
            ),
            cross_group_disconnect=(
                self.cross_group_disconnect or base.cross_group_disconnect
            ),
            default_lock_internal_wires=base.default_lock_internal_wires,
        )

    def clear(self) -> None:
        """Drop every recorded override (used by app reset / project switch)."""
        self.free_wire = None
        self.stationary_group_wire = None
        self.cross_group_disconnect = None
