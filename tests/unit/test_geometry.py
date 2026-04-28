# SPDX-License-Identifier: MIT
"""Tests for protoboard.geometry — Point, Size, Insets, Rect."""

import dataclasses

import pytest
from protoboard.geometry import Insets, Point, Rect, Size


class TestPoint:
    def test_construct(self) -> None:
        p = Point(1.5, 2.5)
        assert p.x == 1.5
        assert p.y == 2.5

    def test_equality(self) -> None:
        assert Point(1.0, 2.0) == Point(1.0, 2.0)
        assert Point(1.0, 2.0) != Point(1.0, 2.1)

    def test_addition(self) -> None:
        result = Point(1.0, 2.0) + Point(0.5, 0.5)
        assert result == Point(1.5, 2.5)

    def test_subtraction(self) -> None:
        result = Point(2.0, 3.0) - Point(0.5, 1.0)
        assert result == Point(1.5, 2.0)

    def test_frozen(self) -> None:
        p = Point(0.0, 0.0)
        with pytest.raises(dataclasses.FrozenInstanceError):
            p.x = 1.0  # type: ignore[misc]

    def test_negative_coords_allowed(self) -> None:
        p = Point(-1.5, -2.0)
        assert p.x == -1.5
        assert p.y == -2.0


class TestSize:
    def test_construct(self) -> None:
        s = Size(3.0, 4.0)
        assert s.width == 3.0
        assert s.height == 4.0

    def test_zero(self) -> None:
        s = Size.zero()
        assert s.width == 0.0
        assert s.height == 0.0

    def test_negative_width_raises(self) -> None:
        with pytest.raises(ValueError):
            Size(-1.0, 1.0)

    def test_negative_height_raises(self) -> None:
        with pytest.raises(ValueError):
            Size(1.0, -1.0)

    def test_zero_dimensions_allowed(self) -> None:
        s = Size(0.0, 0.0)
        assert s.width == 0.0

    def test_frozen(self) -> None:
        s = Size(1.0, 1.0)
        with pytest.raises(dataclasses.FrozenInstanceError):
            s.width = 2.0  # type: ignore[misc]


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
        r = Rect(x=1.0, y=2.0, width=3.0, height=4.0)
        assert r.x == 1.0
        assert r.y == 2.0
        assert r.width == 3.0
        assert r.height == 4.0

    def test_negative_size_raises(self) -> None:
        with pytest.raises(ValueError):
            Rect(0.0, 0.0, -1.0, 1.0)
        with pytest.raises(ValueError):
            Rect(0.0, 0.0, 1.0, -1.0)

    def test_center(self) -> None:
        r = Rect(x=0.0, y=0.0, width=10.0, height=4.0)
        assert r.center == Point(5.0, 2.0)

    def test_bottom_right(self) -> None:
        r = Rect(x=1.0, y=2.0, width=3.0, height=4.0)
        assert r.bottom_right == Point(4.0, 6.0)

    def test_top_left(self) -> None:
        r = Rect(x=1.0, y=2.0, width=3.0, height=4.0)
        assert r.top_left == Point(1.0, 2.0)

    def test_contains_point_inside(self) -> None:
        r = Rect(0.0, 0.0, 10.0, 10.0)
        assert r.contains(Point(5.0, 5.0))

    def test_contains_point_on_edge(self) -> None:
        r = Rect(0.0, 0.0, 10.0, 10.0)
        # Boundary inclusive on top/left, exclusive on bottom/right (half-open).
        assert r.contains(Point(0.0, 0.0))
        assert not r.contains(Point(10.0, 10.0))

    def test_contains_point_outside(self) -> None:
        r = Rect(0.0, 0.0, 10.0, 10.0)
        assert not r.contains(Point(11.0, 5.0))
        assert not r.contains(Point(-1.0, 5.0))

    def test_intersects_overlapping(self) -> None:
        a = Rect(0.0, 0.0, 5.0, 5.0)
        b = Rect(3.0, 3.0, 5.0, 5.0)
        assert a.intersects(b)
        assert b.intersects(a)

    def test_intersects_disjoint(self) -> None:
        a = Rect(0.0, 0.0, 2.0, 2.0)
        b = Rect(5.0, 5.0, 2.0, 2.0)
        assert not a.intersects(b)

    def test_intersects_touching_edge_does_not_count(self) -> None:
        # Edge touching is not intersection (atoms can touch but not overlap).
        a = Rect(0.0, 0.0, 2.0, 2.0)
        b = Rect(2.0, 0.0, 2.0, 2.0)
        assert not a.intersects(b)

    def test_inflate_positive(self) -> None:
        r = Rect(2.0, 2.0, 4.0, 4.0)
        inflated = r.inflate(Insets.all(1.0))
        assert inflated == Rect(1.0, 1.0, 6.0, 6.0)

    def test_inflate_negative(self) -> None:
        # Negative inflate (= deflate). Should still produce a valid rect
        # if the result has non-negative dimensions.
        r = Rect(0.0, 0.0, 10.0, 10.0)
        deflated = r.inflate(Insets.all(-1.0))
        assert deflated == Rect(1.0, 1.0, 8.0, 8.0)

    def test_inflate_asymmetric(self) -> None:
        r = Rect(0.0, 0.0, 10.0, 10.0)
        result = r.inflate(Insets(top=1.0, right=2.0, bottom=3.0, left=4.0))
        # x decreases by left, y decreases by top, width increases by horizontal,
        # height increases by vertical.
        assert result == Rect(-4.0, -1.0, 16.0, 14.0)

    def test_frozen(self) -> None:
        r = Rect(0.0, 0.0, 1.0, 1.0)
        with pytest.raises(dataclasses.FrozenInstanceError):
            r.x = 1.0  # type: ignore[misc]
