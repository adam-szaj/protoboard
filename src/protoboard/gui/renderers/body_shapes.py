# SPDX-License-Identifier: MIT
"""Per-package body shape renderers.

Each BodyShape subclass draws the physical outline of a component package
(axial resistor, radial capacitor, TO-92 transistor, DIP IC, SIL header …)
into a QGraphicsScene using the pin world positions already computed by the
caller.

Usage::

    shape = body_shape_for(defn, placement)
    items = shape.draw(positions, fill, border, alpha, z, theme)
    for item in items:
        scene.addItem(item)
"""

from __future__ import annotations

import math
from abc import ABC, abstractmethod

from PySide6.QtCore import QRectF, Qt
from PySide6.QtGui import QBrush, QColor, QPainterPath, QPen, QTransform
from PySide6.QtWidgets import (
    QGraphicsEllipseItem,
    QGraphicsItem,
    QGraphicsLineItem,
    QGraphicsPathItem,
    QGraphicsRectItem,
)

from protoboard.domain.components.definition import ComponentClass, ComponentDefinition
from protoboard.domain.components.footprints import DualRow, SingleRow
from protoboard.domain.layout.placement import ComponentPlacement
from protoboard.geometry import Point
from protoboard.gui.theme import DEFAULT_THEME, Theme


# ---------------------------------------------------------------------------
# Base class
# ---------------------------------------------------------------------------

class BodyShape(ABC):
    @abstractmethod
    def draw(
        self,
        positions: list[Point],
        fill: QColor,
        border: QColor,
        alpha: float,
        z: float,
        theme: Theme = DEFAULT_THEME,
    ) -> list[QGraphicsItem]:
        """Return scene items (not yet added to scene)."""


# ---------------------------------------------------------------------------
# Helper
# ---------------------------------------------------------------------------

def _a(base_alpha: int, alpha: float) -> int:
    return max(0, min(255, int(base_alpha * alpha)))


def _lead_pen(theme: Theme, alpha: float) -> QPen:
    c = QColor(theme.lead_color)
    c.setAlphaF(alpha)
    return QPen(c, theme.lead_width)


def _body_pen(border: QColor, alpha: float, width: float = 0.06) -> QPen:
    c = QColor(border)
    c.setAlphaF(alpha)
    return QPen(c, width)


# ---------------------------------------------------------------------------
# GenericBody — existing bbox rectangle (fallback)
# ---------------------------------------------------------------------------

class GenericBody(BodyShape):
    def draw(self, positions, fill, border, alpha, z, theme=DEFAULT_THEME):
        if not positions:
            return []
        margin = 0.35
        xs = [p.x for p in positions]
        ys = [p.y for p in positions]
        x0 = min(xs) - margin
        y0 = min(ys) - margin
        w = max(xs) - min(xs) + 2 * margin
        h = max(ys) - min(ys) + 2 * margin
        rect = QGraphicsRectItem(QRectF(x0, y0, w, h))
        rect.setBrush(QBrush(fill))
        rect.setPen(_body_pen(border, alpha))
        rect.setZValue(z)
        return [rect]


# ---------------------------------------------------------------------------
# AxialBody — resistor / inductor / fuse (axial through-hole)
#
#   pin1 ──────╔═══body═══╗────── pin2
# ---------------------------------------------------------------------------

class AxialBody(BodyShape):
    def draw(self, positions, fill, border, alpha, z, theme=DEFAULT_THEME):
        if len(positions) < 2:
            return GenericBody().draw(positions, fill, border, alpha, z, theme)

        p1, p2 = positions[0], positions[1]
        dx = p2.x - p1.x
        dy = p2.y - p1.y
        dist = math.hypot(dx, dy)
        if dist < 1e-6:
            return GenericBody().draw(positions, fill, border, alpha, z, theme)

        mid_x = (p1.x + p2.x) / 2.0
        mid_y = (p1.y + p2.y) / 2.0
        angle_deg = math.degrees(math.atan2(dy, dx))

        items: list[QGraphicsItem] = []
        lead = _lead_pen(theme, alpha)
        bp = _body_pen(border, alpha)

        # Lead line (full pin-to-pin)
        line = QGraphicsLineItem(p1.x, p1.y, p2.x, p2.y)
        line.setPen(lead)
        line.setZValue(z - 0.01)
        items.append(line)

        # Body rectangle in local frame, then transformed
        body_len = dist * theme.axial_body_length_ratio
        body_w = theme.axial_body_width
        path = QPainterPath()
        r = theme.body_corner_radius
        path.addRoundedRect(
            QRectF(-body_len / 2, -body_w / 2, body_len, body_w), r, r
        )
        body = QGraphicsPathItem(path)
        body.setBrush(QBrush(fill))
        body.setPen(bp)
        body.setZValue(z)

        t = QTransform()
        t.translate(mid_x, mid_y)
        t.rotate(angle_deg)
        body.setTransform(t)
        items.append(body)

        return items


