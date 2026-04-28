# SPDX-License-Identifier: MIT
"""DRCEngine — checks SHORT, MISSING_NET, OVERLAP."""

from __future__ import annotations

import uuid
from dataclasses import dataclass
from itertools import combinations
from typing import Literal

from protoboard.domain.board.board import Board
from protoboard.domain.components.definition import ComponentDefinition
from protoboard.domain.components.instance import ComponentInstance
from protoboard.domain.connectivity.graph import ConnectivityGraph
from protoboard.domain.layout.layout import Layout
from protoboard.domain.layout.placement import pin_world_position
from protoboard.domain.schematic.schematic import Schematic
from protoboard.geometry import Point

_Severity = Literal["error", "warning", "info"]
_VALID_SEVERITIES = frozenset({"error", "warning", "info"})


@dataclass(frozen=True)
class DRCIssue:
    severity: _Severity
    code: str
    message: str
    locations: list[Point]
    affected_nets: list[uuid.UUID]

    def __post_init__(self) -> None:
        if self.severity not in _VALID_SEVERITIES:
            raise ValueError(
                f"severity must be one of {_VALID_SEVERITIES}; got {self.severity!r}"
            )


class DRCEngine:
    def check(
        self,
        board: Board,
        schematic: Schematic,
        layout: Layout,
        components: dict[str, ComponentDefinition],
        instances: dict[uuid.UUID, ComponentInstance],
    ) -> list[DRCIssue]:
        graph = ConnectivityGraph(
            board=board,
            layout=layout,
            components=components,
            instances=instances,
        )
        issues: list[DRCIssue] = []
        issues += _check_overlap(layout, components, instances)
        issues += _check_short(schematic, graph)
        issues += _check_missing_net(schematic, graph)
        return issues


def _check_overlap(
    layout: Layout,
    components: dict[str, ComponentDefinition],
    instances: dict[uuid.UUID, ComponentInstance],
) -> list[DRCIssue]:
    """Detect two component pins occupying the same pad on the same side."""
    # Build: (x, y, side) → list of (comp_id, pin)
    pad_occupancy: dict[tuple[float, float, str], list[tuple[uuid.UUID, int]]] = {}
    for comp_id, placement in layout.placements.items():
        inst = instances.get(comp_id)
        if inst is None:
            continue
        defn = components.get(inst.definition_ref)
        if defn is None:
            continue
        for pin_num in range(1, defn.pin_count + 1):
            wp = pin_world_position(defn.footprint, placement, pin_num)
            key = (wp.x, wp.y, placement.side)
            pad_occupancy.setdefault(key, []).append((comp_id, pin_num))

    issues: list[DRCIssue] = []
    for (x, y, side), occupants in pad_occupancy.items():
        if len(occupants) > 1:
            issues.append(
                DRCIssue(
                    severity="error",
                    code="OVERLAP",
                    message=(
                        f"Multiple components share pad ({x},{y}) on {side}: "
                        + ", ".join(f"{iid}/{p}" for iid, p in occupants)
                    ),
                    locations=[Point(x, y)],
                    affected_nets=[],
                )
            )
    return issues


def _check_short(
    schematic: Schematic,
    graph: ConnectivityGraph,
) -> list[DRCIssue]:
    """Detect pads from different nets connected in the physical layout."""
    issues: list[DRCIssue] = []
    # Gather physical node → set of logical net IDs.
    node_nets: dict[tuple[float, float, str], set[uuid.UUID]] = {}
    for net in schematic.nets.values():
        for ref in net.pins:
            node = graph.node_for_pin(ref)
            if node is None:
                continue
            node_nets.setdefault(node, set()).add(net.id)

    for node, net_ids in node_nets.items():
        if len(net_ids) > 1:
            issues.append(
                DRCIssue(
                    severity="error",
                    code="SHORT",
                    message=f"Short between nets: {net_ids}",
                    locations=[],
                    affected_nets=list(net_ids),
                )
            )
    return issues


def _check_missing_net(
    schematic: Schematic,
    graph: ConnectivityGraph,
) -> list[DRCIssue]:
    """Detect nets whose pins are not fully connected in the physical layout."""
    issues: list[DRCIssue] = []
    for net in schematic.nets.values():
        pins = list(net.pins)
        if len(pins) < 2:
            continue
        # All pins in the net must be in the same connected component.
        nodes = [graph.node_for_pin(ref) for ref in pins]
        placed_nodes = [n for n in nodes if n is not None]
        if len(placed_nodes) < 2:
            continue
        for ref_a, ref_b in combinations(pins, 2):
            na = graph.node_for_pin(ref_a)
            nb = graph.node_for_pin(ref_b)
            if na is None or nb is None:
                continue
            if na != nb:
                issues.append(
                    DRCIssue(
                        severity="error",
                        code="MISSING_NET",
                        message=(f"Net {net.name!r}: pins not physically connected"),
                        locations=[],
                        affected_nets=[net.id],
                    )
                )
                break  # one issue per net is enough
    return issues
