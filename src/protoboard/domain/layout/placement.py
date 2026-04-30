# SPDX-License-Identifier: MIT
"""ComponentPlacement and pin_world_position."""

from __future__ import annotations

import uuid
from dataclasses import dataclass, field
from typing import Literal

from protoboard.domain.components.footprints import FootprintTemplate
from protoboard.geometry import Point, Rect

_VALID_ROTATIONS = frozenset({0, 90, 180, 270})


@dataclass(frozen=True, slots=True)
class ComponentPlacement:
    component_id: uuid.UUID
    position: Point
    rotation: int
    side: Literal["top", "bottom"]
    label_offset: Point = field(default_factory=lambda: Point(0.05, 0.05))
    pin_offsets: dict[int, Point] = field(default_factory=dict)
    # Pin numbers (1-based) intentionally sharing a pad with another component.
    shared_pins: frozenset[int] = field(default_factory=frozenset)

    def __post_init__(self) -> None:
        if self.rotation not in _VALID_ROTATIONS:
            raise ValueError(
                f"rotation must be one of {sorted(_VALID_ROTATIONS)}; "
                f"got {self.rotation}"
            )
        if self.side not in ("top", "bottom"):
            raise ValueError(f"side must be 'top' or 'bottom'; got {self.side!r}")


def _rotate(p: Point, rotation: int) -> Point:
    """Rotate (x, y) clockwise by `rotation` degrees around the origin."""
    x, y = p.x, p.y
    if rotation == 0:
        return p
    if rotation == 90:
        return Point(y, -x)
    if rotation == 180:
        return Point(-x, -y)
    # 270
    return Point(-y, x)


_BODY_MARGIN = 0.5  # raster units added on each side of the pin bounding box


def component_bbox(
    footprint: FootprintTemplate,
    placement: ComponentPlacement,
) -> Rect:
    """Return the axis-aligned bounding box of the component body.

    Computed as the bounding box of all pin world positions, expanded by
    _BODY_MARGIN on every side.
    """
    positions = [
        pin_world_position(footprint, placement, n + 1)
        for n in range(footprint.pin_count())
    ]
    xs = [p.x for p in positions]
    ys = [p.y for p in positions]
    min_x, max_x = min(xs), max(xs)
    min_y, max_y = min(ys), max(ys)
    m = _BODY_MARGIN
    return Rect(
        x=min_x - m,
        y=min_y - m,
        width=(max_x - min_x) + 2 * m,
        height=(max_y - min_y) + 2 * m,
    )


def pin_world_position(
    footprint: FootprintTemplate,
    placement: ComponentPlacement,
    pin_number: int,
) -> Point:
    """Return the absolute world position of the given pin (1-indexed).

    If ``placement.pin_offsets`` contains an entry for *pin_number*, that
    offset is applied in the component's local frame *before* rotation so
    that bending a lead "outward" means the same regardless of component
    orientation.
    """
    rel = footprint.pin_positions()[pin_number - 1]
    off = placement.pin_offsets.get(pin_number)
    if off is not None:
        rel = Point(rel.x + off.x, rel.y + off.y)
    rotated = _rotate(rel, placement.rotation)
    return Point(
        placement.position.x + rotated.x,
        placement.position.y + rotated.y,
    )
