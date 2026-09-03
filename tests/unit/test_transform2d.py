# SPDX-License-Identifier: MIT
"""Tests for protoboard.geometry.Transform2D — the affine placement matrix.

A 2-D affine transform in the homogeneous form the user specified::

    [x']   [a  b  tx]   [x]
    [y'] = [c  d  ty] · [y]
                        [1]

The linear part ``[[a, b], [c, d]]`` does rotation / reflection; ``(tx, ty)`` is
the translation.  All entries for the placement use-case are exact: 90° rotations
and axis reflections give integer entries {-1, 0, 1}, so there is no float drift
in the domain.  The class is the single mechanism for rotation, side-flip and
translation — composed and inverted algebraically.
"""

import pytest

from protoboard.geometry import (
    Point,
    Transform2D,
    Vector,
    flip_reflection_from_pins,
    rotation_about,
)


def _key(p: Point) -> tuple[float, float]:
    return (round(p.x, 4), round(p.y, 4))


class TestConstructors:
    def test_identity_leaves_point_unchanged(self) -> None:
        assert Transform2D.identity().apply(Point(3.0, 4.0)) == Point(3.0, 4.0)

    def test_translation_moves_point(self) -> None:
        t = Transform2D.translation(Vector(20.0, 30.0))
        assert t.apply(Point(1.0, 2.0)) == Point(21.0, 32.0)

    def test_rotation_90_cw_matches_legacy(self) -> None:
        # steps=1 is one clockwise 90° step: (x, y) -> (y, -x), same as the
        # legacy _rotate in placement.py.
        r = Transform2D.rotation(1)
        assert r.apply(Point(1.0, 0.0)) == Point(0.0, -1.0)
        assert r.apply(Point(0.0, 1.0)) == Point(1.0, 0.0)

    def test_rotation_about_pivot_composes_translation_and_rotation(self) -> None:
        pivot = Point(4.0, 3.0)
        expected = (
            Transform2D.translation(Vector(pivot.x, pivot.y))
            .compose(Transform2D.rotation(1))
            .compose(Transform2D.translation(Vector(-pivot.x, -pivot.y)))
        )
        assert rotation_about(pivot, 90) == expected

    def test_rotation_steps_wrap_mod_4(self) -> None:
        assert Transform2D.rotation(4).apply(Point(3.0, 5.0)) == Point(3.0, 5.0)
        assert Transform2D.rotation(5).apply(Point(1.0, 0.0)) == Point(0.0, -1.0)

    def test_rotation_180(self) -> None:
        assert Transform2D.rotation(2).apply(Point(2.0, 3.0)) == Point(-2.0, -3.0)

    def test_rotation_270(self) -> None:
        assert Transform2D.rotation(3).apply(Point(1.0, 0.0)) == Point(0.0, 1.0)

    def test_reflection_negates_x(self) -> None:
        # The bottom-side flip in local frame: reflect across the y-axis.
        m = Transform2D.reflection()
        assert m.apply(Point(2.0, 5.0)) == Point(-2.0, 5.0)
        assert m.apply(Point(-3.0, 1.0)) == Point(3.0, 1.0)


class TestApply:
    def test_apply_vector_ignores_translation(self) -> None:
        # A Vector is a displacement — the linear part transforms it, but the
        # translation column does not apply (only points carry the homogeneous 1).
        t = Transform2D.translation(Vector(20.0, 30.0))
        assert t.apply(Vector(1.0, 2.0)) == Vector(1.0, 2.0)

    def test_apply_vector_under_rotation(self) -> None:
        assert Transform2D.rotation(1).apply(Vector(1.0, 0.0)) == Vector(0.0, -1.0)

    def test_apply_point_full_affine(self) -> None:
        # rotate 90° CW then translate: local (1, 0) -> (0, -1) -> (5, 9)
        t = Transform2D.translation(Vector(5.0, 10.0)).compose(Transform2D.rotation(1))
        assert t.apply(Point(1.0, 0.0)) == Point(5.0, 9.0)


