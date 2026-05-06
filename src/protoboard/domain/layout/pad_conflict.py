# SPDX-License-Identifier: MIT
"""Pad-conflict detection and resolution logic for component placement.

Pure functions — no Qt, no side effects.
"""

from __future__ import annotations

import math
import uuid
from dataclasses import dataclass
from enum import Enum
from typing import TYPE_CHECKING

from protoboard.domain.layout.placement import ComponentPlacement, pin_world_position

if TYPE_CHECKING:
    from protoboard.domain.components.definition import ComponentDefinition
    from protoboard.domain.components.instance import ComponentInstance
    from protoboard.domain.layout.wire import WireSegment
    from protoboard.domain.schematic.schematic import Schematic
    from protoboard.geometry import Point


class ConflictKind(Enum):
    SAME_NET = "same_net"                   # auto-allowed: shared intentionally
    DIFFERENT_NETS = "diff_nets"            # always blocked
    ONE_UNCONNECTED = "one_unconnected"     # needs user decision
    BOTH_UNCONNECTED = "both_unconnected"   # needs user decision
    # Wire-conflict kinds (pin lands on a wire point):
    PIN_ON_WIRE_UNCONNECTED = "pin_on_wire_unconnected"    # wire no net -> auto-create
    PIN_ON_WIRE_WITH_NET = "pin_on_wire_with_net"          # wire has net -> auto-join
    PIN_ON_WIRE_SAME_NET = "pin_on_wire_same_net"          # same net -> auto-ok
    PIN_ON_WIRE_DIFFERENT_NET = "pin_on_wire_different_net"  # blocked


@dataclass(frozen=True)
class PadConflict:
    """One conflicting pin of the incoming component with an existing pin or wire."""
    # The incoming component's pin number.
    incoming_pin: int
    # The existing component and its pin (uuid.UUID(int=0) / 0 for wire conflicts).
    existing_comp_id: uuid.UUID
    existing_pin: int
    # Shared world position.
    position: Point
    # Classification.
    kind: ConflictKind
    # The net of the existing pin/wire point (None if unconnected).
    existing_net_id: uuid.UUID | None
    existing_net_name: str | None
    # The net of the incoming pin (None if not yet known — always None at placement).
    incoming_net_id: uuid.UUID | None
    incoming_net_name: str | None
    # Set for wire conflicts — identifies which WireSegment caused the conflict.
    wire_id: uuid.UUID | None = None


def find_conflicts(
    incoming_placement: ComponentPlacement,
    incoming_defn: ComponentDefinition,
    incoming_instance_id: uuid.UUID,
    layout_placements: dict[uuid.UUID, ComponentPlacement],
    instances: dict[uuid.UUID, ComponentInstance],
    components: dict[str, ComponentDefinition],
    schematic: Schematic,
) -> list[PadConflict]:
    """Return all pad-on-pad conflicts for *incoming_placement*.

    Ignores pins already listed in *incoming_placement.shared_pins*
    (they were previously resolved).
    """
    n_pins = incoming_defn.footprint.pin_count()
    # Pre-build: world pos -> (comp_id, pin_num) for all existing placements.
    existing: dict[tuple[float, float], tuple[uuid.UUID, int]] = {}
    for comp_id, pl in layout_placements.items():
        if comp_id == incoming_instance_id:
            continue
        inst = instances.get(comp_id)
        if inst is None:
            continue
        defn = components.get(inst.definition_ref)
        if defn is None:
            continue
        for pin_num in range(1, defn.footprint.pin_count() + 1):
            p = pin_world_position(defn.footprint, pl, pin_num)
            key = (_round(p.x), _round(p.y))
            existing[key] = (comp_id, pin_num)

    # Build pin->net map from schematic.
    pin_net: dict[tuple[uuid.UUID, int], tuple[uuid.UUID, str]] = {}
    for net in schematic.nets.values():
        for ref in net.pins:
            pin_net[(ref.component_id, ref.pin_number)] = (net.id, net.name)

    conflicts: list[PadConflict] = []
    for pin_num in range(1, n_pins + 1):
        if pin_num in incoming_placement.shared_pins:
            continue
        p = pin_world_position(incoming_defn.footprint, incoming_placement, pin_num)
        key = (_round(p.x), _round(p.y))
        hit = existing.get(key)
        if hit is None:
            continue
        ex_comp_id, ex_pin_num = hit

        # Net of existing pin.
        ex_net = pin_net.get((ex_comp_id, ex_pin_num))
        ex_net_id = ex_net[0] if ex_net else None
        ex_net_name = ex_net[1] if ex_net else None

        # Net of incoming pin (by instance id, if it's being moved).
        in_net = pin_net.get((incoming_instance_id, pin_num))
        in_net_id = in_net[0] if in_net else None
        in_net_name = in_net[1] if in_net else None

        # Classify.
        if ex_net_id is not None and in_net_id is not None:
            if ex_net_id == in_net_id:
                kind = ConflictKind.SAME_NET
            else:
                kind = ConflictKind.DIFFERENT_NETS
        elif ex_net_id is None and in_net_id is None:
            kind = ConflictKind.BOTH_UNCONNECTED
        else:
            kind = ConflictKind.ONE_UNCONNECTED

        conflicts.append(PadConflict(
            incoming_pin=pin_num,
            existing_comp_id=ex_comp_id,
            existing_pin=ex_pin_num,
            position=p,
            kind=kind,
            existing_net_id=ex_net_id,
            existing_net_name=ex_net_name,
            incoming_net_id=in_net_id,
            incoming_net_name=in_net_name,
        ))

    return conflicts


