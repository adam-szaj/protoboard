# SPDX-License-Identifier: MIT
"""Per-package body shape renderers.

A component's body is drawn by a :class:`BodyShape`; ``body_shape_for`` picks
the right one.  The parametric package model (:class:`PackageBody`) is the
source of truth for every multi-pin in-line body — DIP / SIL / dev-board
modules synthesize a prism package (see
:func:`~protoboard.domain.components.package.from_footprint.prism_for_footprint`)
and render through it.  The remaining concrete shapes are hand-coded
``QPainterPath`` renderers for the special transistor/power packages
(``GenericBody``, ``TO92Body``, ``TO220Body``).

Usage::

    shape = body_shape_for(defn)
    items = shape.draw(positions, fill, border, alpha, z, theme)
    for item in items:
        scene.addItem(item)
"""

from __future__ import annotations

import math
from abc import ABC, abstractmethod

from PySide6.QtCore import QRectF
from PySide6.QtGui import QBrush, QColor, QPainterPath, QPen, QTransform
from PySide6.QtWidgets import (
    QGraphicsEllipseItem,
    QGraphicsItem,
    QGraphicsLineItem,
    QGraphicsPathItem,
    QGraphicsRectItem,
)

from protoboard.domain.components.definition import (
    ComponentClass,
    ComponentDefinition,
)
from protoboard.domain.components.footprints import (
    SingleRow,
    TO220Footprint,
    TwoPin,
)
from protoboard.domain.components.instance import ComponentInstance
from protoboard.domain.components.package.body import (
    DiscProfile,
    EdgeStyle,
    LeadArrangement,
)
from protoboard.domain.components.package.family import PackageFamily
from protoboard.domain.components.package.from_footprint import prism_for_footprint
from protoboard.domain.components.package.shapes import (
    Cylinder as _PkgCylinder,
    Disc as _PkgDisc,
    Package,
    Prism as _PkgPrism,
)
from protoboard.domain.layout.placement import ComponentPlacement
from protoboard.geometry import Point
from protoboard.gui.renderers.prism_silhouette import silhouette_for
from protoboard.gui.theme import DEFAULT_THEME, Theme

_big_body_alpha: float = 0.90
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

    def drawn_pin_numbers(
        self,
        positions: list[Point],
        theme: Theme = DEFAULT_THEME,
    ) -> frozenset[int]:
        """Pin numbers whose markers are already drawn by this shape.

        component_renderer skips its own pin markers for these pins to avoid
        double-drawing (e.g. CylinderBody in vertical mode draws the circle
        itself and does not want the generic dot on top).
        """
        return frozenset()


# ---------------------------------------------------------------------------
# Helper
# ---------------------------------------------------------------------------

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

        fill = QColor(0x40, 0x40, 0x40)
        fill.setAlphaF(alpha*_big_body_alpha)
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
        perp_y = -row_dx / perp_len

        mm = theme.raster_pitch_mm
        w = theme.to92_body_width_mm  / mm
        h = theme.to92_body_height_mm / mm
        body_offset = 0 # 0.50 / mm   # ~1 mm gap from pin row to body edge

        # Body centre
        cx = p_mid.x + perp_x * body_offset
        # cx = p_mid.x + perp_x * (h / 2) + 2
        cy = p_mid.y + perp_y * body_offset
        # cy = p_mid.y + perp_y * (h / 2)

        # Angle of the pin row
        row_angle = math.degrees(math.atan2(row_dy, row_dx))

        # D-shape path (flat on pin side, rounded on top)
        path = QPainterPath()
        # In local frame: flat bottom at y=+h/2, rounded top at y=-h/2
        # bottom flat edge
        # print(f"w/2: {w/2} h: {h/2}")
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
        # print(f"cx: {cx} cy: {cy}")
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
# TO220Body — voltage regulator / power transistor in TO-220
#
#  [1] [2] [3]
#   |   |   |
#  ╔═══════════╗
#  ║           ║  ← rectangular body + flat metal tab on top
#  ╚═══════════╝
#  ════════════    ← tab (thinner rect)
# ---------------------------------------------------------------------------

