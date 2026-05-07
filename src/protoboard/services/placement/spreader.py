# SPDX-License-Identifier: MIT
"""Force-directed component spreader.

Two-phase algorithm:
  Phase 1 — Force-directed relaxation in continuous space.  Spring attraction
             between net-connected mobile component pairs and mutual repulsion
             between all mobile pairs.  Fixed (unselected) components contribute
             one-directional anchor pulls only.

  Phase 2 — Greedy snap to discrete pad grid.  Each component's ideal float
             position from Phase 1 is mapped to the nearest valid board pad
             with no body collisions, using the same slot-finder as auto_placer.

User decisions preserved:
  - Rotation: never changed.
  - Side (top/bottom): never changed.
  - pin_offsets / shared_pins: copied verbatim from original placement.
"""

from __future__ import annotations

import math
import uuid
from dataclasses import dataclass, field
from typing import Protocol

import structlog

from protoboard.domain.board.board import Board
from protoboard.domain.components.definition import ComponentDefinition
from protoboard.domain.components.instance import ComponentInstance
from protoboard.domain.layout.layout import Layout
from protoboard.domain.layout.placement import (
    ComponentPlacement,
    component_bbox,
    pin_world_position,
)
from protoboard.domain.layout.wire_utils import board_pads_for_side
from protoboard.domain.schematic.schematic import Schematic
from protoboard.geometry import Point, Rect
from protoboard.services.placement.auto_placer import _find_free_slot, _has_collision

_log = structlog.get_logger(__name__)

# ---------------------------------------------------------------------------
# Tuning constants
# ---------------------------------------------------------------------------

_SPRING_K: float = 0.15        # spring attraction coefficient
_REPULSE_K: float = 3.0        # repulsion coefficient
_NEUTRAL_ZONE: float = 2.0     # RU — no spring attraction within this distance
_MIN_REPULSE_DIST: float = 0.5 # RU — clamp to avoid singularity
_ANCHOR_K: float = 0.08        # one-sided pull toward fixed net-neighbors


# ---------------------------------------------------------------------------
# Public types
# ---------------------------------------------------------------------------


@dataclass
class SpreadResult:
    """Result of :func:`spread_components`."""

    new_placements: dict[uuid.UUID, ComponentPlacement] = field(default_factory=dict)
    moved_ids: set[uuid.UUID] = field(default_factory=set)
    unchanged_ids: set[uuid.UUID] = field(default_factory=set)


class SpreadState(Protocol):
    """Minimal session interface required by the spreader (no Qt)."""

    board: Board
    layout: Layout
    schematic: Schematic
    instances: dict[uuid.UUID, ComponentInstance]
    components: dict[str, ComponentDefinition]


# ---------------------------------------------------------------------------
# Public entry point
# ---------------------------------------------------------------------------


def spread_components(
    component_ids: list[uuid.UUID],
    session: SpreadState,
    max_iterations: int = 200,
    temperature: float = 1.0,
    max_net_distance: float = 16.0,
) -> SpreadResult:
    """Spread *component_ids* more evenly using force-directed relaxation.

    Components NOT in *component_ids* that are already placed are treated as
    immovable obstacles during the snap phase.  Rotations and sides are never
    changed.

    Args:
        component_ids: UUIDs of placed components to spread.
        session: Editor session providing board, layout, schematic, etc.
        max_iterations: Force simulation steps (Phase 1).
        temperature: Initial force scale; decays linearly to 0.
        max_net_distance: Spring force is disabled beyond this distance (RU).

    Returns:
        :class:`SpreadResult` with ``new_placements``, ``moved_ids``, and
        ``unchanged_ids``.
    """
    # Filter to only those IDs that are actually placed and have a definition.
    mobile_ids: list[uuid.UUID] = []
    for cid in component_ids:
        if cid not in session.layout.placements:
            continue
        inst = session.instances.get(cid)
        if inst is None:
            continue
        if session.components.get(inst.definition_ref) is None:
            continue
        mobile_ids.append(cid)

    if len(mobile_ids) < 2:
        return SpreadResult()

    # Fixed obstacles: placed but not being spread.
    fixed: dict[uuid.UUID, tuple[ComponentPlacement, ComponentDefinition]] = {}
    for cid, pl in session.layout.placements.items():
        if cid in set(mobile_ids):
            continue
        inst = session.instances.get(cid)
        defn = session.components.get(inst.definition_ref) if inst else None
        if defn is not None:
            fixed[cid] = (pl, defn)

    # Separate board-pads per side so bottom-side components use correct pads.
    pads_top: set[Point] = board_pads_for_side(session.board, "top")
    pads_bottom: set[Point] = board_pads_for_side(session.board, "bottom")

    net_graph = _build_net_graph(set(mobile_ids), session.schematic)
    anchors = _anchor_forces_map(set(mobile_ids), fixed, session)

    # Starting positions: current placement positions (float).
    positions: dict[uuid.UUID, Point] = {
        cid: session.layout.placements[cid].position for cid in mobile_ids
    }

    # Bbox radii for repulsion distance correction.
    bbox_radii: dict[uuid.UUID, float] = {
        cid: _bbox_radius(cid, session, session.layout.placements[cid])
        for cid in mobile_ids
    }

    board_bounds = session.board.bounds

    # Phase 1 — force simulation.
    positions = _run_force_simulation(
        positions=positions,
        net_graph=net_graph,
        anchor_pulls=anchors,
        bbox_radii=bbox_radii,
        board_bounds=board_bounds,
        max_iterations=max_iterations,
        temperature=temperature,
        max_net_distance=max_net_distance,
    )

    # Phase 2 — snap to discrete pad grid.
    new_placements = _snap_to_grid(
        ideal_positions=positions,
        mobile_ids=mobile_ids,
        original_placements={cid: session.layout.placements[cid] for cid in mobile_ids},
        fixed_obstacles=fixed,
        pads_top=pads_top,
        pads_bottom=pads_bottom,
        session=session,
    )

    result = SpreadResult()
    for cid, new_pl in new_placements.items():
        orig_pos = session.layout.placements[cid].position
        if new_pl.position != orig_pos:
            result.moved_ids.add(cid)
        else:
            result.unchanged_ids.add(cid)
        result.new_placements[cid] = new_pl

    return result


