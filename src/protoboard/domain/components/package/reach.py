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
    """Where a pin may land: a euclidean ring measured from the ANCHOR (pin 1).

    ``anchor`` is pin 1's hole (the rotation/reference point). ``default`` is the
    pin's home hole. ``ring`` is the allowed pin-to-anchor spacing (mm), i.e.
    ``board_offset_range`` — for bend it is ``[body_offset, body_offset+bend]``,
    for axial it is the solver's ``[r_min, r_max]``. ``is_movable`` is False when
    the ring is a single point (rigid — DIP, bend=0).
    """

    anchor: Point
    default: Point
    ring: OffsetRange

    @property
    def is_movable(self) -> bool:
        return self.ring.r_max_mm > self.ring.r_min_mm

    def is_reachable(self, target: Point, *, raster_mm: float) -> bool:
        """Whether ``target`` (in holes) is a legal landing for this pin.

        The ring is anchor-to-pin spacing, so distance is measured from the
        ANCHOR (pin 1), in mm (holes × raster) to match the ring units.
        """
        dx = (target.x - self.anchor.x) * raster_mm
        dy = (target.y - self.anchor.y) * raster_mm
        dist = (dx * dx + dy * dy) ** 0.5
        return self.ring.contains_mm(dist)


def pin_reaches(
    package: Package,
    *,
    orientation: MountOrientation = MountOrientation.HORIZONTAL,
) -> dict[int, PinReach]:
    """Reach info per pin number (1-based) for a placed package.

    Pin 1 is the anchor (origin) — it does not move, so it is omitted unless the
    package's range makes pin 1 movable too. For the worked cases the ring is the
    same for every movable pin (``bend``/solver are per package); each pin's ring
    is centred on its own default position.
    """
    positions = package.pin_positions(orientation=orientation)
    ring = package.board_offset_range(orientation=orientation)
    anchor = positions[0]  # pin 1
    out: dict[int, PinReach] = {}
    for idx, pos in enumerate(positions, start=1):
        if idx == 1:
            continue  # pin 1 is the anchor
        out[idx] = PinReach(anchor=anchor, default=pos, ring=ring)
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
