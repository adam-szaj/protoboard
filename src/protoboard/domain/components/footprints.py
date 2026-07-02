# SPDX-License-Identifier: MIT
"""Footprint templates — generate pin positions on the raster grid.

Convention: pin 1 lies at (0, 0); other pins have non-negative coordinates
for an unrotated footprint. Rotation is applied later by the Layout layer.
"""

from __future__ import annotations

from abc import ABC, abstractmethod
from dataclasses import dataclass
from typing import override

from protoboard.geometry import (
    Point,
    PointList,
    Rotate2D,
    Transform2D,
    Vector,
    VectorList,
    flip_reflection_from_pins,
    make_points_from_offsets,
    transform_vectors,
)


class Footprint(ABC):
    """Abstract base for footprint generators.

    Subclasses implement `pin_positions` (a list of `Point`s, indexed by
    pin number minus one) and `pin_count`.
    """

    @abstractmethod
    def pin_positions(self) -> list[Point]:
        ...

    @abstractmethod
    def pin_count(self) -> int:
        ...

    @abstractmethod
    def rotate(self, cw: int):
        ...

    def flip_transform(self) -> Transform2D:
        """Reflection (local frame) for a physical board-side flip.

        A flip is a 180° rotation about an axis in the board plane; its 2-D
        projection is a reflection whose axis runs ALONG the pin row, so the
        pins stay on their pads.  The axis is a property of the geometry and
        never depends on which side the part sits on.

        The default derives it from this footprint's pins; a package-backed
        footprint overrides to use the package's axis (which may be declared
        explicitly) — see ``PackageFootprint``.
        """
        return flip_reflection_from_pins(self.pin_positions())


@dataclass(frozen=False, slots=True)
class GenericFootprint(Footprint):
    # position: Point
    offsets: VectorList

    @override
    def pin_positions(self) -> PointList:
        return make_points_from_offsets(Point(), self.offsets)

    @override
    def pin_count(self) -> int:
        return len(self.offsets) + 1

    @override
    def rotate(self, cw: int):
        rotation = Rotate2D(cw)
        self.offsets = transform_vectors(rotation, self.offsets)


def DIL(n: int, pitch: float = 1.0, rows_spacing: float = 3.0) -> Footprint:
    """DIP-N package (must have an even pin count)."""
    if n <= 0 or n % 2 != 0:
        raise ValueError(f"DIP requires a positive even pin count; got {n}")
    m = (n // 2) - 1
    offsets = [Vector(0.0, pitch)] * m + [Vector(rows_spacing)
                                          ] + [Vector(0.0, -pitch)] * m
    return GenericFootprint(offsets)


def SIL(n: int, pitch: float = 1.0) -> Footprint:
    """SIL-N (single in-line row of N pins)."""
    return GenericFootprint([Vector(0.0, pitch)] * (n - 1))


# Mechanical constants — same pin geometry, different package types.
TO92: Footprint = SIL(3)
TO220: Footprint = SIL(3)

__all__ = [
    "DIL",
    "SIL",
    "TO92",
    "TO220",
    "Footprint",
    "GenericFootprint",
]