class TO220Body(BodyShape):
    def draw(self, positions, fill, border, alpha, z, theme=DEFAULT_THEME):
        if len(positions) < 3:
            return GenericBody().draw(positions, fill, border, alpha, z, theme)

        row_dx = positions[2].x - positions[0].x
        row_dy = positions[2].y - positions[0].y
        perp_len = math.hypot(row_dx, row_dy)
        if perp_len < 1e-6:
            return GenericBody().draw(positions, fill, border, alpha, z, theme)

        # Perpendicular unit vector pointing "away" from user (left of row dir)
        perp_x = -row_dy / perp_len
        perp_y =  row_dx / perp_len

        p_mid = positions[1]
        row_angle = math.degrees(math.atan2(row_dy, row_dx))

        mm = theme.raster_pitch_mm
        w     = theme.to220_body_width_mm  / mm
        h     = theme.to220_body_height_mm / mm
        tab_h = theme.to220_tab_height_mm  / mm
        body_offset = -1.50 / mm   # ~1 mm gap from pin row to body edge

        # Body centre (rectangular, no D-shape)
        cx = p_mid.x - perp_x * (body_offset + h / 2)
        cy = p_mid.y - perp_y * (body_offset + h / 2)

        # Tab centre (on top of body)
        tx = p_mid.x - perp_x * (body_offset + h - tab_h / 2)
        ty = p_mid.y - perp_y * (body_offset + h - tab_h / 2)

        items: list[QGraphicsItem] = []
        lead = _lead_pen(theme, alpha)
        bp = _body_pen(border, alpha)
        r = theme.body_corner_radius

        # Lead lines
        for p in positions:
            ex = p.x + perp_x * body_offset
            ey = p.y + perp_y * body_offset
            line = QGraphicsLineItem(p.x, p.y, ex, ey)
            line.setPen(lead)
            line.setZValue(z - 0.01)
            items.append(line)


        # Main body (rectangular)
        body_path = QPainterPath()
        body_path.addRoundedRect(QRectF(-w / 2, -h / 2, w, h), r, r)
        body = QGraphicsPathItem(body_path)
        fill = QColor(0x40, 0x40, 0x40)
        fill.setAlphaF(alpha * _big_body_alpha)
        body.setBrush(QBrush(fill))
        body.setPen(bp)
        body.setZValue(z)
        t_body = QTransform()
        t_body.translate(cx, cy)
        t_body.rotate(row_angle)
        body.setTransform(t_body)
        items.append(body)

        # Metal tab (slightly wider, darker)
        tab_path = QPainterPath()
        overhang = 0.0 / mm
        tab_path.addRoundedRect(QRectF(-w / 2 - overhang, -tab_h / 2, w + 2 * overhang, tab_h), 0.1 / mm, 0.1 / mm)
        tab = QGraphicsPathItem(tab_path)
        tab_color = QColor(0xdd,0xdd,0xdd)
        tab_color.setAlphaF(alpha * _big_body_alpha)
        tab.setBrush(QBrush(tab_color))
        tab.setPen(_body_pen(border, alpha, width=theme.body_tab_pen_width))
        tab.setZValue(z + 0.005)
        t_tab = QTransform()
        t_tab.translate(tx, ty)
        t_tab.rotate(row_angle)
        tab.setTransform(t_tab)
        items.append(tab)

        return items


# ---------------------------------------------------------------------------
# Dispatcher
# ---------------------------------------------------------------------------

_TRANSISTOR_CLASSES = frozenset({
    ComponentClass.TRANSISTOR_NPN,
    ComponentClass.TRANSISTOR_PNP,
    ComponentClass.TRANSISTOR_NMOS,
    ComponentClass.TRANSISTOR_PMOS,
    ComponentClass.TRANSISTOR_NJFET,
    ComponentClass.TRANSISTOR_PJFET,
})

# ---------------------------------------------------------------------------
# PackageBody — draw the body from a parametric Package (spec model)
#
# When a ComponentDefinition carries a ``package`` (the new parametric model,
# see docs/footprints/spec.md) the body is drawn directly from that geometry
# instead of the legacy per-footprint renderers.  Cylinders render as a
# side-view capsule between the leads (the axial-resistor / diode look);
# every other shape renders its ``bbox()`` rectangle (in holes) centred on
# the pin cloud.  This is the rendering counterpart to the package wiring in
# DRC / place_tool / library, on the same optional-field + fallback footing.
# ---------------------------------------------------------------------------