# ---------------------------------------------------------------------------
# Phase 1 — Force-directed relaxation
# ---------------------------------------------------------------------------


def _run_force_simulation(
    positions: dict[uuid.UUID, Point],
    net_graph: dict[uuid.UUID, dict[uuid.UUID, float]],
    anchor_pulls: dict[uuid.UUID, list[Point]],
    bbox_radii: dict[uuid.UUID, float],
    board_bounds: Rect,
    max_iterations: int,
    temperature: float,
    max_net_distance: float,
) -> dict[uuid.UUID, Point]:
    """Return positions after force-directed relaxation (pure function)."""
    comp_ids = list(positions.keys())
    n = len(comp_ids)
    # Work with mutable lists for speed.
    xs = [positions[c].x for c in comp_ids]
    ys = [positions[c].y for c in comp_ids]
    idx = {c: i for i, c in enumerate(comp_ids)}
    radii = [bbox_radii.get(c, 0.5) for c in comp_ids]

    bx, by = board_bounds.x, board_bounds.y
    bw, bh = board_bounds.width, board_bounds.height

    for iteration in range(max_iterations):
        t = temperature * (1.0 - iteration / max_iterations)
        fx = [0.0] * n
        fy = [0.0] * n

        # Repulsion between all mobile pairs.
        for i in range(n):
            for j in range(i + 1, n):
                dx = xs[j] - xs[i]
                dy = ys[j] - ys[i]
                dist = math.hypot(dx, dy)
                effective = max(_MIN_REPULSE_DIST, dist - radii[i] - radii[j])
                f = _REPULSE_K / (effective * effective)
                if dist < 1e-6:
                    # Exactly coincident: push in a deterministic direction.
                    dx, dy = 1.0, 0.0
                    dist = 1.0
                ux, uy = dx / dist, dy / dist
                fx[i] -= f * ux
                fy[i] -= f * uy
                fx[j] += f * ux
                fy[j] += f * uy

        # Spring attraction between net-connected mobile pairs.
        for ci, neighbors in net_graph.items():
            i = idx[ci]
            for cj, weight in neighbors.items():
                if cj not in idx:
                    continue
                j = idx[cj]
                if j <= i:
                    continue  # avoid double-counting
                dx = xs[j] - xs[i]
                dy = ys[j] - ys[i]
                dist = math.hypot(dx, dy)
                if dist < 1e-6 or dist <= _NEUTRAL_ZONE or dist > max_net_distance:
                    continue
                pull = _SPRING_K * weight * (dist - _NEUTRAL_ZONE)
                ux, uy = dx / dist, dy / dist
                fx[i] += pull * ux
                fy[i] += pull * uy
                fx[j] -= pull * ux
                fy[j] -= pull * uy

        # Anchor pull toward fixed net-neighbors.
        for ci, anchor_pts in anchor_pulls.items():
            i = idx.get(ci)
            if i is None or not anchor_pts:
                continue
            for apt in anchor_pts:
                dx = apt.x - xs[i]
                dy = apt.y - ys[i]
                dist = math.hypot(dx, dy)
                if dist < 1e-6 or dist <= _NEUTRAL_ZONE:
                    continue
                pull = _ANCHOR_K * (dist - _NEUTRAL_ZONE)
                ux, uy = dx / dist, dy / dist
                fx[i] += pull * ux
                fy[i] += pull * uy

        # Apply forces with temperature cooling + board boundary clamping.
        for i in range(n):
            new_x = xs[i] + fx[i] * t
            new_y = ys[i] + fy[i] * t
            xs[i] = max(bx, min(bx + bw, new_x))
            ys[i] = max(by, min(by + bh, new_y))

    return {comp_ids[i]: Point(xs[i], ys[i]) for i in range(n)}


# ---------------------------------------------------------------------------
# Phase 2 — Greedy snap to discrete pad grid
# ---------------------------------------------------------------------------


