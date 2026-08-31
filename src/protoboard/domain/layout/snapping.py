"""Pure candidate selection for board-coordinate snapping."""

from __future__ import annotations

import math
from dataclasses import dataclass

from protoboard.geometry import Point


@dataclass(frozen=True, slots=True)
class SnapRequest:
    """Cursor position and already-authorized nearby snap candidates."""

    scene_point: Point
    pad_candidates: tuple[Point, ...] = ()
    pin_candidates: tuple[Point, ...] = ()


def snap_point(request: SnapRequest) -> Point:
    """Choose the nearest candidate, preferring pads on equal distances."""
    sx, sy = request.scene_point.x, request.scene_point.y
    best: Point | None = None
    best_distance = math.inf
    for candidate in (*request.pad_candidates, *request.pin_candidates):
        dx = sx - candidate.x
        dy = sy - candidate.y
        distance = dx * dx + dy * dy
        if distance < best_distance:
            best = candidate
            best_distance = distance
    if best is not None:
        return best
    return Point(math.floor(sx) + 0.5, math.floor(sy) + 0.5)


__all__ = ["SnapRequest", "snap_point"]
