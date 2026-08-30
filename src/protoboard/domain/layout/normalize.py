# SPDX-License-Identifier: MIT
"""Half-grid normalization for legacy layouts.

Pad centers on every supported board grid are at ``(col + 0.5, row + 0.5)``.
Component placements (``placement.position`` is pin-1 world) and wire path
points should always land on that half-grid.  Old saves can carry tiny
fractional offsets (e.g. ``y = 3.503`` instead of ``3.5``) — a leftover from
pre-snap-fix days — which the connectivity graph indexes as distinct nodes
and so a wire drawn between such a pin and the matching board pad is not
seen as connected.

The connectivity graph itself uses a ``_SNAP_TOL`` of ±0.005 to absorb this
in lookups, but mutating the data once on load cleans up the legacy files
permanently so the next save persists the corrected positions.

This module is pure-domain — no Qt, no YAML, no I/O.
"""

from __future__ import annotations

import math
import uuid
from dataclasses import dataclass

from protoboard.domain.layout.layout import Layout
from protoboard.geometry import Point

_MIN_DELTA = 1e-6  # ignore floating-point noise below this
_MAX_SNAP_DELTA = 0.5  # don't snap something genuinely off-grid (≥ ±0.5)


def _snap_to_half_grid(v: float) -> float:
    """Snap *v* to the nearest ``n + 0.5`` value."""
    return math.floor(v) + 0.5


def _snap_point(p: Point) -> Point:
    return Point(_snap_to_half_grid(p.x), _snap_to_half_grid(p.y))


def _needs_snap(p: Point, snapped: Point) -> bool:
    dx = abs(p.x - snapped.x)
    dy = abs(p.y - snapped.y)
    if dx < _MIN_DELTA and dy < _MIN_DELTA:
        return False  # already on-grid (or noise too small to matter)
    if dx > _MAX_SNAP_DELTA or dy > _MAX_SNAP_DELTA:
        return False  # too far — this is user data, not a rounding artifact
    return True


@dataclass(frozen=True)
class PlacementCorrection:
    component_id: uuid.UUID
    old: Point
    new: Point


@dataclass(frozen=True)
class WireCorrection:
    wire_id: uuid.UUID
    point_index: int
    old: Point
    new: Point


@dataclass(frozen=True)
class NormalizeReport:
    placements: tuple[PlacementCorrection, ...]
    wires: tuple[WireCorrection, ...]

    @property
    def any_corrections(self) -> bool:
        return bool(self.placements or self.wires)

    @property
    def count(self) -> int:
        return len(self.placements) + len(self.wires)


def normalize_layout_positions(layout: Layout) -> NormalizeReport:
    """Snap placement.position and wire path points to the half-grid in place.

    Returns a :class:`NormalizeReport` describing every correction applied.
    Items further than ±0.5 from the nearest half-grid intersection are left
    alone — those are either genuinely off-grid (user error) or simply not
    fractional-offset artifacts and re-snapping would silently change the
    user's design.

    The function never raises.  Safe to call multiple times — a second pass
    over already-snapped data returns an empty report.
    """
    p_corr: list[PlacementCorrection] = []
    w_corr: list[WireCorrection] = []

    for comp_id, pl in layout.placements.items():
        snapped = _snap_point(pl.position)
        if _needs_snap(pl.position, snapped):
            p_corr.append(
                PlacementCorrection(
                    component_id=comp_id,
                    old=pl.position,
                    new=snapped,
                )
            )
            pl.position = snapped

    for wire in layout.wires:
        new_path: list[Point] = []
        for idx, pt in enumerate(wire.path):
            snapped = _snap_point(pt)
            if _needs_snap(pt, snapped):
                w_corr.append(
                    WireCorrection(
                        wire_id=wire.id,
                        point_index=idx,
                        old=pt,
                        new=snapped,
                    )
                )
                new_path.append(snapped)
            else:
                new_path.append(pt)
        # Drop consecutive duplicates that the snap may have introduced.
        deduped: list[Point] = []
        for pt in new_path:
            if not deduped or pt != deduped[-1]:
                deduped.append(pt)
        if len(deduped) >= 2:
            wire.path = deduped

    return NormalizeReport(
        placements=tuple(p_corr),
        wires=tuple(w_corr),
    )


__all__ = [
    "PlacementCorrection",
    "WireCorrection",
    "NormalizeReport",
    "normalize_layout_positions",
]