def find_wire_conflicts(
    incoming_placement: ComponentPlacement,
    incoming_defn: ComponentDefinition,
    incoming_instance_id: uuid.UUID,
    wires: list[WireSegment],
    schematic: Schematic,
    point_to_net: dict[tuple[float, float], tuple[uuid.UUID, str]],
    already_conflicted: set[tuple[float, float]],
) -> list[PadConflict]:
    """Return wire conflicts for *incoming_placement*.

    A wire conflict occurs when a pin of the incoming component lands on a
    point that is part of an existing WireSegment's path.

    *point_to_net* maps rounded (x, y) of any placed pin that has a net to
    (net_id, net_name).  A wire is considered to carry a net when any point
    in its path appears in point_to_net (i.e. another pin connected to that
    wire already has a net).

    *already_conflicted* positions are skipped (already reported as pad conflicts).
    """
    # Build wire points: rounded (x, y) -> wire_id.
    wire_points: dict[tuple[float, float], uuid.UUID] = {}
    for wire in wires:
        for pt in wire.path:
            wire_points[(_round(pt.x), _round(pt.y))] = wire.id

    # Determine the net carried by each wire (first netted point wins).
    wire_net_map: dict[uuid.UUID, tuple[uuid.UUID, str] | None] = {}
    for wire in wires:
        found: tuple[uuid.UUID, str] | None = None
        for pt in wire.path:
            info = point_to_net.get((_round(pt.x), _round(pt.y)))
            if info is not None:
                found = info
                break
        wire_net_map[wire.id] = found

    # Net of incoming component's pins (for move conflicts where pin has a net).
    pin_net_incoming: dict[int, tuple[uuid.UUID, str]] = {}
    for net in schematic.nets.values():
        for ref in net.pins:
            if ref.component_id == incoming_instance_id:
                pin_net_incoming[ref.pin_number] = (net.id, net.name)

    conflicts: list[PadConflict] = []
    n_pins = incoming_defn.footprint.pin_count()
    _sentinel = uuid.UUID(int=0)

    for pin_num in range(1, n_pins + 1):
        p = pin_world_position(incoming_defn.footprint, incoming_placement, pin_num)
        key = (_round(p.x), _round(p.y))
        if key in already_conflicted:
            continue
        wire_id = wire_points.get(key)
        if wire_id is None:
            continue

        wire_net_info = wire_net_map.get(wire_id)
        in_net = pin_net_incoming.get(pin_num)

        if wire_net_info is not None and in_net is not None:
            if wire_net_info[0] == in_net[0]:
                kind = ConflictKind.PIN_ON_WIRE_SAME_NET
            else:
                kind = ConflictKind.PIN_ON_WIRE_DIFFERENT_NET
        elif wire_net_info is not None:
            # Wire carries a net, incoming pin does not.
            kind = ConflictKind.PIN_ON_WIRE_WITH_NET
        elif in_net is not None:
            # Incoming pin has a net, wire does not -> wire is the unconnected side.
            # Treat as WITH_NET using incoming's net as "existing" so
            # _update_schematic adds the pin to that net (it already belongs to it).
            kind = ConflictKind.PIN_ON_WIRE_WITH_NET
            wire_net_info = in_net
            in_net = None
        else:
            kind = ConflictKind.PIN_ON_WIRE_UNCONNECTED

        conflicts.append(PadConflict(
            incoming_pin=pin_num,
            existing_comp_id=_sentinel,
            existing_pin=0,
            position=p,
            kind=kind,
            existing_net_id=wire_net_info[0] if wire_net_info else None,
            existing_net_name=wire_net_info[1] if wire_net_info else None,
            incoming_net_id=in_net[0] if in_net else None,
            incoming_net_name=in_net[1] if in_net else None,
            wire_id=wire_id,
        ))

    return conflicts


