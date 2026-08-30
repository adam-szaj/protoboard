# SPDX-License-Identifier: MIT
"""Geometric primitives in raster-unit coordinates.

The vocabulary law — every name derives from the coordinate axes:

- position: ``Point(x, y)`` / ``Point3D(x, y, z)``
- displacement: ``Vector(dx, dy)`` / ``Vector3D(dx, dy, dz)``
- extents: ``Size(lx, ly)`` / ``Size3D(lx, ly, lz)``
- rectangle: ``Rect(origin: Point, size: Size)``

The words width/height/length are banned from this model: they read
differently in world, body-local and screen contexts (the old
``Prism.height_mm`` was a z-elevation while ``Rect.height`` was a
y-extent). Screen vocabulary ``[x, y, w, h]`` exists only at the Qt
boundary (``gui/qt_geometry.py``); ``length`` survives only as prose —
a magnitude is ``norm()``.

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
    def __sub__(self, o: Point) -> Vector: ...

    @overload
    def __sub__(self, o: Vector) -> Point: ...

    def __sub__(self, o: Point | Vector) -> Point | Vector:
        if isinstance(o, Point):
            return Vector(self.x - o.x, self.y - o.y)
        else:
            return Point(self.x - o.dx, self.y - o.dy)

    def with_z(self, z: float = 0.0) -> Point3D:
        """Lift to 3D at the given elevation."""
        return Point3D(self.x, self.y, z)


@dataclass(frozen=True, slots=True)
class Vector:
    """A 2D displacement [dx, dy]. A vector is not a point — there are
    deliberately no ``.x``/``.y`` aliases."""

    dx: float = 0.0
    dy: float = 0.0

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

    def norm(self) -> float:
        """Euclidean magnitude of the displacement."""
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

    def with_dz(self, dz: float = 0.0) -> Vector3D:
        """Lift to a 3D displacement."""
        return Vector3D(self.dx, self.dy, dz)


@dataclass(frozen=True, slots=True)
class Point3D:
    """A 3D point in raster units."""

    x: float = 0.0
    y: float = 0.0
    z: float = 0.0

    def __add__(self, v: Vector3D) -> Point3D:
        return Point3D(self.x + v.dx, self.y + v.dy, self.z + v.dz)

    @overload
    def __sub__(self, o: Point3D) -> Vector3D: ...

    @overload
    def __sub__(self, o: Vector3D) -> Point3D: ...

    def __sub__(self, o: Point3D | Vector3D) -> Point3D | Vector3D:
        if isinstance(o, Point3D):
            return Vector3D(self.x - o.x, self.y - o.y, self.z - o.z)
        else:
            return Point3D(self.x - o.dx, self.y - o.dy, self.z - o.dz)

    @property
    def xy(self) -> Point:
        """Board-plane projection (drops z)."""
        return Point(self.x, self.y)


ROTATE2D_TRANSFORM: list = [
    lambda v: v,
    lambda v: Vector(v.dy, -v.dx),
    lambda v: Vector(-v.dx, -v.dy),
    lambda v: Vector(-v.dy, v.dx),
]


@dataclass(frozen=True, slots=True)
class Rotate2D:
    steps: int = 0

    def __call__(self, v: Vector) -> Vector:
        return ROTATE2D_TRANSFORM[self.steps % 4](v)


@dataclass(frozen=True, slots=True)
class Transform2D:
    """A 2-D affine transform in homogeneous form.

    Maps a point through ``[x', y'] = [[a, b, tx], [c, d, ty]] · [x, y, 1]``:
    the linear part ``[[a, b], [c, d]]`` rotates/reflects, ``(tx, ty)``
    translates.  This is the single mechanism for component placement —
    rotation, board-side flip and translation are all instances of it,
    composed and inverted algebraically.

    For the placement use-case every entry is exact: 90° rotations and axis
    reflections give integer entries in ``{-1, 0, 1}``, so world coordinates
    never drift through trigonometry.  The class carries no Qt dependency; the
    GUI builds a ``QTransform`` from :meth:`coeffs`.

    A reflection has ``determinant() == -1`` — the chirality flip that a
    physical board-side flip introduces, and that a symmetric pin cloud would
    otherwise hide.
    """

    a: float = 1.0
    b: float = 0.0
    c: float = 0.0
    d: float = 1.0
    tx: float = 0.0
    ty: float = 0.0

    @staticmethod
    def identity() -> Transform2D:
        return Transform2D()

    @staticmethod
    def translation(v: Vector) -> Transform2D:
        return Transform2D(1.0, 0.0, 0.0, 1.0, v.dx, v.dy)

    @staticmethod
    def rotation(steps: int) -> Transform2D:
        """Clockwise rotation by ``steps`` × 90° about the origin.

        Matches the legacy ``_rotate``: one step sends ``(x, y) → (y, -x)``.
        """
        # Column images of (x̂, ŷ) under CW 90°·steps, exact integers.
        a, b, c, d = (
            (1.0, 0.0, 0.0, 1.0),
            (0.0, 1.0, -1.0, 0.0),
            (-1.0, 0.0, 0.0, -1.0),
            (0.0, -1.0, 1.0, 0.0),
        )[steps % 4]
        return Transform2D(a, b, c, d, 0.0, 0.0)

    @staticmethod
    def rotation_to(axis: Vector) -> Transform2D:
        """Rotation (det = +1) mapping the +x unit vector onto *axis*'s
        direction: linear part ``[[ux, -uy], [uy, ux]]`` with ``(ux, uy) =
        axis / |axis|``.

        The continuous generalisation of :meth:`rotation`: for an
        axis-parallel input the entries are exact ``{-1, 0, 1}``. Raises
        ``ValueError`` on a (near-)zero axis — a frame needs a direction.
        """
        n = axis.norm()
        if n < 1e-12:
            raise ValueError("rotation_to needs a non-zero axis")
        ux, uy = axis.dx / n, axis.dy / n
        return Transform2D(ux, -uy, uy, ux, 0.0, 0.0)

    @staticmethod
    def reflection() -> Transform2D:
        """Reflection across the y-axis: ``(x, y) → (-x, y)``.

        The board-side flip in local frame.  ``determinant() == -1``.
        """
        return Transform2D(-1.0, 0.0, 0.0, 1.0, 0.0, 0.0)

    @overload
    def apply(self, p: Point) -> Point: ...
    @overload
    def apply(self, p: Vector) -> Vector: ...

    def apply(self, p: Point | Vector) -> Point | Vector:
        """Transform a point (full affine) or a vector (linear part only).

        A ``Point`` carries the homogeneous ``1`` and picks up the translation;
        a ``Vector`` is a displacement, so only the linear part applies.
        """
        if isinstance(p, Point):
            return Point(
                self.a * p.x + self.b * p.y + self.tx,
                self.c * p.x + self.d * p.y + self.ty,
            )
        return Vector(self.a * p.dx + self.b * p.dy, self.c * p.dx + self.d * p.dy)

    def compose(self, other: Transform2D) -> Transform2D:
        """Return ``self ∘ other`` — apply *other* first, then *self*.

        ``(self.compose(other)).apply(p) == self.apply(other.apply(p))``.
        In homogeneous form this is the 3×3 matrix product ``self · other``.
        """
        return Transform2D(
            self.a * other.a + self.b * other.c,
            self.a * other.b + self.b * other.d,
            self.c * other.a + self.d * other.c,
            self.c * other.b + self.d * other.d,
            self.a * other.tx + self.b * other.ty + self.tx,
            self.c * other.tx + self.d * other.ty + self.ty,
        )

    def determinant(self) -> float:
        """Determinant of the linear part: ``+1`` orientation-preserving
        (rotation), ``-1`` orientation-reversing (reflection / physical flip)."""
        return self.a * self.d - self.b * self.c

    def rotation_steps(self) -> int:
        """CW 90°-steps encoded in a quantised ``det=+1`` linear part.

        Reads ``k`` back from the column image of x̂ under :meth:`rotation`:
        ``(1,0)→0, (0,-1)→1, (-1,0)→2, (0,1)→3``.  A reflection (``det<0``)
        or a non-quantised linear part reports 0 — the identity fallback the
        placement composer has always used.
        """
        a, c = round(self.a), round(self.c)
        return {(1, 0): 0, (0, -1): 1, (-1, 0): 2, (0, 1): 3}.get((a, c), 0)

    def inverse(self) -> Transform2D:
        """The inverse transform (raises for a singular linear part)."""
        det = self.determinant()
        if det == 0.0:
            raise ValueError("Transform2D is singular; no inverse")
        ia = self.d / det
        ib = -self.b / det
        ic = -self.c / det
        id_ = self.a / det
        # Inverse translation: -(L⁻¹ · t)
        itx = -(ia * self.tx + ib * self.ty)
        ity = -(ic * self.tx + id_ * self.ty)
        return Transform2D(ia, ib, ic, id_, itx, ity)

    def coeffs(self) -> tuple[float, float, float, float, float, float]:
        """The six affine coefficients ``(a, b, c, d, tx, ty)``.

        The bridge the GUI uses to build a ``QTransform`` without importing Qt
        into the domain.
        """
        return (self.a, self.b, self.c, self.d, self.tx, self.ty)


def flip_reflection_from_pins(positions: list[Point]) -> Transform2D:
    """The board-side-flip reflection derived from a pin cloud (local frame).

    A physical flip is a 180° rotation about an axis in the board plane; in 2-D
    that is a reflection about the line running ALONG the pin row.  Reflect the
    coordinate PERPENDICULAR to that row, about the pin cloud's centre in that
    coordinate — then the pins stay on their pads (the row lies on the axis) and
    a multi-row part's rows swap onto each other.

    Axis direction: the row runs parallel to whichever coordinate the pins spread
    further on, measured as the total absolute step ``Σ|Δx|`` vs ``Σ|Δy|`` between
    successive pins.  Reflect the *other* coordinate.  A tie (a square layout,
    e.g. a QFP) defaults to reflecting X.

    Axis position: the midpoint ``(min + max) / 2`` of the pins in the reflected
    coordinate, so a symmetric raster layout maps pin-onto-pin exactly.

    Returned as ``translate(+c) ∘ reflect ∘ translate(-c)`` — one matrix.  This
    is the single source of the flip axis, shared by ``Footprint.flip_transform``
    and ``Package.flip_transform``; the axis is a property of the geometry and
    never depends on which board side the part is placed on.
    """
    if len(positions) < 2:
        return Transform2D.reflection()  # trivial; single pin sits on any axis
    xs = [p.x for p in positions]
    ys = [p.y for p in positions]
    spread_x = sum(abs(b - a) for a, b in zip(xs, xs[1:]))
    spread_y = sum(abs(b - a) for a, b in zip(ys, ys[1:]))
    # Reflect the coordinate perpendicular to the dominant pin-row axis.
    reflect_x = spread_x <= spread_y  # tie → reflect X
    if reflect_x:
        centre = (min(xs) + max(xs)) / 2.0
        pivot = Vector(centre, 0.0)
        reflect = Transform2D.reflection()  # negate x
    else:
        centre = (min(ys) + max(ys)) / 2.0
        pivot = Vector(0.0, centre)
        # Reflect y: R(180) about the x-axis is diag(1, -1) — build it directly.
        reflect = Transform2D(1.0, 0.0, 0.0, -1.0, 0.0, 0.0)
    return (
        Transform2D.translation(pivot)
        .compose(reflect)
        .compose(Transform2D.translation(-pivot))
    )


def _transform_vectors(rotate: Rotate2D, vectors: Iterable[Vector]) -> Iterator[Vector]:
    return map(rotate, vectors)


def transform_vectors(rotate: Rotate2D, vectors: Iterable[Vector]) -> VectorList:
    return list(_transform_vectors(rotate, vectors))


def _transform_points(
    rotate: Rotate2D, pivot: Point, points: Iterable[Point]
) -> Iterator[Point]:
    return map(
        lambda v: pivot + v,
        _transform_vectors(rotate, map(lambda p: p - pivot, points)),
    )


def transform_points(
    rotate: Rotate2D, pivot: Point, points: Iterable[Point]
) -> PointList:
    return list(_transform_points(rotate, pivot, points))


@dataclass(frozen=True, slots=True)
class Vector3D:
    """A 3D displacement [dx, dy, dz]. No ``.x``/``.y``/``.z`` aliases —
    a vector is not a point."""

    dx: float = 0.0
    dy: float = 0.0
    dz: float = 0.0

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

    def norm(self) -> float:
        """Euclidean magnitude of the displacement."""
        return norm(self.dx, self.dy, self.dz)

    @property
    def dxdy(self) -> Vector:
        """Board-plane projection (drops dz)."""
        return Vector(self.dx, self.dy)


PointList = list[Point]
VectorList = list[Vector]
Point3DList = list[Point3D]
Vector3DList = list[Vector3D]


@overload
def _make_points_from_offsets(point, offsets: Iterable[Vector]) -> Iterator[Point]: ...


@overload
def _make_points_from_offsets(
    point, offsets: Iterable[Vector3D]
) -> Iterator[Point3D]: ...


def _make_points_from_offsets(point, offsets) -> Iterable[Point3D] | Iterator[Point]:
    return accumulate(offsets, initial=point)


@overload
def make_points_from_offsets(point: Point, offsets: Iterable[Vector]) -> PointList: ...


@overload
def make_points_from_offsets(
    point: Point3D, offsets: Iterable[Vector3D]
) -> Point3DList: ...


def make_points_from_offsets(point, offsets) -> Point3DList | PointList:
    return list(_make_points_from_offsets(point, offsets))


@overload
def _make_offsets_from_points(points: Iterable[Point]) -> Iterator[Vector]: ...


@overload
def _make_offsets_from_points(points: Iterable[Point3D]) -> Iterator[Vector3D]: ...


def _make_offsets_from_points(points) -> Iterator[Vector] | Iterator[Vector3D]:
    from itertools import pairwise

    return map(lambda pw: pw[1] - pw[0], pairwise(points))


@overload
def make_offsets_from_points(points: Iterable[Point]) -> VectorList: ...


@overload
def make_offsets_from_points(points: Iterable[Point3D]) -> Vector3DList: ...


def make_offsets_from_points(points) -> VectorList | Vector3DList:
    return list(_make_offsets_from_points(points))


@dataclass(frozen=True, slots=True)
class Size:
    """Axis-aligned extents [lx, ly] (non-negative): lx along +x, ly along +y.

    The vocabulary law: sizes are named after the axis they extend along —
    never ``width``/``height``, which read differently in world, body-local
    and screen contexts.
    """

    lx: float
    ly: float

    def __post_init__(self) -> None:
        if self.lx < 0:
            raise ValueError(f"lx must be non-negative; got {self.lx}")
        if self.ly < 0:
            raise ValueError(f"ly must be non-negative; got {self.ly}")

    @staticmethod
    def from_vector(v: Vector) -> Size:
        return Size(abs(v.dx), abs(v.dy))

    @staticmethod
    def from_points(p1: Point, p2: Point) -> Size:
        return Size.from_vector(p1 - p2)

    @staticmethod
    def zero() -> Size:
        return Size(0.0, 0.0)

    def with_lz(self, lz: float = 0.0) -> Size3D:
        """Lift to 3D extents with the given off-plane extent."""
        return Size3D(self.lx, self.ly, lz)


@dataclass(frozen=True, slots=True)
class Size3D:
    """Axis-aligned extents [lx, ly, lz] (non-negative).

    In a package-local frame ``lz`` is the off-board elevation; the board
    plane only ever consumes the :attr:`footprint` projection.
    """

    lx: float
    ly: float
    lz: float = 0.0

    def __post_init__(self) -> None:
        if self.lx < 0:
            raise ValueError(f"lx must be non-negative; got {self.lx}")
        if self.ly < 0:
            raise ValueError(f"ly must be non-negative; got {self.ly}")
        if self.lz < 0:
            raise ValueError(f"lz must be non-negative; got {self.lz}")

    @property
    def footprint(self) -> Size:
        """Board-plane projection [lx, ly] (drops lz)."""
        return Size(self.lx, self.ly)

    @staticmethod
    def zero() -> Size3D:
        return Size3D(0.0, 0.0, 0.0)


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
    """Axis-aligned rectangle: a min corner (top-left in the y-down world
    frame) composed with axis-aligned extents.

    One concept per class: the corner is a :class:`Point`, the extents a
    :class:`Size`; scalar views (``x``/``y``/``lx``/``ly``) are derived.
    Screen-vocabulary ``[x, y, w, h]`` exists only at the Qt boundary
    (``gui/qt_geometry.py``).
    """

    origin: Point
    size: Size

    @staticmethod
    def of(x: float, y: float, lx: float, ly: float) -> Rect:
        """Scalar factory: min corner (x, y), extents (lx, ly)."""
        return Rect(Point(x, y), Size(lx, ly))

    @staticmethod
    def from_center(center: Point, size: Size) -> Rect:
        return Rect(Point(center.x - size.lx / 2.0, center.y - size.ly / 2.0), size)

    @property
    def x(self) -> float:
        return self.origin.x

    @property
    def y(self) -> float:
        return self.origin.y

    @property
    def lx(self) -> float:
        return self.size.lx

    @property
    def ly(self) -> float:
        return self.size.ly

    @property
    def bottom_right(self) -> Point:
        return Point(self.origin.x + self.size.lx, self.origin.y + self.size.ly)

    @property
    def center(self) -> Point:
        return Point(
            self.origin.x + self.size.lx / 2.0, self.origin.y + self.size.ly / 2.0
        )

    def corners(self) -> tuple[Point, Point, Point, Point]:
        """The four corners, starting at ``origin``, in ring order."""
        x0, y0 = self.origin.x, self.origin.y
        x1, y1 = x0 + self.size.lx, y0 + self.size.ly
        return (Point(x0, y0), Point(x1, y0), Point(x1, y1), Point(x0, y1))

    def contains(self, point: Point) -> bool:
        """Half-open containment: top/left inclusive, bottom/right exclusive."""
        return (
            self.origin.x <= point.x < self.origin.x + self.size.lx
            and self.origin.y <= point.y < self.origin.y + self.size.ly
        )

    def intersects(self, other: Rect) -> bool:
        """Strict overlap; touching edges do not count as intersection."""
        return (
            self.origin.x < other.origin.x + other.size.lx
            and other.origin.x < self.origin.x + self.size.lx
            and self.origin.y < other.origin.y + other.size.ly
            and other.origin.y < self.origin.y + self.size.ly
        )

    def inflate(self, insets: Insets) -> Rect:
        """Grow the rect by the insets. Negative insets shrink it."""
        return Rect.of(
            self.origin.x - insets.left,
            self.origin.y - insets.top,
            self.size.lx + insets.horizontal,
            self.size.ly + insets.vertical,
        )

    def transformed_aabb(self, t: Transform2D) -> Rect:
        """AABB of the four corners mapped through *t*.

        The general oriented-rectangle projection: rotating a body-local rect
        into the world and taking its axis-aligned bounds. For axis-parallel
        transforms this is exact (entries in {-1, 0, 1}); the along/across
        swaps the old body code hand-rolled fall out of the matrix.
        """
        pts = [t.apply(p) for p in self.corners()]
        xs = [p.x for p in pts]
        ys = [p.y for p in pts]
        x0, y0 = min(xs), min(ys)
        return Rect.of(x0, y0, max(xs) - x0, max(ys) - y0)
