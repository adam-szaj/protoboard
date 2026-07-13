# SPDX-License-Identifier: MIT
"""Selection layer — the SelectTool-as-a-service rewrite.

``Selectable`` is a thin GUI-side protocol over the domain board entities
(component / wire / group / pin / wire-vertex); ``SelectionService`` owns
selection state (wrapping the pure :mod:`protoboard.controller.selection_fsm`)
plus the active-pick API other tools and commands consume.
"""

from __future__ import annotations

from protoboard.controller.selection.adapters import (
    ComponentSelectable,
    GroupSelectable,
    PinSelectable,
    PointSelectable,
    WireSelectable,
    WireVertexSelectable,
)
from protoboard.controller.selection.hit_test import (
    HitTester,
    SelectionFilters,
    WireEditCycle,
)
from protoboard.controller.selection.mouse_input import (
    Intent,
    Mods,
    MouseInputController,
)
from protoboard.controller.selection.point_pick_controller import PointPickController
from protoboard.controller.selection.queries import (
    RelationSource,
    SessionConnQuery,
    SessionNetSource,
    relation_correlated,
    source_net_ids,
)
from protoboard.controller.selection.selectable import (
    PickKind,
    Selectable,
    SelectableKey,
    Stop,
)
from protoboard.controller.selection.service import PickSession, SelectionService

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
