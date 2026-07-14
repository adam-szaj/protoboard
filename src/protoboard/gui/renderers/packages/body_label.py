# SPDX-License-Identifier: MIT
"""The value text printed on a component body, scaled to fit.

Generalises the SMD chip's ``_code_label``: given the string, ink, the body
centre and axis angle, and the available (length × width) box, produce a
``QGraphicsSimpleTextItem`` scaled to fit and centred, flipped upright so it
never reads upside-down.  Shared by every body renderer that prints a value.
"""

from __future__ import annotations

from PySide6.QtGui import QBrush, QColor, QTransform
from PySide6.QtWidgets import QGraphicsSimpleTextItem

from protoboard.geometry import Point


def make_body_label(
    text: str,
    ink: QColor,
    center: Point,
    angle_deg: float,
    avail_len: float,
    avail_w: float,
    *,
    alpha: float,
    z: float,
) -> QGraphicsSimpleTextItem | None:
    """A scaled, centred, upright value label, or ``None`` if it can't fit."""
    if not text or avail_len <= 0.0 or avail_w <= 0.0:
        return None
    label = QGraphicsSimpleTextItem(text)
    pen_ink = QColor(ink)
    pen_ink.setAlphaF(pen_ink.alphaF() * alpha)
    label.setBrush(QBrush(pen_ink))
    br = label.boundingRect()
    if br.width() < 1e-9 or br.height() < 1e-9:
        return None
    scale = min(avail_len / br.width(), avail_w / br.height())
    angle = angle_deg
    if not -90.0 < angle <= 90.0:
        angle += 180.0 if angle <= 0 else -180.0
    t = QTransform()
    t.translate(center.x, center.y)
    t.rotate(angle)
    t.scale(scale, scale)
    t.translate(-br.width() / 2.0, -br.height() / 2.0)
    label.setTransform(t)
    label.setZValue(z)
    return label
