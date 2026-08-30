# SPDX-License-Identifier: MIT
"""Tests for the geometry ↔ Qt value-type bridges (qt-ports §6.1).

The transform bridge is the subtle one: Qt's ``(m11, m12, m21, m22)`` is the
transpose of our ``(a, b, c, d)`` linear part — a point mapped through
``Transform2D.apply`` and through ``to_qtransform(...).map`` must agree.
"""

from __future__ import annotations

from PySide6.QtCore import QPointF, QRectF

from protoboard.geometry import Point, Rect, Transform2D, Vector
from protoboard.gui.qt_geometry import (
    from_qpointf,
    from_qrectf,
    from_qtransform,
    to_qpointf,
    to_qrectf,
    to_qtransform,
)


class TestPointBridge:
    def test_round_trip(self) -> None:
        p = Point(1.25, -3.5)
        assert from_qpointf(to_qpointf(p)) == p

    def test_from_qpointf(self) -> None:
        assert from_qpointf(QPointF(2.0, 4.0)) == Point(2.0, 4.0)


class TestRectBridge:
    def test_round_trip(self) -> None:
        r = Rect.of(1.0, 2.0, 3.0, 4.0)
        assert from_qrectf(to_qrectf(r)) == r

    def test_from_qrectf(self) -> None:
        assert from_qrectf(QRectF(0.5, 1.5, 2.0, 3.0)) == Rect.of(0.5, 1.5, 2.0, 3.0)


class TestTransformBridge:
    def test_round_trip_preserves_coeffs(self) -> None:
        t = Transform2D(0.0, 1.0, -1.0, 0.0, 5.0, -2.0)
        assert from_qtransform(to_qtransform(t)) == t

    def test_apply_matches_qt_map(self) -> None:
        # Rotation + reflection + translation — exercises the transpose.
        cases = [
            Transform2D.rotation(1),
            Transform2D.reflection(),
            Transform2D.rotation(3).compose(Transform2D.translation(Vector(2.0, -1.0))),
            Transform2D(2.0, 0.5, -0.25, 1.5, 3.0, 4.0),
        ]
        points = [Point(0.0, 0.0), Point(1.0, 0.0), Point(-2.5, 3.75)]
        for t in cases:
            qt = to_qtransform(t)
            for p in points:
                ours = t.apply(p)
                theirs = qt.map(QPointF(p.x, p.y))
                assert abs(ours.x - theirs.x()) < 1e-12
                assert abs(ours.y - theirs.y()) < 1e-12
