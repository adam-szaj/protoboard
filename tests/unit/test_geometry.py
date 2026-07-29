# SPDX-License-Identifier: MIT
"""Tests for protoboard.geometry — Point, Size, Insets, Rect."""

import dataclasses

import pytest

from protoboard.geometry import Insets, Point, Rect, Size, Vector, Vector3D


class TestPoint:
    def test_construct(self) -> None:
        p = Point(1.5, 2.5)
        assert p.x == 1.5
        assert p.y == 2.5

    def test_equality(self) -> None:
        assert Point(1.0, 2.0) == Point(1.0, 2.0)
        assert Point(1.0, 2.0) != Point(1.0, 2.1)

    def test_addition(self) -> None:
        result = Point(1.0, 2.0) + Vector(0.5, 0.5)
        assert result == Point(1.5, 2.5)

    def test_subtraction(self) -> None:
        p1 = Point(2.0, 3.0)
        v1 = Vector(0.5, 1.0)
        result = p1 - v1
        assert result == Point(1.5, 2.0)

        assert (result - p1) == - v1

    def test_frozen(self) -> None:
        p = Point(0.0, 0.0)
        with pytest.raises(dataclasses.FrozenInstanceError):
            p.x = 1.0  # type: ignore[misc]

    def test_negative_coords_allowed(self) -> None:
        p = Point(-1.5, -2.0)
        assert p.x == -1.5
        assert p.y == -2.0


class TestVectorScaling:
    def test_mul_scalar_on_right(self) -> None:
        assert Vector(1.0, -2.0) * 3 == Vector(3.0, -6.0)

    def test_mul_scalar_on_left(self) -> None:
        assert 3 * Vector(1.0, -2.0) == Vector(3.0, -6.0)

    def test_truediv_scalar(self) -> None:
        assert Vector(3.0, -6.0) / 3 == Vector(1.0, -2.0)

    def test_scaled_step_along_direction(self) -> None:
        # The routing idiom: a + dir * k walks k units along a unit step.
        a = Point(0.5, 0.5)
        direction = Vector(1.0, 0.0)
        assert a + direction * 3 == Point(3.5, 0.5)

    def test_lerp_via_scaled_displacement(self) -> None:
        # a + (b - a) * t — the parametric point-on-segment idiom.
        a, b = Point(0.0, 0.0), Point(4.0, 2.0)
        assert a + (b - a) * 0.5 == Point(2.0, 1.0)


class TestVectorProducts:
    def test_dot(self) -> None:
        assert Vector(1.0, 2.0).dot(Vector(3.0, 4.0)) == 11.0
        # Perpendicular vectors dot to zero.
        assert Vector(1.0, 0.0).dot(Vector(0.0, 1.0)) == 0.0

    def test_cross_2d_sign(self) -> None:
        # +x cross +y is +1 (counter-clockwise).
        assert Vector(1.0, 0.0).cross(Vector(0.0, 1.0)) == 1.0
        # Reversed order flips the sign.
        assert Vector(0.0, 1.0).cross(Vector(1.0, 0.0)) == -1.0

    def test_cross_2d_zero_when_collinear(self) -> None:
        assert Vector(2.0, 4.0).cross(Vector(1.0, 2.0)) == 0.0

    def test_collinearity_via_point_cross(self) -> None:
        # The cost_analyzer idiom: (b - a) × (p - a) == 0 ⇔ p on line a→b.
        a, b = Point(0.0, 0.0), Point(4.0, 4.0)
        on_line, off_line = Point(2.0, 2.0), Point(2.0, 3.0)
        assert (b - a).cross(on_line - a) == 0.0
        assert (b - a).cross(off_line - a) != 0.0


