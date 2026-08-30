# SPDX-License-Identifier: MIT
"""Greedy auto-placer with net-gravity heuristic.

Algorithm (per component to place):
  1. Collect *gravity points* — world positions of already-placed pins that
     share a net with this component.
  2. Compute the centroid of those points (or None when there are none).
  3. For each candidate rotation (preferred first):
     a. Sort candidate pad positions by Euclidean distance from the centroid
        (fallback: column-major so components spread horizontally).
     b. Pick the first candidate that has no body collision with already-placed
        components.
  4. If no candidate fits on the board in any rotation, fall back to an
     extended virtual grid outside board boundaries.

Components are processed in decreasing order of gravity strength — those
with many connections to already-placed components go first and act as
anchors for later ones.

Rotation selection
------------------
SIL footprints in rotation=0 are vertical (height = n-1 rastry), which
wastes vertical space on a perfboard.  The placer tries rotations in the
order that minimises the component's larger dimension relative to the board,
preferring the "landscape" orientation.
"""

from __future__ import annotations

import uuid
from dataclasses import dataclass, field
from typing import Protocol

import structlog

from protoboard.domain.board.board import Board
from protoboard.domain.components.definition import ComponentDefinition
from protoboard.domain.components.instance import ComponentInstance
from protoboard.domain.layout.layout import Layout
from protoboard.domain.layout.pad_conflict import (
    footprint_of,
    resolved_footprint_or_default,
)
from protoboard.domain.layout.placement import (
    ComponentPlacement,
    component_bbox,
    pin_world_position,
)
from protoboard.domain.layout.wire_utils import board_pads_for_side
from protoboard.domain.schematic.schematic import Schematic
from protoboard.geometry import Point, Rect

_log = structlog.get_logger(__name__)

# ---------------------------------------------------------------------------
# Public types
# ---------------------------------------------------------------------------


@dataclass
class AutoPlaceResult:
    """Result of :func:`auto_place`."""

    placements: list[ComponentPlacement] = field(default_factory=list)
    outside_board: list[uuid.UUID] = field(default_factory=list)
    skipped: list[uuid.UUID] = field(default_factory=list)


class AutoPlaceState(Protocol):
    """Minimal session interface required by the auto-placer (no Qt)."""

    board: Board
    layout: Layout
    schematic: Schematic
    instances: dict[uuid.UUID, ComponentInstance]
    components: dict[str, ComponentDefinition]


# ---------------------------------------------------------------------------
# Public entry point
# ---------------------------------------------------------------------------


def auto_place(
    instances_to_place: list[ComponentInstance],
    session: AutoPlaceState,
    side: str = "top",
) -> AutoPlaceResult:
    """Greedily place *instances_to_place* on the board.

    Rotation is chosen automatically per component to minimise wasted space.

    Returns an :class:`AutoPlaceResult` with:
    - ``placements``: new ComponentPlacement objects ready to be committed.
    - ``outside_board``: component UUIDs placed outside board bounds.
    - ``skipped``: component UUIDs that could not be placed at all (no defn,
      zero pins, or truly no space even in extended grid).
    """
    board_pads: set[Point] = board_pads_for_side(session.board, side)
    board_bounds: Rect = session.board.bounds

    # Column-major ordering: fill left-to-right within each row, but try each
    # column top-to-bottom first — produces a compact grid without clustering.
    all_pads: list[Point] = sorted(board_pads, key=lambda p: (p.x, p.y))

    # Existing placed components: comp_id → (placement, defn, inst). The bound
    # instance travels with each entry so geometry resolves the chosen variant.
    existing: dict[
        uuid.UUID,
        tuple[ComponentPlacement, ComponentDefinition, ComponentInstance],
    ] = {}
    for comp_id, existing_placement in session.layout.placements.items():
        inst = session.instances.get(comp_id)
        defn = session.components.get(inst.definition_ref) if inst else None
        if defn is not None and inst is not None:
            existing[comp_id] = (existing_placement, defn, inst)

    # Pre-sort: process components with the most gravity connections first.
    ordered = _sort_by_gravity(instances_to_place, session, existing)

    result = AutoPlaceResult()

    for inst in ordered:
        defn = session.components.get(inst.definition_ref)
        if defn is None:
            _log.warning(
                "auto_place.skip_no_defn",
                refdes=inst.refdes,
                definition_ref=inst.definition_ref,
            )
            result.skipped.append(inst.id)
            continue
        if resolved_footprint_or_default(defn, inst).pin_count() == 0:
            _log.warning(
                "auto_place.skip_zero_pins",
                refdes=inst.refdes,
                definition_ref=inst.definition_ref,
            )
            result.skipped.append(inst.id)
            continue

        grav_pts = _gravity_points(inst, session, existing)
        center = _centroid(grav_pts)
        sorted_pads = _sort_candidates(all_pads, center)

        rotations = _preferred_rotations(defn, inst, board_bounds)
        pl = _find_free_slot(
            inst.id, defn, inst, sorted_pads, rotations, side, existing
        )
        on_board = True

        if pl is None:
            # Try extended virtual grid outside board bounds.
            extra = _extended_candidates(board_pads, board_bounds)
            extra_sorted = _sort_candidates(extra, center)
            pl = _find_free_slot(
                inst.id, defn, inst, extra_sorted, rotations, side, existing
            )
            on_board = False

        if pl is None:
            _log.warning(
                "auto_place.no_free_slot",
                refdes=inst.refdes,
                definition_ref=inst.definition_ref,
            )
            result.skipped.append(inst.id)
            continue

        result.placements.append(pl)
        existing[inst.id] = (pl, defn, inst)
        if not on_board:
            result.outside_board.append(inst.id)

    return result


