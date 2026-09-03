# SPDX-License-Identifier: MIT
"""Project the connectivity graph onto wires: ``wire.id → net.id``."""

from __future__ import annotations

import uuid
from typing import TYPE_CHECKING

if TYPE_CHECKING:
    from protoboard.domain.connectivity.incremental import ConnectivityGraph
    from protoboard.domain.layout.layout import Layout
    from protoboard.domain.schematic.schematic import Schematic


def build_wire_net_map(
    schematic: Schematic,
    layout: Layout,
    graph: ConnectivityGraph,
) -> dict[uuid.UUID, uuid.UUID]:
    """Return a mapping wire.id → net.id using *graph*.

    Uses the connectivity graph so strips, bare-wire chains, and
    plated-through holes are all handled correctly — not just wires whose
    endpoints happen to land directly on a component pin.

    GUI callers should prefer ``EditorSession.cached_wire_net_map()``, which
    memoises this projection per layout change.
    """
    if not schematic.nets or not layout.wires:
        return {}

    # canonical_key → net.id  (one net per electric set that has named pins)
    canonical_to_net: dict[object, uuid.UUID] = {}
    for ref, net in schematic.pin_net_map().items():
        ck = graph.node_for_pin(ref)
        if ck is not None:
            canonical_to_net[ck] = net.id

    result: dict[uuid.UUID, uuid.UUID] = {}
    for wire in layout.wires:
        # Insulated wires only make electrical contact at their two endpoints;
        # bare wires contact every pad they pass through.
        contact_pts = (
            ([wire.path[0], wire.path[-1]] if len(wire.path) >= 2 else wire.path)
            if wire.insulated
            else wire.path
        )
        for pt in contact_pts:
            ck = graph.node_for_pad(pt, wire.side)
            if ck is not None:
                net_id = canonical_to_net.get(ck)
                if net_id is not None:
                    result[wire.id] = net_id
                    break
    return result
