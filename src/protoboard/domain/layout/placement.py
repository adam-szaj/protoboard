# SPDX-License-Identifier: MIT
"""ComponentPlacement and pin_world_position."""

from __future__ import annotations

import uuid
from dataclasses import dataclass, field
from typing import Literal

from protoboard.domain.components.footprints import FootprintTemplate
from protoboard.geometry import Point, Rect

_VALID_ROTATIONS = frozenset({0, 90, 180, 270})


@dataclass
class ComponentPlacement:
    component_id: uuid.UUID
    position: Point
    rotation: int
    side: Literal["top", "bottom"]
    label_offset: Point = field(default_factory=lambda: Point(0.05, 0.05))
    pin_offsets: dict[int, Point] = field(default_factory=dict)
    # Pin numbers (1-based) intentionally sharing a pad with another component.
    shared_pins: frozenset[int] = field(default_factory=frozenset)
    vertical_flipped: bool = False

    def __hash__(self) -> int:
        return hash(self.component_id)

    def __eq__(self, other: object) -> bool:
        if not isinstance(other, ComponentPlacement):
            return NotImplemented
        return self.component_id == other.component_id

    def copy(self) -> ComponentPlacement:
        return ComponentPlacement(
            component_id=self.component_id,
            position=self.position,
            rotation=self.rotation,
            side=self.side,
            label_offset=self.label_offset,
            pin_offsets=dict(self.pin_offsets),
            shared_pins=self.shared_pins,
            vertical_flipped=self.vertical_flipped,
        )

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


def unrotate(p: Point, rotation: int) -> Point:
    """Reverse a clockwise rotation — equivalent to rotating by -rotation."""
    return _rotate(p, (360 - rotation) % 360)


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


def flip_component(
    footprint: FootprintTemplate,
    placement: ComponentPlacement,
) -> ComponentPlacement:
    """Swap pin1 and pin2 world positions; both pads are exchanged.

    Only valid for 2-pin footprints.  After flip:
      pin_world_position(fp, result, 1) == old pin2 world pos
      pin_world_position(fp, result, 2) == old pin1 world pos
    rotation is adjusted by +180° so the component geometry is preserved.
    Net assignments follow the pin numbers, so the electrical connections
    physically swap sides — which is the intended behaviour.
    """
    assert footprint.pin_count() == 2, "flip_component requires a 2-pin footprint"
    p1_world = pin_world_position(footprint, placement, 1)
    p2_world = pin_world_position(footprint, placement, 2)
    new_rotation = (placement.rotation + 180) % 360
    fp_pos2 = footprint.pin_positions()[1]
    # Solve: new_pos + rotate(fp_pos2 + new_off2, new_rotation) == p1_world
    delta = unrotate(
        Point(p1_world.x - p2_world.x, p1_world.y - p2_world.y),
        new_rotation,
    )
    new_off2 = Point(delta.x - fp_pos2.x, delta.y - fp_pos2.y)
    new_pl = placement.copy()
    new_pl.position = p2_world
    new_pl.rotation = new_rotation
    new_pl.pin_offsets = dict(placement.pin_offsets)
    new_pl.pin_offsets[2] = new_off2
    new_pl.pin_offsets.pop(1, None)
    new_pl.vertical_flipped = not placement.vertical_flipped
    return new_pl


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
    # Components mounted on the bottom side are physically flipped — their
    # local X axis is mirrored relative to the top view.  We apply this flip
    # before rotation so that rotation=0 on bottom means "same anchor point,
    # mirrored geometry", matching real-world PCB conventions.
    if placement.side == "bottom":
        rel = Point(-rel.x, rel.y)
    rotated = _rotate(rel, placement.rotation)
    return Point(
        placement.position.x + rotated.x,
        placement.position.y + rotated.y,
    )