# ---------------------------------------------------------------------------
# RadialBody — capacitor / generic 2-pin radial
#
#       ○  (circular body above pin midpoint)
#      / \
#  [p1]   [p2]
# ---------------------------------------------------------------------------

class RadialBody(BodyShape):
    def draw(self, positions, fill, border, alpha, z, theme=DEFAULT_THEME):
        if len(positions) < 2:
            return GenericBody().draw(positions, fill, border, alpha, z, theme)

        p1, p2 = positions[0], positions[1]
        mid_x = (p1.x + p2.x) / 2.0
        # Place body above midpoint (toward lower Y, which is "up" on screen)
        dy_sep = abs(p2.y - p1.y) if abs(p2.y - p1.y) > 0.1 else abs(p2.x - p1.x)
        body_y = (p1.y + p2.y) / 2.0 - max(0.5, dy_sep * 0.3)

        items: list[QGraphicsItem] = []
        lead = _lead_pen(theme, alpha)
        bp = _body_pen(border, alpha)

        # Lead lines from pins to body center
        for p in (p1, p2):
            line = QGraphicsLineItem(p.x, p.y, mid_x, body_y)
            line.setPen(lead)
            line.setZValue(z - 0.01)
            items.append(line)

        # Circular body
        r = theme.radial_body_radius
        circle = QGraphicsEllipseItem(QRectF(mid_x - r, body_y - r, 2 * r, 2 * r))
        circle.setBrush(QBrush(fill))
        circle.setPen(bp)
        circle.setZValue(z)
        items.append(circle)

        return items


# ---------------------------------------------------------------------------
# DiodeBody — axial diode with directional marker
#
#   A ──────╔▷|╗────── K
# ---------------------------------------------------------------------------

class DiodeBody(BodyShape):
    def draw(self, positions, fill, border, alpha, z, theme=DEFAULT_THEME):
        if len(positions) < 2:
            return GenericBody().draw(positions, fill, border, alpha, z, theme)

        p1, p2 = positions[0], positions[1]
        dx = p2.x - p1.x
        dy = p2.y - p1.y
        dist = math.hypot(dx, dy)
        if dist < 1e-6:
            return GenericBody().draw(positions, fill, border, alpha, z, theme)

        mid_x = (p1.x + p2.x) / 2.0
        mid_y = (p1.y + p2.y) / 2.0
        angle_deg = math.degrees(math.atan2(dy, dx))

        items: list[QGraphicsItem] = []
        lead = _lead_pen(theme, alpha)
        bp = _body_pen(border, alpha)

        # Lead line
        line = QGraphicsLineItem(p1.x, p1.y, p2.x, p2.y)
        line.setPen(lead)
        line.setZValue(z - 0.01)
        items.append(line)

        # Diode body: small cylinder (rounded rect) with cathode bar
        body_len = dist * theme.axial_body_length_ratio
        body_w = theme.axial_body_width * 0.85
        r = theme.body_corner_radius

        path = QPainterPath()
        path.addRoundedRect(
            QRectF(-body_len / 2, -body_w / 2, body_len, body_w), r, r
        )
        # Cathode bar — thin rect at the right end
        bar_w = 0.08
        path.addRect(QRectF(body_len / 2 - bar_w, -body_w / 2, bar_w, body_w))

        body = QGraphicsPathItem(path)
        body.setBrush(QBrush(fill))
        body.setPen(bp)
        body.setZValue(z)

        t = QTransform()
        t.translate(mid_x, mid_y)
        t.rotate(angle_deg)
        body.setTransform(t)
        items.append(body)

        return items


# ---------------------------------------------------------------------------
# TO92Body — transistor / small 3-pin package
#
#  [1] [2] [3]
#   |   |   |
#  ╔═══D═══╗   (D-shape: flat bottom, rounded top)
# ---------------------------------------------------------------------------

