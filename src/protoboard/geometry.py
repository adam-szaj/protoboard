# SPDX-License-Identifier: MIT
"""Geometric primitives in raster-unit coordinates.

All values are floats; negative coordinates are legal (boxes may have
negative margins). Sizes are non-negative; rectangles use a top-left
origin with half-open right/bottom edges (consistent with most 2D
graphics conventions).
"""

from __future__ import annotations

from collections.abc import Iterable, Iterator
from dataclasses import dataclass
from itertools import accumulate
from typing import overload

# from _typeshed import Self

# Coordinate quantisation precision used throughout the codebase for
# float-equality on world coordinates.  Pad centres land on
# ``integer + 0.5`` half-unit positions, so rounding to 4 decimal places
# is safe (far below the half-unit granularity) and matches the
# precision other modules use for dict keys.
_COORD_ROUNDING_DIGITS = 4


def norm(x: float, y: float, z: float) -> float:
    from math import sqrt
    return sqrt(x * x + y * y + z * z)


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

    x: float = 0.0
    y: float = 0.0

    def __add__(self, v: Vector) -> Point:
        return Point(self.x + v.dx, self.y + v.dy)

    @overload
    def __sub__(self, o: Point) -> Vector:
        ...

    @overload
    def __sub__(self, o: Vector) -> Point:
        ...

    def __sub__(self, o: Point | Vector) -> Point | Vector:
        if isinstance(o, Point):
            return Vector(self.x - o.x, self.y - o.y)
        else:
            return Point(self.x - o.x, self.y - o.y)


@dataclass(frozen=True, slots=True)
class Vector:
    dx: float = 0.0
    dy: float = 0.0

    @property
    def x(self) -> float:
        return self.dx

    @property
    def y(self) -> float:
        return self.dy

    def __neg__(self) -> Vector:
        return Vector(-self.dx, -self.dy)

    def __add__(self, other: Vector) -> Vector:
        return Vector(self.dx + other.dx, self.dy + other.dy)

    def __sub__(self, other: Vector) -> Vector:
        return Vector(self.dx - other.dx, self.dy - other.dy)

    def __mul__(self, scalar: float) -> Vector:
        """Scale the displacement by a scalar (``v * k``)."""
        return Vector(self.dx * scalar, self.dy * scalar)

    def __rmul__(self, scalar: float) -> Vector:
        """Scalar on the left (``k * v``) — same as ``v * k``."""
        return Vector(self.dx * scalar, self.dy * scalar)

    def __truediv__(self, scalar: float) -> Vector:
        """Scale the displacement by ``1 / scalar`` (``v / k``)."""
        return Vector(self.dx / scalar, self.dy / scalar)

    def dot(self, other: Vector) -> float:
        """Dot product ``self · other`` (a scalar)."""
        return self.dx * other.dx + self.dy * other.dy

    def cross(self, other: Vector) -> float:
        """2D cross product ``self × other`` — the scalar z of the 3D cross.

        Positive when *other* is counter-clockwise from *self*, zero when the
        two are collinear; its magnitude is twice the triangle area.  This is
        the z-component of ``Vector3D(self, z=0) × Vector3D(other, z=0)``.
        """
        return self.dx * other.dy - self.dy * other.dx

    def length(self) -> float:
        return norm(self.dx, self.dy, 0.0)

    def angle(self) -> float:
        """Direction of the vector in radians, measured CCW from +x.

        Wraps ``atan2(dy, dx)`` so callers ask for "the angle of this vector"
        instead of unpacking components into trigonometry.  The zero vector
        returns ``0.0`` (atan2's convention).
        """
        from math import atan2
        return atan2(self.dy, self.dx)

    def angle_deg(self) -> float:
        """Direction of the vector in degrees, measured CCW from +x."""
        from math import degrees
        return degrees(self.angle())


@dataclass(frozen=True, slots=True)
class Point3D:
    """A 3D point in raster units."""

    x: float = 0.0
    y: float = 0.0
    z: float = 0.0

    def __add__(self, v: Vector3D) -> Point3D:
        return Point3D(self.x + v.dx, self.y + v.dy, self.z + v.dz)

    @overload
    def __sub__(self, o: Point3D) -> Vector3D:
        ...

    @overload
    def __sub__(self, o: Vector3D) -> Point3D:
        ...

    def __sub__(self, o: Point3D | Vector3D) -> Point3D | Vector3D:
        if isinstance(o, Point3D):
            return Vector3D(self.x - o.x, self.y - o.y, self.z - o.z)
        else:
            return Point3D(self.x - o.x, self.y - o.y, self.z - o.z)


ROTATE2D_TRANSFORM: list = [
    lambda v: v, lambda v: Vector(v.dy, -v.dx), lambda v: Vector(-v.dx, -v.dy),
    lambda v: Vector(-v.dy, v.dx)
]


@dataclass(frozen=True, slots=True)
class Rotate2D:
    steps: int = 0

    def __call__(self, v: Vector) -> Vector:
        return ROTATE2D_TRANSFORM[self.steps % 4](v)


def _transform_vectors(rotate: Rotate2D, vectors: Iterable[Vector]) -> Iterator[Vector]:
    return map(rotate, vectors)