class PackageBody(BodyShape):
    """Render a component body from its :class:`Package` geometry.

    Presentation (colour, edge) comes from the package; polarity and which
    pad is the anchor are *not* package properties — a package is class
    agnostic (the same DO-41 hosts a diode, a Zener or a fuse), so the
    polarity marker is driven by ``component_class`` and ``vertical_flipped``
    exactly as the legacy ``TwoPinParametricBody`` did.
    """

    # ComponentClasses that show a polarity marker — diodes get a cathode
    # stripe (axial) or a solid-vs-hollow disc (radial); polar electrolytics
    # share the treatment.  Mirrors TwoPinParametricBody._POLAR_CLASSES.
    _POLAR_CLASSES = frozenset({
        ComponentClass.DIODE,
        ComponentClass.DIODE_ZENER,
        ComponentClass.DIODE_LED,
        ComponentClass.CAPACITOR_POLAR,
    })

    def __init__(
        self,
        package: Package,
        component_class: ComponentClass = ComponentClass.OTHER,
        vertical_flipped: bool = False,
    ) -> None:
        self._pkg = package
        self._component_class = component_class
        self._vertical_flipped = vertical_flipped

    @property
    def _is_polar(self) -> bool:
        return self._component_class in self._POLAR_CLASSES

    @property
    def _arrangement(self) -> LeadArrangement:
        return getattr(self._pkg, "arrangement", LeadArrangement.AXIAL)

    def _resolve_colors(
        self, fill: QColor, border: QColor,
    ) -> tuple[QColor, QColor]:
        """Body fill/border for this package.

        When the package declares a ``color`` it is the fill, and the border is
        derived by darkening it — the package fully describes its own appearance
        (no Material → theme-palette indirection).  Otherwise the caller-supplied
        colours are used unchanged.
        """
        pkg_color = getattr(self._pkg, "color", None)
        if pkg_color is None:
            return fill, border
        body_fill = QColor(pkg_color)
        if not body_fill.isValid():
            return fill, border
        return body_fill, _darken(body_fill)

    def drawn_pin_numbers(
        self,
        positions: list[Point],
        theme: Theme = DEFAULT_THEME,
    ) -> frozenset[int]:
        # The radial top-view body sits centred between the pads and may cover
        # one or both of them depending on the spacing. Report every pin that
        # falls under the body silhouette so the renderer does not draw its own
        # marker on top of it.
        if self._arrangement is not LeadArrangement.RADIAL or len(positions) < 2:
            return frozenset()
        mm = theme.raster_pitch_mm
        mid = Point(
            (positions[0].x + positions[1].x) / 2.0,
            (positions[0].y + positions[1].y) / 2.0,
        )
        radius = self._radial_body_radius(mm)
        covered = {
            i + 1
            for i, p in enumerate(positions)
            if math.hypot(p.x - mid.x, p.y - mid.y) <= radius + 1e-9
        }
        return frozenset(covered)

    def _radial_body_radius(self, mm: float) -> float:
        """Half-extent (in holes) of the radial body across the lead axis."""
        pkg = self._pkg
        if isinstance(pkg, (_PkgCylinder, _PkgDisc)):
            return pkg.diameter_mm / mm / 2.0
        # Prism: half its length (the bbox dimension along the lead axis).
        return pkg.bbox().height / 2.0

    def draw(
        self,
        positions: list[Point],
        fill: QColor,
        border: QColor,
        alpha: float,
        z: float,
        theme: Theme = DEFAULT_THEME,
    ) -> list[QGraphicsItem]:
        if not positions:
            return []
        fill, border = self._resolve_colors(fill, border)
        # Radial 2-pin: top-view silhouette on the anchor pad (vertical_flipped
        # swaps which pad anchors).  Everything else is a side view.
        if self._arrangement is LeadArrangement.RADIAL and len(positions) >= 2:
            if self._vertical_flipped:
                p1, p2 = positions[1], positions[0]
            else:
                p1, p2 = positions[0], positions[1]
            if math.hypot(p2.x - p1.x, p2.y - p1.y) >= 1e-9:
                # The ``-`` marker sits towards the physical cathode (pin 2),
                # independent of the p1/p2 swap above.
                neg_pin = positions[1]
                return self._draw_radial(
                    p1, p2, neg_pin, fill, border, alpha, z, theme,
                )
        if isinstance(self._pkg, _PkgCylinder) and len(positions) >= 2:
            return self._draw_cylinder(
                self._pkg, positions, fill, border, alpha, z, theme,
            )
        # A multi-pin prism (DIP / SIL / quad) draws its class-agnostic
        # silhouette — notch, slots, outer strips, pin-1 marker — placed onto
        # the pins.  Layouts without a dedicated silhouette fall to the bbox.
        if isinstance(self._pkg, _PkgPrism) and len(positions) >= 2:
            prism_items = self._draw_prism(
                self._pkg, positions, fill, border, alpha, z, theme,
            )
            if prism_items is not None:
                return prism_items
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
        dx, dy = p2.x - p1.x, p2.y - p1.y
        dist = math.hypot(dx, dy)
        if dist < 1e-9:
            return self._draw_bbox(positions, fill, border, alpha, z, theme)

        mid_x = (p1.x + p2.x) / 2.0
        mid_y = (p1.y + p2.y) / 2.0
        angle_deg = math.degrees(math.atan2(dy, dx))

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

    # ------------------------------------------------------------------
    # Radial — top-view silhouette centred between the two pads
    # ------------------------------------------------------------------

    def _draw_radial(
        self,
        p1: Point, p2: Point, neg_pin: Point,
        fill: QColor, border: QColor, alpha: float, z: float,
        theme: Theme,
    ) -> list[QGraphicsItem]:
        items: list[QGraphicsItem] = []
        lead = QGraphicsLineItem(p1.x, p1.y, p2.x, p2.y)
        lead.setPen(_lead_pen(theme, alpha))
        lead.setZValue(z - 0.01)
        items.append(lead)

        mm = theme.raster_pitch_mm
        # The radial body sits over both pads, so it is centred on their
        # geometric midpoint (not on the anchor pad).
        mid = Point((p1.x + p2.x) / 2.0, (p1.y + p2.y) / 2.0)
        # Direction from the body centre towards the negative lead — the side
        # the ``-`` segment marker goes on.
        neg_angle = math.degrees(math.atan2(neg_pin.y - mid.y, neg_pin.x - mid.x))

        pkg = self._pkg
        if isinstance(pkg, _PkgDisc):
            # A disc cap mounts on its edge: from above it is a thin side
            # profile, not the round face — a lens (its diameter along the lead
            # axis, its thickness across).
            axis_angle = math.degrees(math.atan2(p2.y - p1.y, p2.x - p1.x))
            items.append(self._radial_disc_side(
                mid, pkg.diameter_mm / mm, pkg.thickness_mm / mm, axis_angle,
                pkg.profile, fill, border, alpha, z, theme,
            ))
            return items
        if isinstance(pkg, _PkgCylinder):
            diam = pkg.diameter_mm / mm
            items.extend(self._radial_disc(
                mid, diam, neg_angle, fill, border, alpha, z, theme,
            ))
            return items

        # Prism radial → rectangle aligned with the lead direction, centred on
        # the midpoint between the pads.
        bb = pkg.bbox()
        length_r, width_r = bb.height, bb.width
        angle_deg = math.degrees(math.atan2(p2.y - p1.y, p2.x - p1.x))
        rect_path = QPainterPath()
        r = _package_corner_radius(pkg) / mm
        rect_path.addRoundedRect(
            QRectF(-length_r / 2.0, -width_r / 2.0, length_r, width_r), r, r,
        )
        body = QGraphicsPathItem(rect_path)
        fill_c = QColor(fill)
        fill_c.setAlphaF(fill_c.alphaF() * alpha)
        body.setBrush(QBrush(fill_c))
        body.setPen(_body_pen(border, alpha))
        body.setZValue(z)
        t = QTransform()
        t.translate(mid.x, mid.y)
        t.rotate(angle_deg)
        body.setTransform(t)
        items.append(body)
        return items

    def _radial_disc(
        self,
        centre: Point,
        diam: float,
        neg_angle_deg: float,
        fill: QColor, border: QColor, alpha: float, z: float,
        theme: Theme,
    ) -> list[QGraphicsItem]:
        """Top-view (looking at the body face) disc centred between the pads.

        Non-polar parts draw a plain filled circle. For polar parts the body
        face shows no end-cap marker (both leads exit the same face), so the
        negative terminal is marked the way a real radial electrolytic is: a
        dark circular segment (a chord-cut slice of the disc) on the side of the
        ``-`` lead, leaving the rest of the disc in the body colour.
        """
        r = diam / 2.0
        circle = QGraphicsEllipseItem(
            QRectF(centre.x - r, centre.y - r, diam, diam),
        )
        circle.setZValue(z)
        fill_c = QColor(fill)
        fill_c.setAlphaF(fill_c.alphaF() * alpha)
        circle.setBrush(QBrush(fill_c))
        circle.setPen(_body_pen(border, alpha))
        if not self._is_polar:
            return [circle]
        return [circle, self._disc_segment(centre, r, neg_angle_deg, theme, alpha, z)]

    def _radial_disc_side(
        self,
        centre: Point,
        length: float,   # the disc diameter, along the lead axis
        width: float,    # the disc thickness, across the lead axis
        axis_angle_deg: float,
        profile: DiscProfile,
        fill: QColor, border: QColor, alpha: float, z: float,
        theme: Theme,
    ) -> QGraphicsItem:
        """Side-view silhouette of a disc cap mounted on its edge: a lens
        ``length`` long bulging to ``width`` across.  ``CANDY`` meets in sharp
        tips on the lead axis; ``FLAT`` has the tips ground down to rounded
        ends.  Quadratic Béziers reach half their control offset at the apex,
        so a control point at ``width`` yields a mid bulge of ``width / 2``."""
        half_l = length / 2.0
        half_w = width / 2.0
        path = QPainterPath()
        if profile is DiscProfile.CANDY:
            # Strongly bulged lens meeting in sharp tips on the lead axis: the
            # arc reaches full half-thickness at the centre. Quadratic Béziers
            # reach half their control offset, so control y = ±width gives the
            # ±width/2 bulge.
            path.moveTo(-half_l, 0.0)
            path.quadTo(0.0, -width, half_l, 0.0)
            path.quadTo(0.0, width, -half_l, 0.0)
            path.closeSubpath()
        else:
            # FLAT: a flat disc (usually thicker, less bulged) is well
            # approximated by a smooth ellipse of diameter × thickness — a
            # gentle, large-radius lens with rounded ends rather than tips.
            path.addEllipse(QRectF(-half_l, -half_w, length, width))
        lens = QGraphicsPathItem(path)
        fill_c = QColor(fill)
        fill_c.setAlphaF(fill_c.alphaF() * alpha)
        lens.setBrush(QBrush(fill_c))
        lens.setPen(_body_pen(border, alpha))
        lens.setZValue(z)
        t = QTransform()
        t.translate(centre.x, centre.y)
        t.rotate(axis_angle_deg)
        lens.setTransform(t)
        return lens

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
    # Prism — DIP / SIL / quad silhouette placed onto the pins
    # ------------------------------------------------------------------

    def _draw_prism(
        self,
        pkg: _PkgPrism,
        positions: list[Point],
        fill: QColor,
        border: QColor,
        alpha: float,
        z: float,
        theme: Theme,
    ) -> list[QGraphicsItem] | None:
        """Render a multi-pin prism body from its column silhouette.

        Returns ``None`` when the prism has no dedicated silhouette (an odd
        column count) so the caller falls back to the plain bbox rectangle.
        The silhouette is built in the package-local frame (pin 1 at the
        origin) and placed with the pin1→pin2 transform, mirroring the legacy
        ``Svg*Body`` generators.
        """
        sil = silhouette_for(pkg)
        if sil is None:
            return None
        drawing = sil.build(pkg, theme)

        # Place: translate to the anchor pad, rotate by the pin1→pin2 heading.
        # The package-local +y axis runs pin1→pin2 down the first column, so
        # the rotation is derived the same way the SVG bodies did.
        p1 = positions[0]
        dx = positions[1].x - p1.x
        dy = positions[1].y - p1.y
        angle_deg = -math.degrees(math.atan2(dx, dy)) if (dx or dy) else 0.0
        place = QTransform()
        place.translate(p1.x, p1.y)
        place.rotate(angle_deg)

        fill_c = QColor(fill)
        fill_c.setAlphaF(fill_c.alphaF() * alpha)
        items: list[QGraphicsItem] = []

        body = QGraphicsPathItem(place.map(drawing.body))
        body.setBrush(QBrush(fill_c))
        body.setPen(_body_pen(border, alpha))
        body.setZValue(z)
        items.append(body)

        if drawing.marker is not None:
            marker = QGraphicsPathItem(place.map(drawing.marker))
            marker.setBrush(QBrush(_body_pen(border, alpha).color()))
            marker.setPen(_body_pen(border, alpha, width=0.0))
            marker.setZValue(z + 0.01)
            items.append(marker)
        return items

    # ------------------------------------------------------------------
    # Generic — bbox() rectangle in holes (prism, devboard, fallback)
    # ------------------------------------------------------------------

    def _draw_bbox(
        self,
        positions: list[Point],
        fill: QColor,
        border: QColor,
        alpha: float,
        z: float,
        theme: Theme,
    ) -> list[QGraphicsItem]:
        try:
            box = self._pkg.bbox()
        except ValueError:
            # bbox needs dims/margin the package doesn't carry — fall back to
            # a rectangle hugging the pin cloud (same as GenericBody).
            generic: list[QGraphicsItem] = GenericBody().draw(
                positions, fill, border, alpha, z, theme,
            )
            return generic

        # bbox() is anchored at pin 1 ([0, 0]); shift it onto the placed pins.
        anchor = positions[0]
        x0 = anchor.x + box.x
        y0 = anchor.y + box.y
        r = _package_corner_radius(self._pkg) / theme.raster_pitch_mm
        rect = QRectF(x0, y0, box.width, box.height)
        fill_c = QColor(fill)
        fill_c.setAlphaF(fill_c.alphaF() * alpha)
        if r > 0:
            path = QPainterPath()
            path.addRoundedRect(rect, r, r)
            path_item = QGraphicsPathItem(path)
            path_item.setBrush(QBrush(fill_c))
            path_item.setPen(_body_pen(border, alpha))
            path_item.setZValue(z)
            return [path_item]
        rect_item = QGraphicsRectItem(rect)
        rect_item.setBrush(QBrush(fill_c))
        rect_item.setPen(_body_pen(border, alpha))
        rect_item.setZValue(z)
        return [rect_item]