# ---------------------------------------------------------------------------
# Rotation selection
# ---------------------------------------------------------------------------


def _preferred_rotations(
    defn: ComponentDefinition,
    inst: ComponentInstance,
    board_bounds: Rect,
) -> list[int]:
    """Return rotations to try, best first.

    Heuristic: prefer the orientation whose longer axis aligns with the
    board's longer axis.  For most SIL components this means rotation=90
    (horizontal) on a wider-than-tall board, which compacts rows nicely.
    DIP footprints are symmetric and default to 0.
    """
    pin_positions = footprint_of(defn, inst).pin_positions()
    if len(pin_positions) < 2:
        return [0, 90, 180, 270]

    xs = [p.x for p in pin_positions]
    ys = [p.y for p in pin_positions]
    fp_width = max(xs) - min(xs)  # extent in local X (rotation=0)
    fp_height = max(ys) - min(ys)  # extent in local Y (rotation=0)

    board_landscape = board_bounds.lx >= board_bounds.ly

    if fp_width == fp_height:
        # Symmetric (e.g. single-pin, square DIP) — all rotations equivalent
        return [0]

    if fp_height > fp_width:
        # Component is taller than wide in rotation=0 (typical SIL).
        # rotation=90 makes it wider than tall → prefer 90 on landscape board.
        if board_landscape:
            return [90, 0, 270, 180]
        else:
            return [0, 90, 180, 270]
    else:
        # Component is already wider than tall in rotation=0.
        if board_landscape:
            return [0, 90, 180, 270]
        else:
            return [90, 0, 270, 180]


# ---------------------------------------------------------------------------
# Gravity helpers
# ---------------------------------------------------------------------------


def _gravity_points(
    inst: ComponentInstance,
    session: AutoPlaceState,
    current_placements: dict[
        uuid.UUID,
        tuple[ComponentPlacement, ComponentDefinition, ComponentInstance],
    ],
) -> list[Point]:
    """Return world positions of all already-placed pins sharing a net with *inst*."""
    pts: list[Point] = []
    for net in session.schematic.nets.values():
        if not any(pr.component_id == inst.id for pr in net.pins):
            continue
        for pr in net.pins:
            if pr.component_id == inst.id:
                continue
            entry = current_placements.get(pr.component_id)
            if entry is None:
                continue
            pl, defn, other_inst = entry
            try:
                pt = pin_world_position(
                    footprint_of(defn, other_inst), pl, pr.pin_number
                )
                pts.append(pt)
            except (IndexError, ValueError):
                pass
    return pts


