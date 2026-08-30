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

The stack *logic* is Qt-free and lives in
:class:`protoboard.controller.highlight_stack.HighlightStackBase`; this module
adds only the ``changed`` Qt signal so ``BoardView`` can react.  A headless
session uses the bare base directly (the signal is simply absent).

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
from protoboard.controller.highlight_stack import HighlightStackBase

# ---------------------------------------------------------------------------
# HighlightStack
# ---------------------------------------------------------------------------


class HighlightStack(QObject, HighlightStackBase):
    """The Qt-visible highlight stack: :class:`HighlightStackBase` + a signal.

    All push/pop/clear/query behaviour comes from the Qt-free base; this class
    contributes the ``changed`` signal (emitted by the base's ``_changed``
    hook) so ``BoardView`` can re-apply opacity dimming.  The active entry
    returned by ``active()`` is the topmost entry of the highest non-empty tier
    (persistent > transient > auto).
    """

    changed = Signal()  # emitted whenever the active entry changes

    def __init__(self, parent: QObject | None = None) -> None:
        QObject.__init__(self, parent)
        HighlightStackBase.__init__(self)

    def _changed(self) -> None:
        self.changed.emit()
