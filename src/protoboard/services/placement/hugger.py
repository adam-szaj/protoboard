# SPDX-License-Identifier: MIT
"""Hug — pull net-connected components closer together.

Starting from a user selection, hug expands the working set to include
direct net-neighbours (one hop), then moves each unlocked mobile component
toward the weighted centroid of its connected neighbours in the working set.

Algorithm
---------
1. Build working set = selected + their direct net-neighbours (placed, one hop).
2. Locked components stay fixed but act as attractors.
3. For each unlocked mobile component (sorted by net-degree desc):
   a. Compute target = weighted centroid of working-set neighbours' pin positions.
   b. Snap to nearest board pad on the component's side, collision-free.
   c. If blocked at ideal pad, search outward (same as spreader _find_free_slot).
4. Return HugResult with new placements for moved components.

Reuses:
  _find_free_slot    from services/placement/auto_placer.py
  board_pads_for_side from domain/layout/wire_utils.py
  pin_world_position  from domain/layout/placement.py
"""

from __future__ import annotations

import math
import uuid
from dataclasses import dataclass, field
from typing import Callable, Protocol

import structlog

from protoboard.domain.board.board import Board
from protoboard.domain.components.definition import ComponentDefinition
from protoboard.domain.components.instance import ComponentInstance
from protoboard.domain.layout.layout import Layout
from protoboard.domain.layout.placement import ComponentPlacement, pin_world_position
from protoboard.domain.layout.wire_utils import board_pads_for_side
from protoboard.domain.schematic.schematic import Schematic
from protoboard.geometry import Point
from protoboard.services.placement.auto_placer import _find_free_slot

_log = structlog.get_logger(__name__)


# ---------------------------------------------------------------------------
# Public types
# ---------------------------------------------------------------------------


@dataclass
class HugResult:
    """Result of :func:`hug_components`."""

    new_placements: dict[uuid.UUID, ComponentPlacement] = field(default_factory=dict)
    moved_ids: set[uuid.UUID] = field(default_factory=set)
    unchanged_ids: set[uuid.UUID] = field(default_factory=set)


class HugState(Protocol):
    """Minimal session interface required by the hugger (no Qt)."""

    board: Board
    layout: Layout
    schematic: Schematic
    instances: dict[uuid.UUID, ComponentInstance]
    components: dict[str, ComponentDefinition]


# ---------------------------------------------------------------------------
# Public entry point
# ---------------------------------------------------------------------------