class TO92Body(BodyShape):
    def draw(self, positions, fill, border, alpha, z, theme=DEFAULT_THEME):
        if len(positions) < 3:
            return GenericBody().draw(positions, fill, border, alpha, z, theme)

        # Pin 2 is the middle pin; body centred above it.
        # "Above" = lower Y value (Qt Y grows downward, but scene coords
        #  can go either way — we use pin positions as reference).
        p_mid = positions[1]
        p_left = positions[0]

        # Determine "up" direction: perpendicular to the pin row, pointing
        # away from the user (negative Y in unrotated component).
        pin_dx = positions[2].x - positions[0].x
        pin_dy = positions[2].y - positions[0].y
        # Perpendicular (rotate 90° CCW)
        perp_len = math.hypot(pin_dx, pin_dy)
        if perp_len < 1e-6:
            return GenericBody().draw(positions, fill, border, alpha, z, theme)
        # Unit vector perpendicular, pointing "above" pins (negative Y side for rot=0)
        ux = -pin_dy / perp_len
        uy = pin_dx / perp_len
        # Ensure it points away from "below" (choose direction by sign)
        # For unrotated TO-92 pins at y=0,1,2 the row is vertical, not horizontal.
        # Recalculate: pin row direction
        row_dx = positions[2].x - positions[0].x
        row_dy = positions[2].y - positions[0].y
        # Perpendicular pointing left of row direction
        perp_x = -row_dy / perp_len
        perp_y = row_dx / perp_len

        w = theme.to92_body_width
        h = theme.to92_body_height
        body_offset = 0.7   # distance from pin row to bottom edge of body

        # Body centre
        cx = p_mid.x + perp_x * (body_offset + h / 2)
        cy = p_mid.y + perp_y * (body_offset + h / 2)

        # Angle of the pin row
        row_angle = math.degrees(math.atan2(row_dy, row_dx))

        # D-shape path (flat on pin side, rounded on top)
        path = QPainterPath()
        # In local frame: flat bottom at y=+h/2, rounded top at y=-h/2
        # bottom flat edge
        path.moveTo(-w / 2, h / 2)
        path.lineTo(w / 2, h / 2)
        # right side up
        path.lineTo(w / 2, 0)
        # semicircle top (from right to left)
        path.arcTo(QRectF(-w / 2, -h / 2, w, h), 0, 180)
        # left side down
        path.lineTo(-w / 2, h / 2)
        path.closeSubpath()

        body = QGraphicsPathItem(path)
        body.setBrush(QBrush(fill))
        body.setPen(_body_pen(border, alpha))
        body.setZValue(z)

        t = QTransform()
        t.translate(cx, cy)
        t.rotate(row_angle)
        body.setTransform(t)

        # Lead lines from each pin to body edge
        items: list[QGraphicsItem] = []
        lead = _lead_pen(theme, alpha)
        for p in positions:
            # End of lead = pin + perp * body_offset
            ex = p.x + perp_x * body_offset
            ey = p.y + perp_y * body_offset
            line = QGraphicsLineItem(p.x, p.y, ex, ey)
            line.setPen(lead)
            line.setZValue(z - 0.01)
            items.append(line)

        items.append(body)
        return items


# ---------------------------------------------------------------------------
# DIPBody — IC in DIP package
#
#  1○ 2○ 3○ 4○
#  ╔═══════════╗
#  ║     ⌒     ║  ← notch marks pin-1 end
#  ╚═══════════╝
#  8○ 7○ 6○ 5○
# ---------------------------------------------------------------------------