class TestVectorAngle:
    def test_angle_deg_axes(self) -> None:
        assert Vector(1.0, 0.0).angle_deg() == 0.0
        assert Vector(0.0, 1.0).angle_deg() == 90.0
        assert Vector(-1.0, 0.0).angle_deg() == 180.0
        assert Vector(0.0, -1.0).angle_deg() == -90.0

    def test_angle_radians(self) -> None:
        import math
        assert Vector(1.0, 1.0).angle() == pytest.approx(math.pi / 4)

    def test_angle_of_displacement(self) -> None:
        # The body_shapes idiom: angle of the axis between two pins.
        p1, p2 = Point(2.0, 2.0), Point(5.0, 2.0)
        assert (p2 - p1).angle_deg() == 0.0
        assert (Point(2.0, 5.0) - p1).angle_deg() == 90.0

    def test_zero_vector_angle_is_zero(self) -> None:
        assert Vector(0.0, 0.0).angle_deg() == 0.0


class TestVector3DProducts:
    def test_dot(self) -> None:
        assert Vector3D(1.0, 2.0, 3.0).dot(Vector3D(4.0, 5.0, 6.0)) == 32.0

    def test_cross_basis(self) -> None:
        # x̂ × ŷ = ẑ
        x = Vector3D(1.0, 0.0, 0.0)
        y = Vector3D(0.0, 1.0, 0.0)
        assert x.cross(y) == Vector3D(0.0, 0.0, 1.0)

    def test_cross_anticommutes(self) -> None:
        u = Vector3D(1.0, 2.0, 3.0)
        v = Vector3D(4.0, 5.0, 6.0)
        assert u.cross(v) == -(v.cross(u))

    def test_2d_cross_is_z_of_3d_cross(self) -> None:
        # Vector.cross matches the z of the embedded 3D cross.
        a, b = Vector(1.0, 2.0), Vector(3.0, -1.0)
        a3 = Vector3D(a.dx, a.dy, 0.0)
        b3 = Vector3D(b.dx, b.dy, 0.0)
        assert a.cross(b) == a3.cross(b3).dz

    def test_scaling(self) -> None:
        assert Vector3D(1.0, -2.0, 3.0) * 2 == Vector3D(2.0, -4.0, 6.0)
        assert 2 * Vector3D(1.0, -2.0, 3.0) == Vector3D(2.0, -4.0, 6.0)
        assert Vector3D(2.0, -4.0, 6.0) / 2 == Vector3D(1.0, -2.0, 3.0)


class TestSize:
    def test_construct(self) -> None:
        s = Size(3.0, 4.0)
        assert s.lx == 3.0
        assert s.ly == 4.0

    def test_zero(self) -> None:
        s = Size.zero()
        assert s.lx == 0.0
        assert s.ly == 0.0

    def test_negative_width_raises(self) -> None:
        with pytest.raises(ValueError):
            Size(-1.0, 1.0)

    def test_negative_height_raises(self) -> None:
        with pytest.raises(ValueError):
            Size(1.0, -1.0)

    def test_zero_dimensions_allowed(self) -> None:
        s = Size(0.0, 0.0)
        assert s.lx == 0.0

    def test_frozen(self) -> None:
        s = Size(1.0, 1.0)
        with pytest.raises(dataclasses.FrozenInstanceError):
            s.lx = 2.0  # type: ignore[misc]


class TestInsets:
    def test_construct(self) -> None:
        i = Insets(top=1.0, right=2.0, bottom=3.0, left=4.0)
        assert i.top == 1.0
        assert i.right == 2.0
        assert i.bottom == 3.0
        assert i.left == 4.0

    def test_zero(self) -> None:
        i = Insets.zero()
        assert i.top == i.right == i.bottom == i.left == 0.0

    def test_all(self) -> None:
        i = Insets.all(2.5)
        assert i.top == i.right == i.bottom == i.left == 2.5

    def test_negative_allowed(self) -> None:
        # Negative margins are intentionally allowed (CLAUDE.md §4.2).
        i = Insets(top=-0.5, right=0.0, bottom=0.0, left=0.0)
        assert i.top == -0.5

    def test_horizontal_total(self) -> None:
        i = Insets(top=1.0, right=2.0, bottom=3.0, left=4.0)
        assert i.horizontal == 6.0  # left + right

    def test_vertical_total(self) -> None:
        i = Insets(top=1.0, right=2.0, bottom=3.0, left=4.0)
        assert i.vertical == 4.0  # top + bottom

    def test_frozen(self) -> None:
        i = Insets.zero()
        with pytest.raises(dataclasses.FrozenInstanceError):
            i.top = 1.0  # type: ignore[misc]


