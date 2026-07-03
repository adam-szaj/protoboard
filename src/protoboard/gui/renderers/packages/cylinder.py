# SPDX-License-Identifier: MIT
"""Cylinder renderer — axial side-view capsule, upright disc, polarity stripe."""

from __future__ import annotations

from PySide6.QtCore import QRectF
from PySide6.QtGui import QBrush, QColor, QPainterPath, QTransform
from PySide6.QtWidgets import QGraphicsItem, QGraphicsLineItem, QGraphicsPathItem

from protoboard.domain.components.package.shapes import (
    Cylinder as _PkgCylinder,
)
from protoboard.geometry import Point
from protoboard.gui.renderers.packages.base import (
    PackageRenderer,
    _body_pen,
    _lead_pen,
)
from protoboard.gui.theme import Theme


class CylinderRenderer(PackageRenderer):
    """Axial cylinder (resistor, diode, glass DO-35) — a side-view capsule
    between the two pads; the radial top-view branch is handled in the base.
    """

    renders = "cylinder"

    def render_body(
        self,
        positions: list[Point],
        fill: QColor,
        border: QColor,
        alpha: float,
        z: float,
        theme: Theme,
    ) -> list[QGraphicsItem]:
        radial = self._maybe_radial(positions, fill, border, alpha, z, theme)
        if radial is not None:
            return radial
        pkg = self._pkg
        if isinstance(pkg, _PkgCylinder) and len(positions) >= 2:
            return self._draw_cylinder(pkg, positions, fill, border, alpha, z,
                                       theme)
        return self._draw_bbox(positions, fill, border, alpha, z, theme)

    # ------------------------------------------------------------------
    # Cylinder — side-view capsule along the lead axis (axial parts)
    # ------------------------------------------------------------------

    def _draw_cylinder(
        self,
        pkg: _PkgCylinder,
        positions: list[Point],
        fill: QColor,
        border: QColor,
        alpha: float,
        z: float,
        theme: Theme,
    ) -> list[QGraphicsItem]:
        mm = theme.raster_pitch_mm
        body_len = pkg.length_mm / mm
        body_w = pkg.diameter_mm / mm

        # vertical_flipped swaps the anchor end so the polarity stripe mirrors
        # when the part is mounted the other way round (legacy convention).
        if self._vertical_flipped:
            p1, p2 = positions[1], positions[0]
        else:
            p1, p2 = positions[0], positions[1]
        axis = p2 - p1  # Point − Point → Vector
        dx, dy = axis.dx, axis.dy
        dist = axis.length()
        if dist < 1e-9:
            return self._draw_bbox(positions, fill, border, alpha, z, theme)

        # Upright mount: when the pad spacing is shorter than the body, the part
        # cannot lie flat — an axial resistor/diode placed on adjacent pads
        # STANDS UPRIGHT, the body sitting directly OVER ONE pad (its anchor),
        # with the bent lead reaching the other. This is NOT the radial centroid
        # silhouette: an axial part mounts over a single pad (which pad = the
        # Flip / ``vertical_flipped`` end, already swapped into ``p1`` above),
        # whereas a radial part sits per its leads (centroid). Seen from above
        # the upright body is a disc (diameter = body width) on ``p1``; the ``-``
        # marker points towards the physical cathode (pin 2). (User rule,
        # 2026-06-28.)
        if dist < body_len - 1e-6:
            anchor = p1               # the Flip-chosen pad the body stands on
            other = p2                # the bent lead reaches here
            upright: list[QGraphicsItem] = []
            lead = QGraphicsLineItem(anchor.x, anchor.y, other.x, other.y)
            lead.setPen(_lead_pen(theme, alpha))
            lead.setZValue(z - 0.01)
            upright.append(lead)
            neg_pin = positions[1]    # physical cathode, independent of flip
            neg_angle = (neg_pin - anchor).angle_deg()
            world_axis_deg = (other - anchor).angle_deg()
            flat = self._flat_side_world(pkg, world_axis_deg)
            upright.extend(self._radial_disc(
                anchor, body_w, neg_angle, fill, border, alpha, z, theme,
                flat=flat,
            ))
            return upright

        mid_x = (p1.x + p2.x) / 2.0
        mid_y = (p1.y + p2.y) / 2.0
        angle_deg = axis.angle_deg()

        items: list[QGraphicsItem] = []

        # Leads — only when the pad spacing exceeds the body length.
        if dist > body_len + 1e-6:
            unit_x, unit_y = dx / dist, dy / dist
            attach = dist / 2.0 - body_len / 2.0
            lead_pen = _lead_pen(theme, alpha)
            l1 = QGraphicsLineItem(
                p1.x, p1.y, p1.x + unit_x * attach, p1.y + unit_y * attach,
            )
            l2 = QGraphicsLineItem(
                p2.x - unit_x * attach, p2.y - unit_y * attach, p2.x, p2.y,
            )
            for ln in (l1, l2):
                ln.setPen(lead_pen)
                ln.setZValue(z - 0.01)
            items.extend([l1, l2])

        # Edge rounding is a package property (edge_radius_mm, default 0 → a
        # plain rectangular body); only packages that declare it (e.g. a glass
        # DO-35) get a capsule. Clamp to half the shorter side so the rounded
        # rect never degenerates.
        r = min(pkg.edge_radius_mm / mm, body_w / 2.0, body_len / 2.0)
        path = QPainterPath()
        path.addRoundedRect(
            QRectF(-body_len / 2.0, -body_w / 2.0, body_len, body_w), r, r,
        )
        body = QGraphicsPathItem(path)
        fill_c = QColor(fill)
        fill_c.setAlphaF(fill_c.alphaF() * alpha)
        body.setBrush(QBrush(fill_c))
        body.setPen(_body_pen(border, alpha))
        body.setZValue(z)
        t = QTransform()
        t.translate(mid_x, mid_y)
        t.rotate(angle_deg)
        body.setTransform(t)
        items.append(body)

        # Polar cathode stripe near the p2 end (class flavour, not a package
        # property); vertical_flipped already swapped p1/p2 above.
        if self._is_polar:
            stripe_w = min(body_len * 0.12, body_w * 0.4)
            # Keep the whole stripe on the straight ("cylindrical") part of the
            # body: its outer edge sits where the end rounding begins
            # (body_len/2 - r), so even a fully-rounded capsule shows the stripe
            # on the flat segment, never on the curved end cap.
            stripe_right = max(body_len / 2.0 - r, stripe_w)
            stripe_x = stripe_right - stripe_w
            stripe_path = QPainterPath()
            stripe_path.addRect(QRectF(stripe_x, -body_w / 2.0, stripe_w, body_w))
            stripe = QGraphicsPathItem(stripe_path)
            dark = QColor(theme.body_led_stripe_color)
            dark.setAlphaF(alpha)
            stripe.setBrush(QBrush(dark))
            stripe.setPen(_body_pen(border, alpha, width=0.0))
            stripe.setZValue(z + 0.01)
            t_stripe = QTransform()
            t_stripe.translate(mid_x, mid_y)
            t_stripe.rotate(angle_deg)
            stripe.setTransform(t_stripe)
            items.append(stripe)
        return items
