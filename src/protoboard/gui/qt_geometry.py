# SPDX-License-Identifier: MIT
"""Bridges between ``protoboard.geometry`` and Qt value types (qt-ports §6.1).

The canonical conversion vocabulary for the board-canvas seam: Qt-side
adapters convert at the seam edge; code behind the seam speaks
``Point`` / ``Rect`` / ``Transform2D`` only and never imports these helpers.
"""

from __future__ import annotations

from PySide6.QtCore import QPointF, QRectF
from PySide6.QtGui import QTransform

from protoboard.geometry import Point, Rect, Transform2D


def to_qpointf(p: Point) -> QPointF:
    return QPointF(p.x, p.y)


def from_qpointf(qp: QPointF) -> Point:
    return Point(qp.x(), qp.y())


def to_qrectf(r: Rect) -> QRectF:
    return QRectF(r.x, r.y, r.lx, r.ly)


def from_qrectf(qr: QRectF) -> Rect:
    return Rect.of(qr.x(), qr.y(), qr.width(), qr.height())


def to_qtransform(t: Transform2D) -> QTransform:
    """Qt's constructor takes ``(m11, m12, m21, m22, dx, dy)`` mapping
    ``x' = m11·x + m21·y + dx`` — the transpose of our linear part
    ``x' = a·x + b·y + tx``, so the arguments interleave.
    """
    a, b, c, d, tx, ty = t.coeffs()
    return QTransform(a, c, b, d, tx, ty)


def from_qtransform(qt: QTransform) -> Transform2D:
    """Inverse of :func:`to_qtransform` (affine part only)."""
    return Transform2D(qt.m11(), qt.m21(), qt.m12(), qt.m22(), qt.dx(), qt.dy())
