# SPDX-License-Identifier: MIT
"""``InteractionContext`` — RMB as context publication, no port (qt-ports §2.4).

Pure data: *what can be done here, now*.  The view surface answers
``get_context_at`` as a plain query and publishes it via ``notify_context``;
the consumer (MainWindow, a test, a CLI) renders the choice UI and executes
the picked ``action_id`` through the existing ``trigger_tool_action`` path.
No blocking call ever crosses the seam.
"""

from __future__ import annotations

import uuid
from dataclasses import dataclass
from typing import TYPE_CHECKING

if TYPE_CHECKING:
    from protoboard.geometry import Point


@dataclass(frozen=True, slots=True)
class ActionOption:
    """One action executable in this context (presentation hints included)."""

    action_id: str
    """Executes via the existing ``trigger_tool_action`` dispatch."""
    label: str
    enabled: bool = True
    shortcut_hint: str = ""
    group: str = ""
    """Semantic grouping (the consumer decides how to render it)."""


@dataclass(frozen=True, slots=True)
class InteractionContext:
    """A snapshot of the interaction possibilities at one board position."""

    scene_pos: Point
    view_pos: Point
    hit: tuple[str, uuid.UUID] | None
    """(kind, id) of the clicked element, or None for bare canvas."""
    selection: tuple[uuid.UUID, ...]
    actions: tuple[ActionOption, ...]
    kind: str = "canvas"
    """``"canvas" | "selection" | "wire" | …`` — the context flavour."""
