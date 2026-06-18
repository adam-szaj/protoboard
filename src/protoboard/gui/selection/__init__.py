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
    PointSelectable,
    WireSelectable,
    WireVertexSelectable,
)
from protoboard.gui.selection.hit_test import (
    HitTester,
    SelectionFilters,
    WireEditCycle,
)
from protoboard.gui.selection.mouse_input import (
    Intent,
    Mods,
    MouseInputController,
)
from protoboard.gui.selection.point_pick_controller import PointPickController
from protoboard.gui.selection.queries import (
    RelationSource,
    SessionConnQuery,
    SessionNetSource,
    relation_correlated,
    source_net_ids,
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
    "Intent",
    "Mods",
    "MouseInputController",
    "PickKind",
    "PickSession",
    "PinSelectable",
    "PointPickController",
    "PointSelectable",
    "RelationSource",
    "Selectable",
    "SelectableKey",
    "SelectionFilters",
    "SelectionService",
    "SessionConnQuery",
    "SessionNetSource",
    "Stop",
    "WireEditCycle",
    "WireSelectable",
    "WireVertexSelectable",
    "relation_correlated",
    "source_net_ids",
]
