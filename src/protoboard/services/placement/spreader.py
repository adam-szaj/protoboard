# SPDX-License-Identifier: MIT
"""Grid-cell component spreader.

Two-phase algorithm:
  Phase 1 — Grid-cell assignment.  The board is divided into N equal cells
             (one per mobile component).  Components are assigned to cells so
             that net-connected pairs land in adjacent or nearby cells.
             Assignment is done via a greedy BFS walk ordered by net-degree:
             the most-connected component is placed first at the cell nearest
             its current position; each subsequent component claims the nearest
             free cell to its already-assigned net-neighbours.  The centre of
             the assigned cell becomes the ideal target for Phase 2.

  Phase 2 — Greedy snap to discrete pad grid.  Each component's ideal float
             position is mapped to the nearest valid board pad with no body
             collisions, reusing _find_free_slot from auto_placer.

Why grid-cell assignment beats force-directed for uniform distribution:
  Force-directed has a well-known clustering problem — repulsion decays as
  1/d², so pairs far apart barely feel each other and naturally form groups.
  Grid cells give every component an exclusive zone by construction, so the
  result is always evenly distributed regardless of the number of components.

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
from protoboard.domain.layout.placement import ComponentPlacement
from protoboard.domain.layout.wire_utils import board_pads_for_side
from protoboard.domain.schematic.schematic import Schematic
from protoboard.geometry import Point, Rect
from protoboard.services.placement.auto_placer import _find_free_slot

_log = structlog.get_logger(__name__)


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
) -> SpreadResult:
    """Spread *component_ids* evenly using grid-cell assignment.

    The board is divided into N equal cells (one per mobile component).
    Net-connected components are assigned to adjacent cells.  The centre of
    each assigned cell becomes the snap target for Phase 2.

    Components NOT in *component_ids* that are already placed are treated as
    immovable obstacles during the snap phase.  Rotations and sides are never
    changed.

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
    mobile_set = set(mobile_ids)
    fixed: dict[uuid.UUID, tuple[ComponentPlacement, ComponentDefinition]] = {}
    for cid, pl in session.layout.placements.items():
        if cid in mobile_set:
            continue
        inst = session.instances.get(cid)
        defn = session.components.get(inst.definition_ref) if inst else None
        if defn is not None:
            fixed[cid] = (pl, defn)

    pads_top: set[Point] = board_pads_for_side(session.board, "top")
    pads_bottom: set[Point] = board_pads_for_side(session.board, "bottom")
    board_bounds = session.board.bounds

    net_graph = _build_net_graph(mobile_set, session.schematic)

    # Phase 1 — assign each component a unique grid cell.
    ideal_positions = _assign_grid_cells(
        mobile_ids=mobile_ids,
        net_graph=net_graph,
        current_positions={cid: session.layout.placements[cid].position for cid in mobile_ids},
        board_bounds=board_bounds,
    )

    # Phase 2 — snap to discrete pad grid.
    new_placements = _snap_to_grid(
        ideal_positions=ideal_positions,
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
# Phase 1 — Grid-cell assignment
# ---------------------------------------------------------------------------


def _assign_grid_cells(
    mobile_ids: list[uuid.UUID],
    net_graph: dict[uuid.UUID, dict[uuid.UUID, float]],
    current_positions: dict[uuid.UUID, Point],
    board_bounds: Rect,
) -> dict[uuid.UUID, Point]:
    """Assign each component a unique grid cell; return cell-centre targets.

    The board is divided into a grid with as many cells as there are mobile
    components.  Cells are laid out in a rectangle whose aspect ratio matches
    the board.

    Assignment strategy (greedy BFS by net-degree):
    1. Sort components by net-degree descending — most-connected goes first.
    2. The first component claims the cell nearest its current position.
    3. Each subsequent unassigned component claims the free cell nearest the
       centroid of already-assigned net-neighbours (gravity), falling back to
       the free cell nearest its own current position.
    """
    n = len(mobile_ids)

    # Compute grid dimensions matching board aspect ratio.
    aspect = board_bounds.width / max(board_bounds.height, 1e-6)
    cols = max(1, round(math.sqrt(n * aspect)))
    rows = max(1, math.ceil(n / cols))
    # Ensure we have at least n cells.
    while rows * cols < n:
        cols += 1

    cell_w = board_bounds.width / cols
    cell_h = board_bounds.height / rows

    # Pre-compute all cell centres (row-major).
    all_cells: list[Point] = []
    for r in range(rows):
        for c in range(cols):
            cx = board_bounds.x + (c + 0.5) * cell_w
            cy = board_bounds.y + (r + 0.5) * cell_h
            all_cells.append(Point(cx, cy))

    free_cells: list[Point] = list(all_cells[:n])  # only as many as components

    # Sort by net-degree (descending), tie-break by index for stability.
    net_degrees = {cid: sum(net_graph[cid].values()) for cid in mobile_ids}
    ordered = sorted(
        range(n),
        key=lambda i: (-net_degrees.get(mobile_ids[i], 0.0), i),
    )

    assigned_cell: dict[uuid.UUID, Point] = {}
    free_set: list[Point] = list(free_cells)  # mutable free-cell pool

    for idx in ordered:
        cid = mobile_ids[idx]

        # Gravity: centroid of cells already assigned to net-neighbours.
        neighbour_cells: list[Point] = []
        for nbr_id in net_graph.get(cid, {}):
            if nbr_id in assigned_cell:
                neighbour_cells.append(assigned_cell[nbr_id])

        if neighbour_cells:
            gx = sum(p.x for p in neighbour_cells) / len(neighbour_cells)
            gy = sum(p.y for p in neighbour_cells) / len(neighbour_cells)
            gravity = Point(gx, gy)
        else:
            gravity = current_positions[cid]

        # Claim the free cell nearest the gravity point.
        best = min(free_set, key=lambda p: math.hypot(p.x - gravity.x, p.y - gravity.y))
        assigned_cell[cid] = best
        free_set.remove(best)

    return assigned_cell


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