class TestCompose:
    def test_compose_is_apply_right_then_left(self) -> None:
        # (A ∘ B).apply(p) == A.apply(B.apply(p))
        a = Transform2D.translation(Vector(10.0, 0.0))
        b = Transform2D.rotation(1)
        p = Point(1.0, 0.0)
        assert a.compose(b).apply(p) == a.apply(b.apply(p))

    def test_compose_order_matters(self) -> None:
        rot = Transform2D.rotation(1)
        trans = Transform2D.translation(Vector(10.0, 0.0))
        # translate-then-rotate ≠ rotate-then-translate
        assert rot.compose(trans).apply(Point(0.0, 0.0)) != trans.compose(rot).apply(
            Point(0.0, 0.0)
        )

    def test_compose_with_identity(self) -> None:
        t = Transform2D.translation(Vector(3.0, 4.0)).compose(Transform2D.rotation(2))
        i = Transform2D.identity()
        p = Point(2.0, 7.0)
        assert i.compose(t).apply(p) == t.apply(p)
        assert t.compose(i).apply(p) == t.apply(p)

    def test_full_placement_chain(self) -> None:
        # world = T(position) ∘ R(rotation) ∘ M(side) — the placement pipeline.
        # A bottom-side part, pins along x, rotated 90° CW, placed at (10, 20).
        place = (
            Transform2D.translation(Vector(10.0, 20.0))
            .compose(Transform2D.rotation(1))
            .compose(Transform2D.reflection())
        )
        # local pin at (2, 0): reflect -> (-2, 0); rotate 90cw -> (0, 2);
        # translate -> (10, 22)
        assert place.apply(Point(2.0, 0.0)) == Point(10.0, 22.0)


class TestInverse:
    def test_inverse_of_translation(self) -> None:
        t = Transform2D.translation(Vector(20.0, 30.0))
        p = Point(1.0, 2.0)
        assert t.inverse().apply(t.apply(p)) == p

    def test_inverse_of_rotation(self) -> None:
        r = Transform2D.rotation(1)
        p = Point(3.0, 5.0)
        assert r.inverse().apply(r.apply(p)) == p

    def test_inverse_of_reflection_is_self(self) -> None:
        m = Transform2D.reflection()
        p = Point(4.0, 7.0)
        assert m.inverse().apply(m.apply(p)) == p

    def test_inverse_of_full_chain_roundtrips(self) -> None:
        place = (
            Transform2D.translation(Vector(10.0, 20.0))
            .compose(Transform2D.rotation(3))
            .compose(Transform2D.reflection())
        )
        p = Point(2.5, -1.5)
        assert place.inverse().apply(place.apply(p)) == p

    def test_compose_inverse_is_identity(self) -> None:
        place = Transform2D.translation(Vector(7.0, -3.0)).compose(
            Transform2D.rotation(2)
        )
        p = Point(9.0, 9.0)
        assert place.compose(place.inverse()).apply(p) == p


class TestDeterminant:
    def test_rotation_preserves_orientation(self) -> None:
        # A pure rotation has det +1 (orientation preserved).
        for steps in range(4):
            assert Transform2D.rotation(steps).determinant() == 1.0

    def test_reflection_flips_orientation(self) -> None:
        # A reflection (physical side-flip) has det -1 — the chirality change
        # that a symmetric pin cloud would otherwise hide.
        assert Transform2D.reflection().determinant() == -1.0

    def test_translation_preserves_determinant(self) -> None:
        assert Transform2D.translation(Vector(5.0, 5.0)).determinant() == 1.0

    def test_flip_then_rotate_stays_negative(self) -> None:
        place = Transform2D.rotation(1).compose(Transform2D.reflection())
        assert place.determinant() == -1.0