def hug_components(
    selected_ids: set[uuid.UUID],
    session: HugState,
    progress_callback: Callable[[int, int], None] | None = None,
) -> HugResult:
    """Pull *selected_ids* and their net-neighbours closer together.

    Locked components in the working set act as fixed attractors — they
    influence targets but do not move themselves.  Components outside the
    working set are treated as immovable obstacles.

    Args:
        progress_callback: Optional ``(current, total)`` callable invoked
            after each mobile component is processed.  Suitable for driving
            a progress bar from a background thread via Qt signals.

    Returns:
        :class:`HugResult` with ``new_placements``, ``moved_ids``, and
        ``unchanged_ids``.
    """
    # Filter selected to placed components with known definitions.
    valid_selected: set[uuid.UUID] = set()
    for cid in selected_ids:
        if cid not in session.layout.placements:
            continue
        inst = session.instances.get(cid)
        if inst and session.components.get(inst.definition_ref):
            valid_selected.add(cid)

    if not valid_selected:
        return HugResult()

    # Step 1 — build working set: selected + direct net-neighbours (one hop).
    # Global nets (GND, VCC, etc.) are excluded from neighbour expansion.
    working_set: set[uuid.UUID] = set(valid_selected)
    for cid in valid_selected:
        for ref in session.schematic.pins_for_component(cid):
            net = session.schematic.net_for_pin(ref)
            if net is None or net.global_net:
                continue
            for pr in net.pins:
                neighbour = pr.component_id
                if neighbour == cid or neighbour not in session.layout.placements:
                    continue
                inst = session.instances.get(neighbour)
                if inst and session.components.get(inst.definition_ref):
                    working_set.add(neighbour)

    if len(working_set) < 2:
        _log.info("hugger.skip", reason="working_set_too_small", size=len(working_set))
        return HugResult()

    # Partition into mobile (unlocked) and fixed-within-working-set (locked).
    mobile_ids: list[uuid.UUID] = []
    for cid in working_set:
        pl = session.layout.placements[cid]
        if not pl.locked:
            mobile_ids.append(cid)

    if not mobile_ids:
        _log.info("hugger.skip", reason="all_locked")
        return HugResult()

    # Board pads for snap.
    pads_top: set[Point] = board_pads_for_side(session.board, "top")
    pads_bottom: set[Point] = board_pads_for_side(session.board, "bottom")

    # Build fixed obstacles: everything NOT in mobile_ids.
    mobile_set = set(mobile_ids)
    fixed: dict[uuid.UUID, tuple[ComponentPlacement, ComponentDefinition]] = {}
    for cid, pl in session.layout.placements.items():
        if cid in mobile_set:
            continue
        inst = session.instances.get(cid)
        defn = session.components.get(inst.definition_ref) if inst else None
        if defn is not None:
            fixed[cid] = (pl, defn)

    # Compute per-component net-degree within working set (for ordering).
    # Global nets are excluded — they should not skew ordering.
    net_degree: dict[uuid.UUID, float] = {cid: 0.0 for cid in mobile_ids}
    for net in session.schematic.nets.values():
        if net.global_net:
            continue
        members = [pr.component_id for pr in net.pins if pr.component_id in working_set]
        for cid in members:
            if cid in net_degree:
                net_degree[cid] += len(members) - 1

    # Process most-connected first so they anchor near their target first.
    ordered = sorted(mobile_ids, key=lambda c: -net_degree.get(c, 0.0))

    # Collision map starts with all fixed obstacles; mobile comps added as placed.
    collision_map: dict[uuid.UUID, tuple[ComponentPlacement, ComponentDefinition]] = dict(fixed)

    result_placements: dict[uuid.UUID, ComponentPlacement] = {}
    total_mobile = len(ordered)

    for i, cid in enumerate(ordered):
        inst = session.instances.get(cid)
        defn = session.components.get(inst.definition_ref) if inst else None
        if inst is None or defn is None:
            result_placements[cid] = session.layout.placements[cid]
            continue

        orig_pl = session.layout.placements[cid]

        # Compute target: weighted centroid of working-set neighbours' pin positions.
        # Use current positions from result_placements if already moved, else original.
        target = _target_position(cid, working_set, session, result_placements)

        board_pads = pads_top if orig_pl.side == "top" else pads_bottom
        sorted_pads = sorted(
            board_pads,
            key=lambda p: math.hypot(p.x - target.x, p.y - target.y),
        )

        # For SingleRow footprints, compute preferred rotation so the connected
        # pin points toward the main neighbour.  For multi-row or grid footprints
        # keep the original rotation — orienting them is less meaningful.
        preferred_rotations = _preferred_rotations_toward(
            cid, defn, orig_pl, working_set, session, result_placements, target
        )

        # extra_gap=2.0 inflates the new component's bbox by 2 rasters on each
        # side, guaranteeing at least 2-raster clearance between component
        # bodies after hugging — for both rigid and flexible-lead parts.
        pl = _find_free_slot(
            cid, defn, sorted_pads, preferred_rotations, orig_pl.side, collision_map,
            extra_gap=2.0,
        )

        if pl is None:
            new_pl = orig_pl.copy()
        else:
            new_pl = ComponentPlacement(
                component_id=cid,
                position=pl.position,
                rotation=pl.rotation,
                side=orig_pl.side,
                label_offset=orig_pl.label_offset,
                pin_offsets=dict(orig_pl.pin_offsets),
                shared_pins=orig_pl.shared_pins,
                vertical_flipped=orig_pl.vertical_flipped,
                locked=orig_pl.locked,
            )

        result_placements[cid] = new_pl
        collision_map[cid] = (new_pl, defn)
        if progress_callback is not None:
            progress_callback(i + 1, total_mobile)

    _log.info(
        "hugger.done",
        working_set=len(working_set),
        mobile=len(mobile_ids),
        selected=len(valid_selected),
    )

    result = HugResult()
    for cid, new_pl in result_placements.items():
        orig_pos = session.layout.placements[cid].position
        if new_pl.position != orig_pos:
            result.moved_ids.add(cid)
        else:
            result.unchanged_ids.add(cid)
        result.new_placements[cid] = new_pl

    return result