class DIPBody(BodyShape):
    def __init__(self, n_per_row: int) -> None:
        self._n = n_per_row

    def draw(self, positions, fill, border, alpha, z, theme=DEFAULT_THEME):
        n = self._n
        if len(positions) < 2 * n:
            return GenericBody().draw(positions, fill, border, alpha, z, theme)

        # Left column: positions[0..n-1], right column: positions[n..2n-1]
        left = positions[:n]
        right = positions[n:]

        # Body bounds: between the two rows + margin
        margin = 0.5
        lx = min(p.x for p in left + right)
        rx = max(p.x for p in left + right)
        ty = min(p.y for p in left + right) - margin
        by = max(p.y for p in left + right) + margin

        bp = _body_pen(border, alpha)
        items: list[QGraphicsItem] = []

        # Main body rectangle via QPainterPath (so we can subtract notch)
        r = theme.body_corner_radius
        notch_r = theme.dip_notch_radius
        mid_x = (lx + rx) / 2.0

        path = QPainterPath()
        path.addRoundedRect(QRectF(lx, ty, rx - lx, by - ty), r, r)
        # Subtract notch (addEllipse subtracts in even-odd fill rule)
        path.addEllipse(QRectF(
            mid_x - notch_r, ty - notch_r,
            2 * notch_r, 2 * notch_r
        ))

        body = QGraphicsPathItem(path)
        body.setBrush(QBrush(fill))
        body.setPen(bp)
        body.setFillRule(Qt.FillRule.OddEvenFill)
        body.setZValue(z)
        items.append(body)

        return items


# ---------------------------------------------------------------------------
# SILBody — single in-line header / connector
#
#  ╔═╤═╤═╤═╗
#  ║ | | | ║
#  [1][2][3][4]
# ---------------------------------------------------------------------------

class SILBody(BodyShape):
    def draw(self, positions, fill, border, alpha, z, theme=DEFAULT_THEME):
        if not positions:
            return []

        slot = theme.sil_slot_size
        items: list[QGraphicsItem] = []
        bp = _body_pen(border, alpha)
        lead = _lead_pen(theme, alpha)

        # Overall housing: tight bbox around all pins + slot/2 margin
        xs = [p.x for p in positions]
        ys = [p.y for p in positions]
        m = slot / 2 + 0.05
        housing = QGraphicsRectItem(QRectF(
            min(xs) - m, min(ys) - m,
            max(xs) - min(xs) + 2 * m,
            max(ys) - min(ys) + 2 * m,
        ))
        housing.setBrush(QBrush(fill))
        housing.setPen(bp)
        housing.setZValue(z)
        items.append(housing)

        # Per-pin slot (small inset square)
        slot_fill = QColor(fill)
        slot_fill.setAlphaF(slot_fill.alphaF() * 0.5)
        slot_border = QColor(border)
        slot_border.setAlphaF(alpha * 0.7)
        slot_pen = QPen(slot_border, 0.04)
        hs = slot / 2
        for p in positions:
            sq = QGraphicsRectItem(QRectF(p.x - hs, p.y - hs, slot, slot))
            sq.setBrush(QBrush(slot_fill))
            sq.setPen(slot_pen)
            sq.setZValue(z + 0.01)
            items.append(sq)

        return items


# ---------------------------------------------------------------------------
# Dispatcher
# ---------------------------------------------------------------------------

_AXIAL_CLASSES = frozenset({
    ComponentClass.RESISTOR,
    ComponentClass.INDUCTOR,
    ComponentClass.FUSE,
    ComponentClass.TRANSFORMER,
})
_RADIAL_CLASSES = frozenset({
    ComponentClass.CAPACITOR_POLAR,
    ComponentClass.CAPACITOR_NONPOLAR,
})
_DIODE_CLASSES = frozenset({
    ComponentClass.DIODE,
    ComponentClass.DIODE_ZENER,
    ComponentClass.DIODE_LED,
})
_TRANSISTOR_CLASSES = frozenset({
    ComponentClass.TRANSISTOR_NPN,
    ComponentClass.TRANSISTOR_PNP,
    ComponentClass.TRANSISTOR_NMOS,
    ComponentClass.TRANSISTOR_PMOS,
    ComponentClass.TRANSISTOR_NJFET,
    ComponentClass.TRANSISTOR_PJFET,
})


def body_shape_for(defn: ComponentDefinition) -> BodyShape:
    """Return the appropriate BodyShape for *defn*."""
    fp = defn.footprint
    cls = defn.component_class

    if isinstance(fp, SingleRow):
        if fp.n == 2:
            if cls in _AXIAL_CLASSES:
                return AxialBody()
            if cls in _DIODE_CLASSES:
                return DiodeBody()
            if cls in _RADIAL_CLASSES:
                return RadialBody()
        if fp.n == 3 and cls in _TRANSISTOR_CLASSES:
            return TO92Body()
        # Generic single-row connector / switch / other
        return SILBody()

    if isinstance(fp, DualRow):
        return DIPBody(fp.n_per_row)

    return GenericBody()