class TestFlipReflectionFromPins:
    """flip_reflection_from_pins — the geometry primitive that derives the
    board-side-flip axis from a pin cloud (shared by Footprint and Package)."""

    def test_pins_along_x_reflect_y(self) -> None:
        # TO-92: 3 pins along +x → axis parallel to x → reflect y about y=0,
        # so the pins (all y=0) stay put.
        pins = [Point(0.0, 0.0), Point(1.0, 0.0), Point(2.0, 0.0)]
        m = flip_reflection_from_pins(pins)
        for p in pins:
            assert _key(m.apply(p)) == _key(p)

    def test_pins_along_y_reflect_x(self) -> None:
        # SIL: 5 pins down +y → axis parallel to y → reflect x about x=0,
        # pins (all x=0) stay put.
        pins = [Point(0.0, float(i)) for i in range(5)]
        m = flip_reflection_from_pins(pins)
        for p in pins:
            assert _key(m.apply(p)) == _key(p)

    def test_two_columns_swap(self) -> None:
        # DIP: columns at x=0 and x=3 (longer along y) → reflect x about x=1.5,
        # columns swap onto each other's pads.
        pins = [Point(0.0, float(i)) for i in range(4)] + [
            Point(3.0, float(3 - i)) for i in range(4)
        ]
        m = flip_reflection_from_pins(pins)
        assert _key(m.apply(pins[0])) == _key(pins[7])  # (0,0) → (3,0)
        assert _key(m.apply(pins[7])) == _key(pins[0])

    def test_is_reflection(self) -> None:
        pins = [Point(0.0, 0.0), Point(1.0, 0.0)]
        assert flip_reflection_from_pins(pins).determinant() == -1.0

    def test_tie_defaults_to_reflect_x(self) -> None:
        # A square 2×2 layout: equal spread → reflect X (the documented default).
        pins = [Point(0.0, 0.0), Point(1.0, 1.0)]
        m = flip_reflection_from_pins(pins)
        assert m.coeffs()[:4] == (-1.0, 0.0, 0.0, 1.0)  # negate x

    def test_single_pin_is_reflection(self) -> None:
        # Degenerate: one pin sits on any axis — return a plain reflection.
        assert flip_reflection_from_pins([Point(2.0, 2.0)]).determinant() == -1.0


class TestExactness:
    def test_rotation_entries_are_exact_integers(self) -> None:
        # No trigonometry in the domain: 90° rotations give integer entries,
        # so pad coordinates never drift.
        r = Transform2D.rotation(1)
        p = r.apply(Point(1.0, 0.0))
        assert p.x == 0.0 and p.y == -1.0  # exact, not approx
        # Four steps return exactly to the start.
        q = Point(3.7, -2.1)
        assert Transform2D.rotation(4).apply(q) == q


class TestCoeffs:
    """The six affine coefficients (a, b, c, d, tx, ty) — the bridge the GUI
    adapter uses to build a QTransform without importing Qt into the domain."""

    def test_identity_coeffs(self) -> None:
        assert Transform2D.identity().coeffs() == (1.0, 0.0, 0.0, 1.0, 0.0, 0.0)

    def test_translation_coeffs(self) -> None:
        # a, b, c, d, tx, ty
        assert Transform2D.translation(Vector(20.0, 30.0)).coeffs() == (
            1.0,
            0.0,
            0.0,
            1.0,
            20.0,
            30.0,
        )

    def test_rotation_90_coeffs(self) -> None:
        # (x, y) -> (y, -x): x' = 0*x + 1*y, y' = -1*x + 0*y
        assert Transform2D.rotation(1).coeffs() == (0.0, 1.0, -1.0, 0.0, 0.0, 0.0)

    def test_reflection_coeffs(self) -> None:
        assert Transform2D.reflection().coeffs() == (-1.0, 0.0, 0.0, 1.0, 0.0, 0.0)

    def test_coeffs_reconstruct_apply(self) -> None:
        place = (
            Transform2D.translation(Vector(10.0, 20.0))
            .compose(Transform2D.rotation(1))
            .compose(Transform2D.reflection())
        )
        a, b, c, d, tx, ty = place.coeffs()
        for p in (Point(0.0, 0.0), Point(2.0, 0.0), Point(0.0, 3.0), Point(1.5, -2.5)):
            want = place.apply(p)
            assert a * p.x + b * p.y + tx == pytest.approx(want.x)
            assert c * p.x + d * p.y + ty == pytest.approx(want.y)
