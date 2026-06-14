# SPDX-License-Identifier: MIT
"""Reach info derived from a Package — the budulec that replaces bendable_pins.

Consumers (place_tool ring overlay, DRC overlap-skip) historically read
``PinBendSpec``; this computes the same information from a ``Package`` instead,
with one deliberate simplification (no backward compat): the reach ring is
centred on the PIN's default position (our model), not on the component origin.

Pure domain. ``bend`` is per package; the resulting reach applies to every
movable pin around its own default position.
"""

from __future__ import annotations

from dataclasses import dataclass

from protoboard.domain.components.package.shapes import Package
from protoboard.domain.components.package.solver import (
    MountOrientation,
    OffsetRange,
)
from protoboard.geometry import Point


@dataclass(frozen=True, slots=True)
class PinReach:
    """Where a pin's lead may land: a euclidean ring measured from the ORIGIN.

    The component ORIGIN is the fixed reference point ``[0, 0]`` (= the default
    position of pin 1 = the rotation point). The origin does NOT move; what
    moves is the pin's bent lead relative to its default. ``default`` is the
    pin's home hole. ``ring`` is the allowed spacing from the origin (mm) — for
    bend ``[body_offset, body_offset+bend]``, for axial the solver's
    ``[r_min, r_max]``. ``is_movable`` is False when the ring is a single point
    (rigid — DIP, bend=0).
    """

    origin: Point
    default: Point
    ring: OffsetRange

    @property
    def is_movable(self) -> bool:
        return self.ring.r_max_mm > self.ring.r_min_mm

    def is_reachable(self, target: Point, *, raster_mm: float) -> bool:
        """Whether ``target`` (in holes) is a legal landing for this pin's lead.

        The ring is spacing from the fixed component origin, so distance is
        measured from the ORIGIN, in mm (holes × raster) to match ring units.
        """
        dx = (target.x - self.origin.x) * raster_mm
        dy = (target.y - self.origin.y) * raster_mm
        dist = (dx * dx + dy * dy) ** 0.5
        return self.ring.contains_mm(dist)


def pin_reaches(
    package: Package,
    *,
    orientation: MountOrientation = MountOrientation.HORIZONTAL,
) -> dict[int, PinReach]:
    """Reach info per pin number (1-based) for a placed package.

    The component ORIGIN is the fixed point ``[0, 0]`` (default position of pin
    1, the rotation point). The origin never moves; every pin — including pin 1
    — may have its lead bent within ``ring`` of the origin. ``bend``/solver are
    per package, so the ring is shared; each pin carries its own default hole.
    """
    positions = package.pin_positions(orientation=orientation)
    ring = package.board_offset_range(orientation=orientation)
    origin = Point(0.0, 0.0)
    out: dict[int, PinReach] = {}
    for idx, pos in enumerate(positions, start=1):
        out[idx] = PinReach(origin=origin, default=pos, ring=ring)
    return out


def is_pin_bendable(
    package: Package,
    pin_number: int,
    *,
    orientation: MountOrientation = MountOrientation.HORIZONTAL,
) -> bool:
    """Whether a pin can bend (non-zero slack) — replaces ``pin in bendable_pins``.

    A pin is bendable iff its reach ring is wider than a point. This covers both
    ``bend`` and the axial solver uniformly: the solver yields r_max > r_min when
    lead_length > 0, so movability falls out of the ring itself.
    """
    reach = pin_reaches(package, orientation=orientation).get(pin_number)
    return reach is not None and reach.is_movable
