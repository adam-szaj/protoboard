# SPDX-License-Identifier: MIT
"""Geometric primitives in raster-unit coordinates.

All values are floats; negative coordinates are legal (boxes may have
negative margins). Sizes are non-negative; rectangles use a top-left
origin with half-open right/bottom edges (consistent with most 2D
graphics conventions).
"""

from __future__ import annotations

from dataclasses import dataclass


# Coordinate quantisation precision used throughout the codebase for
# float-equality on world coordinates.  Pad centres land on
# ``integer + 0.5`` half-unit positions, so rounding to 4 decimal places
# is safe (far below the half-unit granularity) and matches the
# precision other modules use for dict keys.
_COORD_ROUNDING_DIGITS = 4


def round_coord(v: float) -> float:
    """Round *v* to the canonical coordinate precision.

    Single source of truth for "make a float coordinate hashable / safe
    for equality".  Previously each of
    ``services/routing/autorouter._round``,
    ``domain/layout/pad_conflict._round``,
    ``gui/placement_conflict._round`` reimplemented the same
    ``round(v, 4)`` — three copies that drifted is the same bug-pattern
    that hit ``_pin_pos`` resolution before.
    """
    return round(v, _COORD_ROUNDING_DIGITS)


@dataclass(frozen=True, slots=True)
class Point:
    """A 2D point in raster units."""

    x: float
    y: float

    def __add__(self, other: Point) -> Point:
        return Point(self.x + other.x, self.y + other.y)

    def __sub__(self, other: Point) -> Point:
        return Point(self.x - other.x, self.y - other.y)


@dataclass(frozen=True, slots=True)
class Size:
    """Width and height in raster units. Both must be non-negative."""

    width: float
    height: float

    def __post_init__(self) -> None:
        if self.width < 0:
            raise ValueError(f"width must be non-negative; got {self.width}")
        if self.height < 0:
            raise ValueError(f"height must be non-negative; got {self.height}")

    @staticmethod
    def zero() -> Size:
        return Size(0.0, 0.0)


@dataclass(frozen=True, slots=True)
class Insets:
    """Per-side spacing (margins). Values may be negative."""

    top: float
    right: float
    bottom: float
    left: float

    @staticmethod
    def zero() -> Insets:
        return Insets(0.0, 0.0, 0.0, 0.0)

    @staticmethod
    def all(value: float) -> Insets:
        return Insets(value, value, value, value)

    @property
    def horizontal(self) -> float:
        return self.left + self.right

    @property
    def vertical(self) -> float:
        return self.top + self.bottom


@dataclass(frozen=True, slots=True)
class Rect:
    """Axis-aligned rectangle. (x, y) = top-left corner."""

    x: float
    y: float
    width: float
    height: float

    def __post_init__(self) -> None:
        if self.width < 0:
            raise ValueError(f"width must be non-negative; got {self.width}")
        if self.height < 0:
            raise ValueError(f"height must be non-negative; got {self.height}")

    @property
    def top_left(self) -> Point:
        return Point(self.x, self.y)

    @property
    def bottom_right(self) -> Point:
        return Point(self.x + self.width, self.y + self.height)

    @property
    def center(self) -> Point:
        return Point(self.x + self.width / 2.0, self.y + self.height / 2.0)

    def contains(self, point: Point) -> bool:
        """Half-open containment: top/left inclusive, bottom/right exclusive."""
        return (
            self.x <= point.x < self.x + self.width
            and self.y <= point.y < self.y + self.height
        )

    def intersects(self, other: Rect) -> bool:
        """Strict overlap; touching edges do not count as intersection."""
        return (
            self.x < other.x + other.width
            and other.x < self.x + self.width
            and self.y < other.y + other.height
            and other.y < self.y + self.height
        )

    def inflate(self, insets: Insets) -> Rect:
        """Grow the rect by the insets. Negative insets shrink it."""
        return Rect(
            x=self.x - insets.left,
            y=self.y - insets.top,
            width=self.width + insets.horizontal,
            height=self.height + insets.vertical,
        )