# ---------------------------------------------------------------------------
# Internal helpers
# ---------------------------------------------------------------------------


def _target_position(
    cid: uuid.UUID,
    working_set: set[uuid.UUID],
    session: HugState,
    already_placed: dict[uuid.UUID, ComponentPlacement],
) -> Point:
    """Weighted centroid of working-set net-neighbours' pin positions.

    Uses the latest position from *already_placed* if a neighbour was already
    moved in this hug pass, otherwise falls back to the original placement.
    Falls back to the component's own current position if it has no neighbours
    in the working set.
    """
    weighted_x = 0.0
    weighted_y = 0.0
    total_w = 0.0

    for ref in session.schematic.pins_for_component(cid):
        net = session.schematic.net_for_pin(ref)
        if net is None or net.global_net:
            continue
        for pr in net.pins:
            neighbour = pr.component_id
            if neighbour == cid or neighbour not in working_set:
                continue
            # Prefer already-computed position for this pass.
            neighbour_pl = already_placed.get(
                neighbour, session.layout.placements[neighbour]
            )
            inst = session.instances.get(neighbour)
            defn = session.components.get(inst.definition_ref) if inst else None
            if defn is None:
                continue
            pos = pin_world_position(defn.footprint, neighbour_pl, pr.pin_number)
            weighted_x += pos.x
            weighted_y += pos.y
            total_w += 1.0

    if total_w == 0.0:
        return session.layout.placements[cid].position

    return Point(weighted_x / total_w, weighted_y / total_w)


def _rotation_toward(pin1: Point, target: Point) -> int:
    """Return the rotation (0/90/180/270) that makes a SingleRow pin axis
    point from *pin1* toward *target*.

    For a SingleRow footprint the second pin is at local (0, pitch), so the
    component "axis" runs along local +Y.  After clockwise rotation by R:
      R=0   → world +Y  (down in screen coords)
      R=90  → world +X  (right)
      R=180 → world -Y  (up in screen coords)
      R=270 → world -X  (left)

    We pick the rotation whose axis direction most closely matches the
    target vector from pin1.
    """
    dx = target.x - pin1.x
    dy = target.y - pin1.y
    if dx == 0.0 and dy == 0.0:
        return 0
    # Each rotation maps to a cardinal direction for the axis.
    # Score = dot product with target vector (higher = better alignment).
    scores = {
        0:   dy,        # axis points +Y
        90:  dx,        # axis points +X
        180: -dy,       # axis points -Y
        270: -dx,       # axis points -X
    }
    return max(scores, key=lambda r: scores[r])


def _preferred_rotations_toward(
    cid: uuid.UUID,
    defn: "ComponentDefinition",
    orig_pl: ComponentPlacement,
    working_set: set[uuid.UUID],
    session: HugState,
    already_placed: dict[uuid.UUID, ComponentPlacement],
    target: Point,
) -> list[int]:
    """Return rotations sorted by preference for this component during hugging.

    For SingleRow footprints (resistors, diodes, capacitors…) the preferred
    rotation is computed so the component axis aligns with the vector toward
    the main connected neighbour.  The other three rotations follow as
    fallback in case the preferred slot is occupied.

    For all other footprint types the original rotation is returned unchanged
    (rotating a DIP-16 toward a neighbour is not well-defined).
    """
    from protoboard.domain.components.footprints import SingleRow

    if not isinstance(defn.footprint, SingleRow):
        return [orig_pl.rotation]

    # Pin 1 at current (or computed) position of this component.
    pin1 = pin_world_position(defn.footprint, orig_pl, 1)

    best = _rotation_toward(pin1, target)

    # Order: best first, then the remaining three in 90° steps (so the
    # runner-up is always a 90° rotation away, not the opposite direction).
    all_rots = [0, 90, 180, 270]
    idx = all_rots.index(best)
    ordered = [all_rots[(idx + i) % 4] for i in range(4)]
    return ordered
