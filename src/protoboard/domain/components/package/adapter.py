# SPDX-License-Identifier: MIT
"""Adapter: Package → FootprintTemplate.

The least-invasive bridge into the live pipeline. A ``Package`` already exposes
``pin_positions`` and ``pin_count``; this wraps it as a ``FootprintTemplate`` so
anything that consumes ``ComponentDefinition.footprint`` works unchanged, with
geometry now driven by the parametric package model.

``FootprintTemplate.pin_positions()`` takes no arguments, so the mount
orientation is fixed at construction (default horizontal).
"""

from __future__ import annotations

from dataclasses import dataclass

from protoboard.domain.components.footprints import FootprintTemplate
from protoboard.domain.components.package.shapes import Package
from protoboard.domain.components.package.solver import MountOrientation
from protoboard.geometry import Point


@dataclass(frozen=True)
class PackageFootprint(FootprintTemplate):
    """Expose a Package as a FootprintTemplate (pin geometry only).

    Note: ``FootprintTemplate`` convention is pin 1 at (0, 0) with non-negative
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
