# SPDX-License-Identifier: MIT
"""Cylinder renderer — axial side-view capsule, upright / radial disc with
flat-side (D-shape) support, and the polarity markers (stripe / segment)."""

from __future__ import annotations

import math

from PySide6.QtCore import QRectF
from PySide6.QtGui import QBrush, QColor, QPainterPath, QTransform
from PySide6.QtWidgets import (
    QAbstractGraphicsShapeItem,
    QGraphicsEllipseItem,
    QGraphicsItem,
    QGraphicsLineItem,
    QGraphicsPathItem,
)

from protoboard.domain.components.package.shapes import (
    Cylinder as _PkgCylinder,
)
from protoboard.geometry import Point, Vector
from protoboard.gui.renderers.packages.base import (
    PackageRenderer,
    _lead_pen,
)
from protoboard.gui.renderers.packages.canvas import _body_pen
from protoboard.gui.theme import DEFAULT_THEME, Theme


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
    # Radial — top-view disc (full circle or D-shape) with the ``-`` segment
    # ------------------------------------------------------------------

    def _radial_silhouette(
        self,
        mid: Point, p1: Point, p2: Point, neg_pin: Point,
        fill: QColor, border: QColor, alpha: float, z: float,
        theme: Theme,
    ) -> list[QGraphicsItem]:
        pkg = self._pkg
        if not isinstance(pkg, _PkgCylinder):
            return super()._radial_silhouette(
                mid, p1, p2, neg_pin, fill, border, alpha, z, theme,
            )
        diam = pkg.diameter_mm / theme.raster_pitch_mm
        world_axis_deg = (p2 - p1).angle_deg()
        flat = self._flat_side_world(pkg, world_axis_deg)
        # Direction from the body centre towards the negative lead — the side
        # the ``-`` segment marker goes on.
        neg_angle = (neg_pin - mid).angle_deg()
        return self._radial_disc(
            mid, diam, neg_angle, fill, border, alpha, z, theme, flat=flat,
        )

    def _radial_body_radius(self, mm: float) -> float:
        pkg = self._pkg
        if isinstance(pkg, _PkgCylinder):
            return pkg.diameter_mm / mm / 2.0
        return super()._radial_body_radius(mm)

    def _flat_side_world(
        self, pkg: _PkgCylinder, world_axis_deg: float,
    ) -> tuple[float, float] | None:
        """The flat side as ``(normal_angle_deg, offset_holes)`` in world space.

        ``None`` for a full circle (no flat side). ``flat_normal`` is given in
        the package's *local* frame (the same frame the lead offsets use), so we
        rotate it by the local→world rotation. That rotation is the difference
        between the world pad axis (``world_axis_deg``) and the local pad axis
        derived from ``pkg.pin_positions()`` — the pads carry the part's
        rotation/flip, so the flat side follows the body automatically.
        """
        if pkg.flat_radius_mm is None:
            return None
        local = pkg.pin_positions()
        if len(local) < 2:
            return None
        axis = local[-1] - local[0]  # Point − Point → Vector
        if axis.length() < 1e-9:
            return None
        local_axis_deg = axis.angle_deg()
        rot = math.radians(world_axis_deg - local_axis_deg)
        n = pkg.flat_normal
        # On the bottom side the body is physically flipped: reflect the flat
        # normal about the package flip axis (the same reflection the pins
        # undergo in pin_world_position), so the flat face turns to the correct
        # side.  Applied as a direction (the linear part; translation ignored).
        if self._side == "bottom":
            n = pkg.flip_transform().apply(n)
        nx = n.x * math.cos(rot) - n.y * math.sin(rot)
        ny = n.x * math.sin(rot) + n.y * math.cos(rot)
        normal_deg = Vector(nx, ny).angle_deg()
        return normal_deg, pkg.flat_radius_mm / DEFAULT_THEME.raster_pitch_mm

    def _radial_disc(
        self,
        centre: Point,
        diam: float,
        neg_angle_deg: float,
        fill: QColor, border: QColor, alpha: float, z: float,
        theme: Theme,
        *,
        flat: tuple[float, float] | None = None,
    ) -> list[QGraphicsItem]:
        """Top-view (looking at the body face) disc centred between the pads.

        Non-polar parts draw a plain filled circle. For polar parts the body
        face shows no end-cap marker (both leads exit the same face), so the
        negative terminal is marked the way a real radial electrolytic is: a
        dark circular segment (a chord-cut slice of the disc) on the side of the
        ``-`` lead, leaving the rest of the disc in the body colour.

        ``flat`` (``(normal_angle_deg, offset_holes)``) cuts a chord off the
        circle — a D-shaped cross-section such as a TO-92 — leaving the body a
        full circle when ``None``.
        """
        r = diam / 2.0
        fill_c = QColor(fill)
        fill_c.setAlphaF(fill_c.alphaF() * alpha)
        body: QAbstractGraphicsShapeItem
        if flat is not None:
            body = self._d_shape(centre, r, flat[0], flat[1])
        else:
            body = QGraphicsEllipseItem(
                QRectF(centre.x - r, centre.y - r, diam, diam),
            )
        body.setZValue(z)
        body.setBrush(QBrush(fill_c))
        body.setPen(_body_pen(border, alpha))
        if not self._is_polar:
            return [body]
        return [body, self._disc_segment(centre, r, neg_angle_deg, theme, alpha, z)]

    def _d_shape(
        self, centre: Point, radius: float, normal_deg: float, offset: float,
    ) -> QGraphicsPathItem:
        """A circle with one chord cut off, normal to ``normal_deg`` at distance
        ``offset`` (holes) from the centre. The remaining arc spans the far side;
        the chord is the flat face."""
        # Half-angle subtended by the chord: the chord sits at +offset along the
        # flat normal, so the arc we KEEP runs the long way around the far side.
        c = max(-1.0, min(1.0, offset / radius))
        half = math.degrees(math.acos(c))
        rect = QRectF(-radius, -radius, 2 * radius, 2 * radius)
        path = QPainterPath()
        # Arc from +half to (360 − half) around the back, then the chord closes
        # it. Angles are in the local frame where the flat normal points +x;
        # Qt arc angles are counter-clockwise from +x.
        path.arcMoveTo(rect, half)
        path.arcTo(rect, half, 360.0 - 2.0 * half)
        path.closeSubpath()
        item = QGraphicsPathItem(path)
        t = QTransform()
        t.translate(centre.x, centre.y)
        t.rotate(normal_deg)
        item.setTransform(t)
        return item

    def _disc_segment(
        self,
        centre: Point,
        radius: float,
        neg_angle_deg: float,
        theme: Theme,
        alpha: float,
        z: float,
    ) -> QGraphicsItem:
        """A dark circular segment cut by a chord perpendicular to the lead
        axis, on the negative-lead side — the radial ``-`` polarity marker."""
        # Chord at 0.45·radius from the centre → the slice is ~the outer third
        # of the disc, on the side ``neg_angle_deg`` points to (local +x after
        # the rotation below).
        d = 0.45 * radius
        half = math.degrees(math.acos(max(-1.0, min(1.0, d / radius))))
        rect = QRectF(-radius, -radius, 2 * radius, 2 * radius)
        path = QPainterPath()
        path.arcMoveTo(rect, -half)
        path.arcTo(rect, -half, 2 * half)
        path.closeSubpath()
        seg = QGraphicsPathItem(path)
        dark = QColor(theme.body_diode_cathode_color)
        dark.setAlphaF(alpha)
        seg.setBrush(QBrush(dark))
        seg.setPen(_body_pen(dark, alpha, width=0.0))
        seg.setZValue(z + 0.01)
        t = QTransform()
        t.translate(centre.x, centre.y)
        t.rotate(neg_angle_deg)
        seg.setTransform(t)
        return seg

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
