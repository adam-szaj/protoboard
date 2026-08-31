# SPDX-License-Identifier: MIT
"""The value text printed on a component body, pinned to the body frame.

The label is placed in the body's LOCAL frame (centre + direction) and mapped
through the body's local→world transform, so it inherits the body's rotation
and flip and stays fixed relative to the body geometry — not the pin row
(this matters for bendable pins).  The glyphs are kept readable: a mirrored
(flipped) body is compensated so text never reads backwards, and an
upside-down world orientation is flipped upright.  Size is geometric
scale-to-fit, optionally bounded by a maximum rendered height.
"""

from __future__ import annotations

from PySide6.QtGui import QBrush, QColor, QTransform
from PySide6.QtWidgets import QGraphicsItem, QGraphicsSimpleTextItem

from protoboard.geometry import Point
from protoboard.gui.layer_state import Z_PIN_MODIFIERS
from protoboard.instrumentation.debug_trace import traced


@traced()
def make_body_label(
    text: str,
    ink: QColor,
    body_transform: QTransform,
    local_center: Point,
    local_angle_deg: float,
    *,
    avail_len: float,
    avail_w: float,
    max_height: float | None = None,
    alpha: float,
    z: float,
) -> list[QGraphicsItem]:
    """A body-frame-pinned, readable value label as ``[full_alpha, half_alpha]``.

    Returns two copies of the text (full-alpha, then 50%-alpha) so the caller
    can route them to different Z bands, or an empty list when there is nothing
    to draw.
    """
    if not text or avail_len <= 0.0 or avail_w <= 0.0:
        return []

    label = [QGraphicsSimpleTextItem(text), QGraphicsSimpleTextItem(text)]

    for _label, coeff in zip(label, [1.0, 0.5]):
        pen_ink = QColor(ink)
        pen_ink.setAlphaF(pen_ink.alphaF() * alpha * coeff)
        _label.setBrush(QBrush(pen_ink))

    br = label[0].boundingRect()

    if br.width() < 1e-9 or br.height() < 1e-9:
        return []
    scale = min(avail_len / br.width(), avail_w / br.height())
    if max_height is not None:
        scale = min(scale, max_height / br.height())

    mirrored = body_transform.determinant() < 0.0

    t = QTransform(body_transform)
    t.translate(local_center.x, local_center.y)
    t.rotate(local_angle_deg)
    if mirrored:
        t.scale(-1.0, 1.0)  # counter the reflection so glyphs read forwards
    if scale != 1.0:
        t.scale(scale, scale)
    t.translate(-br.width() / 2.0, -br.height() / 2.0)

    for _label in label:
        _label.setTransform(t)

    label[0].setZValue(z)
    label[1].setZValue(Z_PIN_MODIFIERS + 0.9)

    out: list[QGraphicsItem] = list(label)
    return out
