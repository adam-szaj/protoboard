# SPDX-License-Identifier: MIT
"""DRCEngine — checks SHORT, MISSING_NET, OVERLAP."""

from __future__ import annotations

import uuid
from dataclasses import dataclass
from itertools import combinations
from typing import Literal

import structlog
from protoboard.domain.board.board import Board
from protoboard.domain.components.definition import ComponentDefinition
from protoboard.domain.components.instance import ComponentInstance
from protoboard.domain.connectivity.graph import ConnectivityGraph
from protoboard.domain.layout.layout import Layout
from protoboard.domain.layout.placement import pin_world_position
from protoboard.domain.schematic.schematic import Schematic
from protoboard.geometry import Point

_log = structlog.get_logger(__name__)
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
        issues += _check_floating_pins(schematic, layout)
        issues += _check_bare_wire_cross(schematic, layout, graph)
        counts: dict[str, int] = {"error": 0, "warning": 0, "info": 0}
        for iss in issues:
            counts[iss.severity] = counts.get(iss.severity, 0) + 1
        _log.info(
            "drc.complete",
            total=len(issues),
            errors=counts["error"],
            warnings=counts["warning"],
            infos=counts["info"],
            codes=[iss.code for iss in issues],
        )
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
        if len(occupants) <= 1:
            continue
        # Filter out pairs where every occupant has the pin in shared_pins
        # (intentional sharing agreed by the user).
        unshared = [
            (cid, pnum)
            for cid, pnum in occupants
            if pnum not in (layout.placements[cid].shared_pins
                            if cid in layout.placements else frozenset())
        ]
        if len(unshared) <= 1:
            continue
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


def _check_floating_pins(
    schematic: Schematic,
    layout: Layout,
) -> list[DRCIssue]:
    """Warn about nets whose pins are not placed on the board at all."""
    issues: list[DRCIssue] = []
    for net in schematic.nets.values():
        for ref in net.pins:
            if ref.component_id not in layout.placements:
                issues.append(
                    DRCIssue(
                        severity="warning",
                        code="FLOATING_PIN",
                        message=(
                            f"Net {net.name!r}: component {ref.component_id} "
                            f"pin {ref.pin_number} is not placed"
                        ),
                        locations=[],
                        affected_nets=[net.id],
                    )
                )
    return issues


def _check_bare_wire_cross(
    schematic: Schematic,
    layout: Layout,
    graph: ConnectivityGraph,
) -> list[DRCIssue]:
    """Detect bare wires of different nets sharing a pad (electrical cross).

    A bare wire is assigned to the net whose pins it directly connects to
    (checking the pin-world-positions against wire endpoints). Two bare wires
    that share a pad and belong to different nets constitute an illegal cross.
    """
    bare_wires = [w for w in layout.wires if not w.insulated]
    if len(bare_wires) < 2:
        return []

    # Map (x, y, side) pad → set of net IDs whose bare wires pass through it.
    # We use the schematic-level net assignment: a pad that belongs to a net
    # (via pin_world_position) tags every bare wire passing through it.
    pad_net_ids: dict[tuple[float, float, str], set[uuid.UUID]] = {}

    # Build pad → net mapping from placed component pins.
    # Register both sides for each pad so that plated-through pads (where top
    # and bottom are one electrical node) are found regardless of wire side.
    for net in schematic.nets.values():
        for ref in net.pins:
            n = graph.node_for_pin(ref)
            if n is None:
                continue
            nx, ny, _ = n
            for s in ("top", "bottom"):
                pad_net_ids.setdefault((nx, ny, s), set()).add(net.id)

    # For each bare wire path point, collect which nets are declared at that pad.
    wire_pad_nets: dict[uuid.UUID, set[uuid.UUID]] = {}
    for wire in bare_wires:
        for pt in wire.path:
            key = (pt.x, pt.y, wire.side)
            nets_here = pad_net_ids.get(key, set())
            wire_pad_nets.setdefault(wire.id, set()).update(nets_here)

    # Find pads shared by two bare wires from different declared nets.
    pad_wire_ids: dict[tuple[float, float, str], list[uuid.UUID]] = {}
    for wire in bare_wires:
        for pt in wire.path:
            key = (pt.x, pt.y, wire.side)
            pad_wire_ids.setdefault(key, []).append(wire.id)

    issues: list[DRCIssue] = []
    reported: set[frozenset[uuid.UUID]] = set()
    for (x, y, side), wids in pad_wire_ids.items():
        if len(wids) < 2:
            continue
        # Collect all net IDs from wires that pass through this pad.
        nets_at_pad: set[uuid.UUID] = set()
        for wid in wids:
            nets_at_pad.update(wire_pad_nets.get(wid, set()))
        if len(nets_at_pad) < 2:
            continue
        key_f = frozenset(nets_at_pad)
        if key_f in reported:
            continue
        reported.add(key_f)
        issues.append(
            DRCIssue(
                severity="error",
                code="BARE_WIRE_CROSS",
                message=(
                    f"Bare wires of different nets cross at ({x:.2f}, {y:.2f}) "
                    f"on {side}"
                ),
                locations=[Point(x, y)],
                affected_nets=list(nets_at_pad),
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
