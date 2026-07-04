# SPDX-License-Identifier: MIT
"""Adapter: Package → Footprint.

The least-invasive bridge into the live pipeline. A ``Package`` already exposes
``pin_positions`` and ``pin_count``; this wraps it as a ``Footprint`` so
every footprint consumer works unchanged, with geometry driven by the
parametric package model (``ComponentDefinition.default_footprint()`` and
``ComponentInstance.resolved_footprint()`` both return one of these).

``Footprint.pin_positions()`` takes no arguments, so the mount
orientation is fixed at construction (default horizontal).
"""

from __future__ import annotations

from dataclasses import dataclass

from protoboard.domain.components.footprints import Footprint
from protoboard.domain.components.package.shapes import Package
from protoboard.domain.components.package.solver import MountOrientation
from protoboard.geometry import Point, Transform2D


@dataclass(frozen=True)
class PackageFootprint(Footprint):
    """Expose a Package as a Footprint (pin geometry only).

    Note: ``Footprint`` convention is pin 1 at (0, 0) with non-negative
    coords for the unrotated footprint. ``Package.pin_positions`` already places
    pin 1 at (0, 0); axial body=0 lays the second pin along +x, so coords stay
    non-negative for the worked cases.
    """

    package: Package
    orientation: MountOrientation = MountOrientation.HORIZONTAL

    def pin_positions(self) -> list[Point]:
        return self.package.pin_positions(orientation=self.orientation)

    def pin_count(self) -> int:
        return self.package.pin_count()

    def rotate(self, cw: int) -> None:
        # A package-derived footprint has no independent rotation: its pin
        # geometry is driven by the package's own mount orientation, not by
        # rotating a flat offset list. Rotation is a no-op here.
        return None

    def flip_transform(self) -> Transform2D:
        # Defer to the package: it owns how the part physically flips (a future
        # explicit axis flows through here without the pipeline knowing).
        return self.package.flip_transform()
