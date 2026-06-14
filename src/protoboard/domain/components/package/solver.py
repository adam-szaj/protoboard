# SPDX-License-Identifier: MIT
"""Axial board-offset solver and the OffsetRange type (pure domain).

For axial components with ``body_offset == 0`` the on-board pin spacing is not
fixed — we compute the minimal spacing from geometry (see
``docs/footprints/spec.md``, section "Solver"). Linear approximation; the
minimum bend radius (3·lead_r) is added as slack.
"""

from __future__ import annotations

from dataclasses import dataclass
from enum import StrEnum

from protoboard.services.debug_trace import traced

# Canonical raster: 2.54 mm / 0.1" is represented internally as 2.5 mm.
RASTER_MM = 2.5


class MountOrientation(StrEnum):
    """Mount orientation — instance state, not a package property.

    Axial body=0 yields a different board-offset range per orientation.
    """

    HORIZONTAL = "horizontal"
    VERTICAL = "vertical"


@dataclass(frozen=True, slots=True)
class OffsetRange:
    """Allowed pin spacing as a euclidean ring ``[r_min, r_max]`` in mm.

    A pin may land in any hole (integer dX, dY) with
    ``r_min <= sqrt(dX^2 + dY^2) <= r_max``. "Snap" means hitting a hole, NOT
    rounding direction to an axis.
    """

    r_min_mm: float
    r_max_mm: float

    def __post_init__(self) -> None:
        if self.r_min_mm < 0:
            raise ValueError(f"r_min_mm must be >= 0; got {self.r_min_mm}")
        if self.r_max_mm < self.r_min_mm:
            raise ValueError(
                f"r_max_mm ({self.r_max_mm}) must be >= r_min_mm ({self.r_min_mm})"
            )

    def contains_mm(self, distance_mm: float) -> bool:
        return self.r_min_mm <= distance_mm <= self.r_max_mm


@traced("axial_board_offset_range")
def axial_board_offset_range(
    *,
    length_mm: float,
    r_body_mm: float,
    lead_r_mm: float,
    lead_length_mm: float,
    body_eccentricity_mm: float = 0.0,
    orientation: MountOrientation = MountOrientation.HORIZONTAL,
) -> OffsetRange:
    """Compute the allowed board-offset ring for an axial (body=0) component.

    Horizontal (default): ``r_min = L + 6·lead_r``.
    Vertical:             ``r_min = r_body − eccentricity + 3·lead_r``.
    Both:                 ``r_max = r_min + 2·lead_length``.

    Eccentricity reduces the vertical minimum (lead exits nearer the edge);
    horizontally it is negligible and ignored.
    """
    if orientation is MountOrientation.HORIZONTAL:
        r_min = length_mm + 6.0 * lead_r_mm
    else:
        r_min = r_body_mm - body_eccentricity_mm + 3.0 * lead_r_mm
    r_min = max(r_min, 0.0)
    return OffsetRange(r_min, r_min + 2.0 * lead_length_mm)
