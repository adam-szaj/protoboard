# SPDX-License-Identifier: MIT
"""The value text printed on a component body, pinned to the body frame.

The label is placed in the body's LOCAL frame (centre + direction) and mapped
through the body's local→world transform, so it inherits the body's rotation
and flip and stays fixed relative to the body geometry — not the pin row
(this matters for bendable pins).  The glyphs are kept readable: a mirrored
(flipped) body is compensated so text never reads backwards, and an
upside-down world orientation is flipped upright.  Size is either a fixed
point size (generic bodies) or geometric scale-to-fit (the SMD chip).
"""

from __future__ import annotations

import math

from PySide6.QtGui import QBrush, QColor, QFont, QTransform
from PySide6.QtWidgets import QGraphicsSimpleTextItem

from protoboard.geometry import Point


def make_body_label(
    text: str,
    ink: QColor,
    body_transform: QTransform,
    local_center: Point,
    local_angle_deg: float,
    *,
    point_size: float | None = None,
    avail_len: float | None = None,
    avail_w: float | None = None,
    alpha: float,
    z: float,
) -> QGraphicsSimpleTextItem | None:
    """A body-frame-pinned, readable value label, or ``None``."""
    if not text:
        return None
    label = QGraphicsSimpleTextItem(text)
    pen_ink = QColor(ink)
    pen_ink.setAlphaF(pen_ink.alphaF() * alpha)
    label.setBrush(QBrush(pen_ink))
    if point_size is not None:
        font = QFont()
        font.setPointSizeF(point_size)
        label.setFont(font)
    br = label.boundingRect()
    if br.width() < 1e-9 or br.height() < 1e-9:
        return None
    if point_size is None:
        if avail_len is None or avail_w is None or avail_len <= 0.0 or avail_w <= 0.0:
            return None
        scale = min(avail_len / br.width(), avail_w / br.height())
    else:
        scale = 1.0

    mirrored = body_transform.determinant() < 0.0

    t = QTransform(body_transform)
    t.translate(local_center.x, local_center.y)
    t.rotate(local_angle_deg)
    if mirrored:
        t.scale(-1.0, 1.0)  # counter the reflection so glyphs read forwards
    # Upright: if the accumulated world rotation would read upside-down,
    # add a half turn (position unaffected; glyphs turn right-way-up).
    world_angle = math.degrees(math.atan2(t.m12(), t.m11()))
    if not -90.0 < world_angle <= 90.0:
        t.rotate(180.0)
    if scale != 1.0:
        t.scale(scale, scale)
    t.translate(-br.width() / 2.0, -br.height() / 2.0)
    label.setTransform(t)
    label.setZValue(z)
    return label