def _centroid(points: list[Point]) -> Point | None:
    if not points:
        return None
    return Point(
        sum(p.x for p in points) / len(points),
        sum(p.y for p in points) / len(points),
    )


def _sort_by_gravity(
    instances: list[ComponentInstance],
    session: AutoPlaceState,
    existing: dict[
        uuid.UUID,
        tuple[ComponentPlacement, ComponentDefinition, ComponentInstance],
    ],
) -> list[ComponentInstance]:
    """Sort descending by number of gravity connections to already-placed comps."""

    def _key(inst: ComponentInstance) -> int:
        return -len(_gravity_points(inst, session, existing))

    return sorted(instances, key=_key)


# ---------------------------------------------------------------------------
# Candidate generation and sorting
# ---------------------------------------------------------------------------


def _sort_candidates(candidates: list[Point], center: Point | None) -> list[Point]:
    if center is None:
        return candidates  # already in column-major order
    return sorted(candidates, key=lambda p: (p - center).norm())


def _extended_candidates(board_pads: set[Point], board_bounds: Rect) -> list[Point]:
    """Generate a virtual grid outside board boundaries (same 1.0 RU step)."""
    margin_x = max(10, int(board_bounds.lx * 0.5))
    margin_y = max(10, int(board_bounds.ly * 0.5))
    x_min = int(board_bounds.x) - margin_x
    x_max = int(board_bounds.x + board_bounds.lx) + margin_x
    y_min = int(board_bounds.y) - margin_y
    y_max = int(board_bounds.y + board_bounds.ly) + margin_y
    extra: list[Point] = []
    for col in range(x_min, x_max + 1):
        for row in range(y_min, y_max + 1):
            pt = Point(float(col) + 0.5, float(row) + 0.5)
            if pt not in board_pads:
                extra.append(pt)
    return extra


# ---------------------------------------------------------------------------
# Collision detection
# ---------------------------------------------------------------------------


def _has_collision(
    new_pl: ComponentPlacement,
    new_defn: ComponentDefinition,
    new_inst: ComponentInstance,
    existing: dict[
        uuid.UUID,
        tuple[ComponentPlacement, ComponentDefinition, ComponentInstance],
    ],
    extra_gap: float = 0.0,
) -> bool:
    """True when *new_pl* body-overlaps any existing placement.

    *extra_gap* enforces a minimum clearance between component bounding boxes:
    the new component's bbox is inflated by *extra_gap* on all sides before the
    intersection test.  This makes the gap between any two bbox edges at least
    *extra_gap* raster units, regardless of each component's individual body
    margin (rigid vs. flexible leads).
    """
    raw_new = component_bbox(
        footprint_of(new_defn, new_inst), new_pl, new_defn.component_class
    )
    new_bbox = Rect.of(
        x=raw_new.x - extra_gap,
        y=raw_new.y - extra_gap,
        lx=raw_new.lx + 2 * extra_gap,
        ly=raw_new.ly + 2 * extra_gap,
    )
    for other_pl, other_defn, other_inst in existing.values():
        other_bbox = component_bbox(
            footprint_of(other_defn, other_inst),
            other_pl,
            other_defn.component_class,
        )
        if new_bbox.intersects(other_bbox):
            return True
    return False


# ---------------------------------------------------------------------------
# Slot search
# ---------------------------------------------------------------------------


def _find_free_slot(
    comp_id: uuid.UUID,
    defn: ComponentDefinition,
    inst: ComponentInstance,
    candidates: list[Point],
    rotations: list[int],
    side: str,
    existing: dict[
        uuid.UUID,
        tuple[ComponentPlacement, ComponentDefinition, ComponentInstance],
    ],
    extra_gap: float = 0.0,
) -> ComponentPlacement | None:
    """Try each candidate position with each rotation; return first free slot.

    *extra_gap* is forwarded to :func:`_has_collision` to enforce a minimum
    clearance beyond the component body margins.
    """
    for pt in candidates:
        for rot in rotations:
            pl = ComponentPlacement(
                component_id=comp_id,
                position=pt,
                rotation=rot,
                side=side,  # type: ignore[arg-type]
            )
            if not _has_collision(pl, defn, inst, existing, extra_gap=extra_gap):
                return pl
    return None