def _darken(c: QColor) -> QColor:
    """Border colour derived from a body fill — a darker shade of it."""
    return c.darker(150)


def _package_corner_radius(pkg: Package) -> float:
    """Corner radius (mm) declared by the package shape, or 0.

    A ``SHARP`` edge style forces square corners regardless of any declared
    radius; ``ROUNDED`` uses the shape's radius field.
    """
    if getattr(pkg, "edge", None) is EdgeStyle.SHARP:
        return 0.0
    if isinstance(pkg, _PkgCylinder):
        return pkg.edge_radius_mm
    if isinstance(pkg, _PkgPrism):
        return pkg.corner_radius_mm
    return 0.0


def body_shape_for(
    defn: ComponentDefinition,
    placement: ComponentPlacement | None = None,
    instance: ComponentInstance | None = None,
    package_families: dict[str, PackageFamily] | None = None,
) -> BodyShape:
    """Return the appropriate :class:`BodyShape` for *defn*.

    A parametric package (resolved from the instance/definition/catalogue via
    :func:`resolve_component_package`) is the source of truth for the body and
    renders through :class:`PackageBody`; a two-pin part without a package draws
    a generic bbox.

    Larger packages keep their dedicated renderers: TO-92 for 3-pin
    transistor, TO-220 for power packages, SVG-based DIP for two-row
    dual in-line packages, SIL header for three-or-more in-line headers.
    """
    cls = defn.component_class
    vf = placement.vertical_flipped if placement is not None else False

    # A parametric package (new spec model) is the source of truth for the
    # body geometry when one resolves: the instance's package_variant, the
    # definition's package_family_ref (both against the package-family
    # catalogue), or an inline defn.package — see resolve_component_package.
    # Polarity / anchor come from the class and placement, not the
    # (class-agnostic) package.
    from protoboard.domain.components.package import resolve_component_package
    pkg = resolve_component_package(defn, instance, package_families or {})
    if pkg is not None:
        return PackageBody(pkg, component_class=cls, vertical_flipped=vf)

    fp  = defn.footprint

    if isinstance(fp, TO220Footprint):
        return TO220Body()

    if isinstance(fp, TwoPin):
        # Two-pin bodies come from the package model (resolved above); without
        # a package the part draws a generic bbox.
        return GenericBody()

    if isinstance(fp, SingleRow) and fp.n == 3 and cls in _TRANSISTOR_CLASSES:
        # A 3-pin transistor in a SIL footprint is a TO-92.
        return TO92Body()

    # In-line multi-pin footprints (DIP / SIL header / dev-board module) with no
    # catalogue package synthesize a prism whose pins match the footprint and
    # render through the parametric PackageBody (DIP notch, SIL slots, or a
    # plain dev-board bbox) — the same path a catalogued package takes.
    synth = prism_for_footprint(fp)
    if synth is not None:
        return PackageBody(synth, component_class=cls, vertical_flipped=vf)

    return GenericBody()
