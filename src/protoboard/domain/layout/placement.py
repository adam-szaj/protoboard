# SPDX-License-Identifier: MIT
"""ComponentPlacement and pin_world_position."""

from __future__ import annotations

import uuid
from dataclasses import dataclass, field
from typing import Literal

from protoboard.domain.components.footprints import FootprintTemplate
from protoboard.geometry import Point, Rect

# Component classes whose leads can be bent freely — only the pads matter for
# collision/selection purposes, not the floating body between them.
_FLEXIBLE_LEAD_CLASSES: frozenset[str] = frozenset({
    "resistor", "capacitor_polar", "capacitor_nonpolar", "inductor",
    "diode", "diode_zener", "diode_led",
    "transistor_npn", "transistor_pnp",
    "transistor_nmos", "transistor_pmos",
    "transistor_njfet", "transistor_pjfet",
    "fuse",
})

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


_BODY_MARGIN = 0.5      # raster units — rigid packages (DIP, SIL, connectors)
_PAD_MARGIN  = 0.1      # raster units — flexible-lead parts (axial, TO-92, …)


def component_bbox(
    footprint: FootprintTemplate,
    placement: ComponentPlacement,
    component_class: str = "",
) -> Rect:
    """Return the axis-aligned bounding box of the component body.

    For components with flexible leads (resistors, diodes, transistors, …)
    only the pad positions matter — the body floats above the board and does
    not block neighbouring components.  A small _PAD_MARGIN is used instead
    of _BODY_MARGIN so that 45° placements don't reserve an oversized area.
    """
    positions = [
        pin_world_position(footprint, placement, n + 1)
        for n in range(footprint.pin_count())
    ]
    xs = [p.x for p in positions]
    ys = [p.y for p in positions]
    min_x, max_x = min(xs), max(xs)
    min_y, max_y = min(ys), max(ys)
    m = _PAD_MARGIN if component_class in _FLEXIBLE_LEAD_CLASSES else _BODY_MARGIN
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
    """Swap pin1 and pin2 local positions via pin_offsets.

    Only valid for 2-pin footprints.  After flip:
      pin_world_position(fp, result, 1) == old pin2 world pos
      pin_world_position(fp, result, 2) == old pin1 world pos

    placement.position and rotation are untouched — the anchor (local origin)
    stays fixed, and only the pin_offsets are updated so that each pin lands
    on the other pin's previous local position.
    """
    assert footprint.pin_count() == 2, "flip_component requires a 2-pin footprint"
    fp1, fp2 = footprint.pin_positions()[0], footprint.pin_positions()[1]
    off1 = placement.pin_offsets.get(1, Point(0.0, 0.0))
    off2 = placement.pin_offsets.get(2, Point(0.0, 0.0))
    # local position of each pin = fp_pos + offset
    loc1 = Point(fp1.x + off1.x, fp1.y + off1.y)
    loc2 = Point(fp2.x + off2.x, fp2.y + off2.y)
    # new offset for pin n = (other pin's old local pos) - fp_pos[n]
    new_off1 = Point(loc2.x - fp1.x, loc2.y - fp1.y)
    new_off2 = Point(loc1.x - fp2.x, loc1.y - fp2.y)

    new_pl = placement.copy()
    new_pl.pin_offsets = dict(placement.pin_offsets)
    new_pl.pin_offsets[1] = new_off1
    new_pl.pin_offsets[2] = new_off2
    new_pl.vertical_flipped = not placement.vertical_flipped
    return new_pl


def pin_world_position(
    footprint: FootprintTemplate,
    placement: ComponentPlacement,
    pin_number: int,
) -> Point:
    """Return the absolute world position of the given pin (1-indexed).

    ``placement.position`` is the world coordinate of the footprint origin
    (pin 1 at local (0,0), *without* any pin_offset).  Rotation is always
    around that origin, so changing rotation never moves the anchor.

    Pipeline: local_pos = fp_pos + pin_offset → X-mirror (if bottom) →
    clockwise rotation → + placement.position.
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
