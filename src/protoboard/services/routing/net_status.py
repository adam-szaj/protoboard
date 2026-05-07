# SPDX-License-Identifier: MIT
"""Net routing-completeness helpers."""

from __future__ import annotations

import uuid

from protoboard.domain.connectivity.graph import ConnectivityGraph
from protoboard.domain.schematic.net import Net
from protoboard.domain.schematic.schematic import Schematic


def net_is_complete(net: Net, graph: ConnectivityGraph) -> bool:
    """Return True if all placed pins of *net* are in the same connected component.

    A net with fewer than 2 placed pins is considered complete (nothing to route).
    """
    nodes = [graph.node_for_pin(ref) for ref in net.pins]
    placed = [n for n in nodes if n is not None]
    if len(placed) < 2:
        return True
    # node_for_pin() returns the canonical key of the ElectricSet — equal iff connected.
    first = placed[0]
    return all(n == first for n in placed[1:])


def routed_net_ids(
    schematic: Schematic, graph: ConnectivityGraph
) -> set[uuid.UUID]:
    """Return the set of net IDs that are fully routed."""
    return {
        net.id
        for net in schematic.nets.values()
        if net_is_complete(net, graph)
    }
