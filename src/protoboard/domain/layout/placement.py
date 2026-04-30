# SPDX-License-Identifier: MIT
"""ComponentPlacement and pin_world_position."""

from __future__ import annotations

import uuid
from dataclasses import dataclass, field
from typing import Literal

from protoboard.domain.components.footprints import FootprintTemplate
from protoboard.geometry import Point

_VALID_ROTATIONS = frozenset({0, 90, 180, 270})


@dataclass(frozen=True, slots=True)
class ComponentPlacement:
    component_id: uuid.UUID
    position: Point
    rotation: int
    side: Literal["top", "bottom"]
    label_offset: Point = field(default_factory=lambda: Point(0.05, 0.05))

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


def pin_world_position(
    footprint: FootprintTemplate,
    placement: ComponentPlacement,
    pin_number: int,
) -> Point:
    """Return the absolute world position of the given pin (1-indexed)."""
    rel = footprint.pin_positions()[pin_number - 1]
    rotated = _rotate(rel, placement.rotation)
    return Point(
        placement.position.x + rotated.x,
        placement.position.y + rotated.y,
    )