def transform_vectors(rotate: Rotate2D, vectors: Iterable[Vector]) -> VectorList:
    return list(_transform_vectors(rotate, vectors))


def _transform_points(rotate: Rotate2D, pivot: Point,
                      points: Iterable[Point]) -> Iterator[Point]:
    return map(lambda v: pivot + v,
               _transform_vectors(rotate, map(lambda p: p - pivot, points)))


def transform_points(rotate: Rotate2D, pivot: Point,
                     points: Iterable[Point]) -> PointList:
    return list(_transform_points(rotate, pivot, points))


@dataclass(frozen=True, slots=True)
class Vector3D:
    dx: float = 0.0
    dy: float = 0.0
    dz: float = 0.0

    @property
    def x(self) -> float:
        return self.dx

    @property
    def y(self) -> float:
        return self.dy

    @property
    def z(self) -> float:
        return self.dz

    def __add__(self, o: Vector3D) -> Vector3D:
        return Vector3D(self.dx + o.dx, self.dy + o.dy, self.dz + o.dz)

    def __sub__(self, o: Vector3D) -> Vector3D:
        return Vector3D(self.dx - o.dx, self.dy - o.dy, self.dz - o.dz)

    def __neg__(self) -> Vector3D:
        return Vector3D(-self.dx, -self.dy, -self.dz)

    def __mul__(self, scalar: float) -> Vector3D:
        return Vector3D(self.dx * scalar, self.dy * scalar, self.dz * scalar)

    def __rmul__(self, scalar: float) -> Vector3D:
        return Vector3D(self.dx * scalar, self.dy * scalar, self.dz * scalar)

    def __truediv__(self, scalar: float) -> Vector3D:
        return Vector3D(self.dx / scalar, self.dy / scalar, self.dz / scalar)

    def dot(self, other: Vector3D) -> float:
        """Dot product ``self · other`` (a scalar)."""
        return self.dx * other.dx + self.dy * other.dy + self.dz * other.dz

    def cross(self, other: Vector3D) -> Vector3D:
        """Cross product ``self × other`` (a vector perpendicular to both)."""
        return Vector3D(
            self.dy * other.dz - self.dz * other.dy,
            self.dz * other.dx - self.dx * other.dz,
            self.dx * other.dy - self.dy * other.dx,
        )

    def length(self) -> float:
        return norm(self.dx, self.dy, self.dz)


PointList = list[Point]
VectorList = list[Vector]
Point3DList = list[Point3D]
Vector3DList = list[Vector3D]


@overload
def _make_points_from_offsets(point, offsets: Iterable[Vector]) -> Iterator[Point]:
    ...


@overload
def _make_points_from_offsets(point, offsets: Iterable[Vector3D]) -> Iterator[Point3D]:
    ...


def _make_points_from_offsets(point, offsets) -> Iterable[Point3D] | Iterator[Point]:
    return accumulate(offsets, initial=point)


@overload
def make_points_from_offsets(point: Point, offsets: Iterable[Vector]) -> PointList:
    ...


@overload
def make_points_from_offsets(point: Point3D,
                             offsets: Iterable[Vector3D]) -> Point3DList:
    ...


def make_points_from_offsets(point, offsets) -> Point3DList | PointList:
    return list(_make_points_from_offsets(point, offsets))


@overload
def _make_offsets_from_points(points: Iterable[Point]) -> Iterator[Vector]:
    ...


@overload
def _make_offsets_from_points(points: Iterable[Point3D]) -> Iterator[Vector3D]:
    ...


def _make_offsets_from_points(points) -> Iterator[Vector] | Iterator[Vector3D]:
    from itertools import pairwise
    return map(lambda pw: pw[1] - pw[0], pairwise(points))


@overload
def make_offsets_from_points(points: Iterable[Point]) -> VectorList:
    ...


@overload
def make_offsets_from_points(points: Iterable[Point3D]) -> Vector3DList:
    ...


def make_offsets_from_points(points) -> VectorList | Vector3DList:
    return list(_make_offsets_from_points(points))


@dataclass(frozen=True, slots=True)
class Size:

    width: float
    height: float

    def __post_init__(self) -> None:
        if self.width < 0:
            raise ValueError(f"width must be non-negative; got {self.width}")
        if self.height < 0:
            raise ValueError(f"height must be non-negative; got {self.height}")

    @staticmethod
    def from_vector(v: Vector) -> Size:
        return Size(abs(v.dx), abs(v.dy))

    @staticmethod
    def from_points(p1: Point, p2: Point) -> Size:
        return Size.from_vector(p1 - p2)

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
        return (self.x <= point.x < self.x + self.width
                and self.y <= point.y < self.y + self.height)

    def intersects(self, other: Rect) -> bool:
        """Strict overlap; touching edges do not count as intersection."""
        return (self.x < other.x + other.width
                and other.x < self.x + self.width
                and self.y < other.y + other.height
                and other.y < self.y + self.height)

    def inflate(self, insets: Insets) -> Rect:
        """Grow the rect by the insets. Negative insets shrink it."""
        return Rect(
            x=self.x - insets.left,
            y=self.y - insets.top,
            width=self.width + insets.horizontal,
            height=self.height + insets.vertical,
        )