class TestRect:
    def test_construct(self) -> None:
        r = Rect.of(x=1.0, y=2.0, lx=3.0, ly=4.0)
        assert r.x == 1.0
        assert r.y == 2.0
        assert r.lx == 3.0
        assert r.ly == 4.0

    def test_negative_size_raises(self) -> None:
        with pytest.raises(ValueError):
            Rect.of(0.0, 0.0, -1.0, 1.0)
        with pytest.raises(ValueError):
            Rect.of(0.0, 0.0, 1.0, -1.0)

    def test_center(self) -> None:
        r = Rect.of(x=0.0, y=0.0, lx=10.0, ly=4.0)
        assert r.center == Point(5.0, 2.0)

    def test_bottom_right(self) -> None:
        r = Rect.of(x=1.0, y=2.0, lx=3.0, ly=4.0)
        assert r.bottom_right == Point(4.0, 6.0)

    def test_top_left(self) -> None:
        r = Rect.of(x=1.0, y=2.0, lx=3.0, ly=4.0)
        assert r.origin == Point(1.0, 2.0)

    def test_contains_point_inside(self) -> None:
        r = Rect.of(0.0, 0.0, 10.0, 10.0)
        assert r.contains(Point(5.0, 5.0))

    def test_contains_point_on_edge(self) -> None:
        r = Rect.of(0.0, 0.0, 10.0, 10.0)
        # Boundary inclusive on top/left, exclusive on bottom/right (half-open).
        assert r.contains(Point(0.0, 0.0))
        assert not r.contains(Point(10.0, 10.0))

    def test_contains_point_outside(self) -> None:
        r = Rect.of(0.0, 0.0, 10.0, 10.0)
        assert not r.contains(Point(11.0, 5.0))
        assert not r.contains(Point(-1.0, 5.0))

    def test_intersects_overlapping(self) -> None:
        a = Rect.of(0.0, 0.0, 5.0, 5.0)
        b = Rect.of(3.0, 3.0, 5.0, 5.0)
        assert a.intersects(b)
        assert b.intersects(a)

    def test_intersects_disjoint(self) -> None:
        a = Rect.of(0.0, 0.0, 2.0, 2.0)
        b = Rect.of(5.0, 5.0, 2.0, 2.0)
        assert not a.intersects(b)

    def test_intersects_touching_edge_does_not_count(self) -> None:
        # Edge touching is not intersection (atoms can touch but not overlap).
        a = Rect.of(0.0, 0.0, 2.0, 2.0)
        b = Rect.of(2.0, 0.0, 2.0, 2.0)
        assert not a.intersects(b)

    def test_inflate_positive(self) -> None:
        r = Rect.of(2.0, 2.0, 4.0, 4.0)
        inflated = r.inflate(Insets.all(1.0))
        assert inflated == Rect.of(1.0, 1.0, 6.0, 6.0)

    def test_inflate_negative(self) -> None:
        # Negative inflate (= deflate). Should still produce a valid rect
        # if the result has non-negative dimensions.
        r = Rect.of(0.0, 0.0, 10.0, 10.0)
        deflated = r.inflate(Insets.all(-1.0))
        assert deflated == Rect.of(1.0, 1.0, 8.0, 8.0)

    def test_inflate_asymmetric(self) -> None:
        r = Rect.of(0.0, 0.0, 10.0, 10.0)
        result = r.inflate(Insets(top=1.0, right=2.0, bottom=3.0, left=4.0))
        # x decreases by left, y decreases by top, width increases by horizontal,
        # height increases by vertical.
        assert result == Rect.of(-4.0, -1.0, 16.0, 14.0)

    def test_frozen(self) -> None:
        r = Rect.of(0.0, 0.0, 1.0, 1.0)
        with pytest.raises(dataclasses.FrozenInstanceError):
            r.origin = Point(1.0, 1.0)  # type: ignore[misc]