def _round(v: float) -> float:
    return round(v, 4)


# ---------------------------------------------------------------------------
# Wire-segment crossing detection
# ---------------------------------------------------------------------------

_CROSS_EPS = 0.5  # half-raster tolerance — pad radius in raster units


def _seg_point_dist(px: float, py: float,
                    ax: float, ay: float,
                    bx: float, by: float) -> float:
    """Shortest distance from point (px, py) to segment (ax, ay)→(bx, by)."""
    dx, dy = bx - ax, by - ay
    if dx == 0.0 and dy == 0.0:
        return math.hypot(px - ax, py - ay)
    t = max(0.0, min(1.0, ((px - ax) * dx + (py - ay) * dy) / (dx * dx + dy * dy)))
    return math.hypot(px - ax - t * dx, py - ay - t * dy)


def find_wires_crossing_pads(
    wires: list[WireSegment],
    moved_comp_ids: set[uuid.UUID],
    layout_placements: dict[uuid.UUID, ComponentPlacement],
    instances: dict[uuid.UUID, ComponentInstance],
    components: dict[str, ComponentDefinition],
    schematic: Schematic,
) -> list[uuid.UUID]:
    """Return IDs of wires whose path passes within _CROSS_EPS of an alien pad.

    Checks every *line segment* of wire.path — not only the vertices.
    A wire routed diagonally from (1,1) to (3,3) that passes within
    half-raster of a pad at (2, 2.1) is considered conflicting even though
    no vertex coincides with that pad.

    "Alien pad" means a pad belonging to a net different from the wire's net,
    or a pad with a net when the wire is unconnected (and vice-versa).
    Pads belonging to `moved_comp_ids` are the new (post-move) positions.

    Returns a list of wire IDs that should be removed.
    """
    # Build pad_pos → net_id | None for ALL pins (moved comps already updated
    # in layout_placements before this function is called).
    pad_net: dict[tuple[float, float], uuid.UUID | None] = {}
    pin_net_map: dict[tuple[uuid.UUID, int], uuid.UUID] = {}
    for net in schematic.nets.values():
        for ref in net.pins:
            pin_net_map[(ref.component_id, ref.pin_number)] = net.id
    for comp_id, pl in layout_placements.items():
        inst = instances.get(comp_id)
        if inst is None:
            continue
        defn = components.get(inst.definition_ref)
        if defn is None:
            continue
        for pin_num in range(1, defn.footprint.pin_count() + 1):
            p = pin_world_position(defn.footprint, pl, pin_num)
            net_id = pin_net_map.get((comp_id, pin_num))
            pad_net[(_round(p.x), _round(p.y))] = net_id

    if not pad_net:
        return []

    # Pre-extract pad positions as floats for distance calculations.
    pad_positions: list[tuple[float, float, uuid.UUID | None]] = [
        (float(k[0]), float(k[1]), v) for k, v in pad_net.items()
    ]

    # Build wire → net map: first pad in wire.path that has a net wins.
    wire_net: dict[uuid.UUID, uuid.UUID | None] = {}
    for wire in wires:
        found: uuid.UUID | None = None
        for pt in wire.path:
            info = pad_net.get((_round(pt.x), _round(pt.y)))
            if info is not None:
                found = info
                break
        wire_net[wire.id] = found

    conflicting: list[uuid.UUID] = []
    for wire in wires:
        w_net = wire_net.get(wire.id)
        path = wire.path
        if len(path) < 2:
            continue
        bad = False
        for i in range(len(path) - 1):
            ax, ay = path[i].x, path[i].y
            bx, by = path[i + 1].x, path[i + 1].y
            for px, py, p_net in pad_positions:
                if _seg_point_dist(px, py, ax, ay, bx, by) < _CROSS_EPS:
                    # Same net on both sides → OK (intentional contact).
                    if w_net is not None and p_net is not None and w_net == p_net:
                        continue
                    # Both unconnected → no conflict (free wire passing free pad).
                    if w_net is None and p_net is None:
                        continue
                    # Otherwise: alien pad encountered.
                    bad = True
                    break
            if bad:
                break
        if bad:
            conflicting.append(wire.id)

    return conflicting