def _snap_to_grid(
    ideal_positions: dict[uuid.UUID, Point],
    mobile_ids: list[uuid.UUID],
    original_placements: dict[uuid.UUID, ComponentPlacement],
    fixed_obstacles: dict[uuid.UUID, tuple[ComponentPlacement, ComponentDefinition]],
    pads_top: set[Point],
    pads_bottom: set[Point],
    session: SpreadState,
) -> dict[uuid.UUID, ComponentPlacement]:
    """Map each ideal float position to the nearest collision-free board pad."""
    # Sort mobile components by net-degree (most connections first → anchor first).
    net_degree: dict[uuid.UUID, int] = {}
    for cid in mobile_ids:
        degree = 0
        for net in session.schematic.nets.values():
            comp_ids_in_net = {pr.component_id for pr in net.pins}
            if cid in comp_ids_in_net and len(comp_ids_in_net) > 1:
                degree += 1
        net_degree[cid] = degree

    ordered = sorted(mobile_ids, key=lambda c: -net_degree.get(c, 0))

    # Start collision map with immovable obstacles.
    collision_map: dict[uuid.UUID, tuple[ComponentPlacement, ComponentDefinition]] = dict(fixed_obstacles)

    result: dict[uuid.UUID, ComponentPlacement] = {}

    for cid in ordered:
        inst = session.instances.get(cid)
        if inst is None:
            result[cid] = original_placements[cid]
            continue
        defn = session.components.get(inst.definition_ref)
        if defn is None:
            result[cid] = original_placements[cid]
            continue

        orig_pl = original_placements[cid]
        target = ideal_positions[cid]

        board_pads = pads_top if orig_pl.side == "top" else pads_bottom
        sorted_pads = sorted(board_pads, key=lambda p: math.hypot(p.x - target.x, p.y - target.y))

        pl = _find_free_slot(
            cid, defn, sorted_pads, [orig_pl.rotation], orig_pl.side, collision_map
        )

        if pl is None:
            # No free slot — keep original position.
            new_pl = orig_pl.copy()
        else:
            # Preserve all per-pin customisations; only replace position.
            new_pl = ComponentPlacement(
                component_id=cid,
                position=pl.position,
                rotation=orig_pl.rotation,
                side=orig_pl.side,
                label_offset=orig_pl.label_offset,
                pin_offsets=dict(orig_pl.pin_offsets),
                shared_pins=orig_pl.shared_pins,
                vertical_flipped=orig_pl.vertical_flipped,
            )

        result[cid] = new_pl
        collision_map[cid] = (new_pl, defn)

    return result


# ---------------------------------------------------------------------------
# Net graph construction
# ---------------------------------------------------------------------------


def _build_net_graph(
    component_ids: set[uuid.UUID],
    schematic: Schematic,
) -> dict[uuid.UUID, dict[uuid.UUID, float]]:
    """Build weighted adjacency map for mobile components only."""
    graph: dict[uuid.UUID, dict[uuid.UUID, float]] = {cid: {} for cid in component_ids}
    for net in schematic.nets.values():
        members = [pr.component_id for pr in net.pins if pr.component_id in component_ids]
        for i, a in enumerate(members):
            for b in members[i + 1:]:
                if a == b:
                    continue  # skip self-loops (same component, different pins)
                graph[a][b] = graph[a].get(b, 0.0) + 1.0
                graph[b][a] = graph[b].get(a, 0.0) + 1.0
    return graph


def _anchor_forces_map(
    mobile_ids: set[uuid.UUID],
    fixed: dict[uuid.UUID, tuple[ComponentPlacement, ComponentDefinition]],
    session: SpreadState,
) -> dict[uuid.UUID, list[Point]]:
    """Return per-mobile-component lists of pull-points from fixed net-neighbors."""
    anchor_map: dict[uuid.UUID, list[Point]] = {cid: [] for cid in mobile_ids}
    for net in session.schematic.nets.values():
        mobile_in_net = [pr for pr in net.pins if pr.component_id in mobile_ids]
        fixed_in_net = [pr for pr in net.pins if pr.component_id in fixed]
        if not mobile_in_net or not fixed_in_net:
            continue
        for fpr in fixed_in_net:
            fpl, fdefn = fixed[fpr.component_id]
            try:
                pt = pin_world_position(fdefn.footprint, fpl, fpr.pin_number)
            except (IndexError, ValueError):
                continue
            for mpr in mobile_in_net:
                anchor_map[mpr.component_id].append(pt)
    return anchor_map


# ---------------------------------------------------------------------------
# Bbox radius helper
# ---------------------------------------------------------------------------


def _bbox_radius(
    comp_id: uuid.UUID,
    session: SpreadState,
    placement: ComponentPlacement,
) -> float:
    """Half-diagonal of the component's bounding box (RU)."""
    inst = session.instances.get(comp_id)
    if inst is None:
        return 0.5
    defn = session.components.get(inst.definition_ref)
    if defn is None:
        return 0.5
    bbox = component_bbox(defn.footprint, placement, defn.component_class.value)
    return math.hypot(bbox.width, bbox.height) / 2.0
