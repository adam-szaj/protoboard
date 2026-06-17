# SPDX-License-Identifier: MIT
"""Selection layer — the SelectTool-as-a-service rewrite.

``Selectable`` is a thin GUI-side protocol over the domain board entities
(component / wire / group / pin / wire-vertex); ``SelectionService`` owns
selection state (wrapping the pure :mod:`protoboard.gui.selection_fsm`)
plus the active-pick API other tools and commands consume.
"""

from __future__ import annotations

from protoboard.gui.selection.adapters import (
    ComponentSelectable,
    GroupSelectable,
    PinSelectable,
    WireSelectable,
    WireVertexSelectable,
)
from protoboard.gui.selection.hit_test import (
    HitTester,
    SelectionFilters,
)
from protoboard.gui.selection.selectable import (
    PickKind,
    Selectable,
    SelectableKey,
    Stop,
)
from protoboard.gui.selection.service import PickSession, SelectionService

__all__ = [
    "ComponentSelectable",
    "GroupSelectable",
    "HitTester",
    "PickKind",
    "PickSession",
    "PinSelectable",
    "Selectable",
    "SelectableKey",
    "SelectionFilters",
    "SelectionService",
    "Stop",
    "WireSelectable",
    "WireVertexSelectable",
]
