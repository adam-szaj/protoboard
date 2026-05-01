# SPDX-License-Identifier: MIT
"""Per-package body shape renderers.

Two rendering backends are available and selected automatically:

SvgBodyShape (preferred)
    Loads a parametric SVG template from ``assets/components/``, patches
    lead lengths, body colour, and per-pin net colours in-place via
    ElementTree, then hands the modified XML to QSvgRenderer →
    QGraphicsSvgItem.  A single QTransform (translate pin-1 world pos +
    rotate to pin-2 direction) positions the item on the scene.

    Templates live in  <project_root>/assets/components/<name>.svg
    and follow the id/data-* conventions documented in that directory's
    README.md.

QPainterPath fallback (GenericBody and friends)
    Used when no SVG template is found or when the package type is not
    mapped.  These are the original hand-coded shapes.

Usage::

    shape = body_shape_for(defn)
    items = shape.draw(positions, fill, border, alpha, z, theme)
    for item in items:
        scene.addItem(item)
"""

from __future__ import annotations

import math
import xml.etree.ElementTree as ET
from abc import ABC, abstractmethod
from pathlib import Path

from PySide6.QtCore import QByteArray, QRectF, Qt
from PySide6.QtGui import QBrush, QColor, QPainterPath, QPen, QTransform
from PySide6.QtSvg import QSvgRenderer
from PySide6.QtSvgWidgets import QGraphicsSvgItem
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
# Assets directory
# ---------------------------------------------------------------------------

_ASSETS_DIR = Path(__file__).parent.parent.parent.parent.parent.parent / "assets" / "components"
# Resolves to <project_root>/assets/components regardless of install layout.
# Fallback: walk up until we find the directory.
def _find_assets() -> Path:
    candidate = Path(__file__).resolve()
    for _ in range(8):
        candidate = candidate.parent
        d = candidate / "assets" / "components"
        if d.is_dir():
            return d
    return _ASSETS_DIR

_ASSETS: Path = _find_assets()


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
# SVG namespace helper
# ---------------------------------------------------------------------------

_SVG_NS = "http://www.w3.org/2000/svg"
ET.register_namespace("", _SVG_NS)


def _svg_tag(local: str) -> str:
    return f"{{{_SVG_NS}}}{local}"


def _find_id(root: ET.Element, elem_id: str) -> ET.Element | None:
    """Find first element with matching id anywhere in the tree."""
    for el in root.iter():
        if el.get("id") == elem_id:
            return el
    return None


# ---------------------------------------------------------------------------
# SvgBodyShape — SVG-template based renderer
# ---------------------------------------------------------------------------

class SvgBodyShape(BodyShape):
    """Render a component using a parametric SVG template.

    The template is loaded once, deep-copied per draw call, then patched:
    - lead endpoints adjusted for actual pin_offsets distance
    - body fill/stroke set from theme colours
    - pin circles coloured per net
    - viewBox updated to match actual geometry
    The result is passed to QSvgRenderer → QGraphicsSvgItem with a
    QTransform that places and rotates it in world space.
    """

    def __init__(self, template_path: Path) -> None:
        self._path = template_path
        # Parse once; draw() works on a deep copy each time.
        self._template: ET.ElementTree = ET.parse(template_path)

    # ------------------------------------------------------------------
    # BodyShape interface
    # ------------------------------------------------------------------

    def draw(
        self,
        positions: list[Point],
        fill: QColor,
        border: QColor,
        alpha: float,
        z: float,
        theme: Theme = DEFAULT_THEME,
    ) -> list[QGraphicsItem]:
        import copy
        tree = copy.deepcopy(self._template)
        root = tree.getroot()

        if len(positions) < 2:
            return GenericBody().draw(positions, fill, border, alpha, z, theme)

        p1 = positions[0]
        p2 = positions[1]
        dist = math.hypot(p2.x - p1.x, p2.y - p1.y)
        angle_deg = math.degrees(math.atan2(p2.y - p1.y, p2.x - p1.x))

        self._patch_leads(root, dist, theme)
        self._patch_body_colour(root, fill, border, alpha)
        self._patch_pins(root, positions, fill, alpha)
        self._patch_viewbox(root, dist)

        xml_bytes = ET.tostring(root, encoding="unicode",
                                xml_declaration=False).encode("utf-8")

        renderer = QSvgRenderer(QByteArray(xml_bytes))
        item = QGraphicsSvgItem()
        item.setSharedRenderer(renderer)
        item.setZValue(z)

        # Position: translate to pin-1 world, rotate toward pin-2.
        t = QTransform()
        t.translate(p1.x, p1.y)
        t.rotate(angle_deg)
        item.setTransform(t)

        return [item]

    # ------------------------------------------------------------------
    # Patching helpers
    # ------------------------------------------------------------------

    def _patch_leads(self, root: ET.Element, dist: float, theme: Theme) -> None:
        """Adjust lead-1 and lead-2 endpoints to match actual pin distance."""
        body_len = dist * theme.axial_body_length_ratio
        attach1 = (dist - body_len) / 2.0   # y2 of lead-1 (pin-1 side)
        attach2 = dist - attach1              # y1 of lead-2 (pin-2 side)

        lead1 = _find_id(root, "lead-1")
        if lead1 is not None:
            lead1.set("y2", f"{attach1:.4f}")

        lead2 = _find_id(root, "lead-2")
        if lead2 is not None:
            lead2.set("y1", f"{attach2:.4f}")
            lead2.set("y2", f"{dist:.4f}")

        # Update body position to stay centred
        body = _find_id(root, "body")
        if body is not None and body.tag in (_svg_tag("rect"), "rect"):
            body_h = dist - 2 * (dist - body_len) / 2.0
            body.set("y", f"{attach1:.4f}")
            body.set("height", f"{body_h:.4f}")

        # Update pin-2 circle position
        pin2 = _find_id(root, "pin-2")
        if pin2 is not None:
            pin2.set("cy", f"{dist:.4f}")

    def _patch_body_colour(
        self, root: ET.Element, fill: QColor, border: QColor, alpha: float
    ) -> None:
        fill_hex = fill.name()          # "#rrggbb"
        fill_a = fill.alphaF() * alpha
        border_hex = border.name()
        border_a = border.alphaF() * alpha

        body = _find_id(root, "body")
        if body is not None:
            body.set("fill", fill_hex)
            body.set("fill-opacity", f"{fill_a:.3f}")
            body.set("stroke", border_hex)
            body.set("stroke-opacity", f"{border_a:.3f}")

        # Leads share the lead_color from theme, but respect alpha
        lead_hex = QColor(DEFAULT_THEME.lead_color).name()
        for lead_id in ("lead-1", "lead-2", "lead-3"):
            lead = _find_id(root, lead_id)
            if lead is not None:
                lead.set("stroke", lead_hex)
                lead.set("stroke-opacity", f"{alpha:.3f}")

    def _patch_pins(
        self,
        root: ET.Element,
        positions: list[Point],
        fallback: QColor,
        alpha: float,
    ) -> None:
        """Set pin circle colours; pin1-marker gets the same colour as pin-1."""
        fallback_hex = fallback.name()
        for i in range(len(positions)):
            pin_id = f"pin-{i + 1}"
            el = _find_id(root, pin_id)
            if el is not None:
                el.set("fill", fallback_hex)
                el.set("fill-opacity", f"{alpha:.3f}")
        marker = _find_id(root, "pin1-marker")
        if marker is not None:
            marker.set("fill", fallback_hex)
            marker.set("fill-opacity", f"{alpha:.3f}")

    def patch_pin_net_color(
        self, root: ET.Element, pin_number: int, color: QColor, alpha: float
    ) -> None:
        """Update a single pin's colour after net assignment is known."""
        el = _find_id(root, f"pin-{pin_number}")
        if el is not None:
            el.set("fill", color.name())
            el.set("fill-opacity", f"{alpha:.3f}")
        if pin_number == 1:
            marker = _find_id(root, "pin1-marker")
            if marker is not None:
                marker.set("fill", color.name())
                marker.set("fill-opacity", f"{alpha:.3f}")

    def _patch_viewbox(self, root: ET.Element, dist: float) -> None:
        """Expand viewBox to cover actual pin distance."""
        margin = 0.5
        root.set("viewBox", f"{-margin} {-margin} {1.0} {dist + 2 * margin}")
        root.set("width",  f"{1.0}")
        root.set("height", f"{dist + 2 * margin:.4f}")


# ---------------------------------------------------------------------------
# SVG-based DIP renderer (generates SVG programmatically for any N)
# ---------------------------------------------------------------------------

class SvgDIPBody(BodyShape):
    """Generate DIP SVG on the fly for any n_per_row × rows_spacing."""

    def __init__(self, n_per_row: int, rows_spacing: float = 3.0) -> None:
        self._n = n_per_row
        self._rs = rows_spacing

    def draw(
        self,
        positions: list[Point],
        fill: QColor,
        border: QColor,
        alpha: float,
        z: float,
        theme: Theme = DEFAULT_THEME,
    ) -> list[QGraphicsItem]:
        n = self._n
        rs = self._rs
        if len(positions) < 2 * n:
            return GenericBody().draw(positions, fill, border, alpha, z, theme)

        fill_hex = fill.name()
        fill_a = f"{fill.alphaF() * alpha:.3f}"
        border_hex = border.name()
        border_a = f"{border.alphaF() * alpha:.3f}"
        pin_hex = fill.name()  # placeholder; caller patches per-net

        margin = 0.5
        body_h = (n - 1) + 2 * margin
        notch_r = theme.dip_notch_radius
        r = theme.body_corner_radius
        mid_x = rs / 2.0

        # Build SVG string directly
        pin_circles = []
        # Left column: pins 1..n at x=0, y=0..(n-1)
        for i in range(n):
            pin_circles.append(
                f'<circle id="pin-{i+1}" cx="0" cy="{i}" r="0.14"'
                f' fill="{pin_hex}" fill-opacity="{alpha:.3f}" stroke="none"/>'
            )
        # Right column: pins n+1..2n at x=rs, y=(n-1)..0
        for i in range(n):
            pid = n + i + 1
            pin_circles.append(
                f'<circle id="pin-{pid}" cx="{rs}" cy="{n-1-i}" r="0.14"'
                f' fill="{pin_hex}" fill-opacity="{alpha:.3f}" stroke="none"/>'
            )

        pin1_marker = (
            f'<rect id="pin1-marker" x="-0.12" y="-0.12" width="0.24" height="0.24"'
            f' fill="{pin_hex}" fill-opacity="{alpha:.3f}" stroke="none"/>'
        )

        svg = f"""<svg xmlns="http://www.w3.org/2000/svg"
     viewBox="{-margin} {-margin} {rs + 2*margin:.3f} {body_h:.3f}"
     width="{rs + 2*margin:.3f}" height="{body_h:.3f}"
     data-pin-count="{2*n}" data-n-per-row="{n}" data-rows-spacing="{rs}">
  <path id="body" fill-rule="evenodd"
        d="M {r},{-margin}
           L {rs-r},{-margin} Q {rs},{-margin} {rs},{-margin+r}
           L {rs},{n-1+margin-r} Q {rs},{n-1+margin} {rs-r},{n-1+margin}
           L {r},{n-1+margin} Q {0},{n-1+margin} {0},{n-1+margin-r}
           L {0},{-margin+r} Q {0},{-margin} {r},{-margin} Z
           M {mid_x},{-margin}
           A {notch_r},{notch_r} 0 1,0 {mid_x},{-margin+0.001} Z"
        fill="{fill_hex}" fill-opacity="{fill_a}"
        stroke="{border_hex}" stroke-opacity="{border_a}" stroke-width="0.05"/>
  {pin1_marker}
  {"".join(pin_circles)}
</svg>"""

        xml_bytes = svg.encode("utf-8")
        renderer = QSvgRenderer(QByteArray(xml_bytes))
        item = QGraphicsSvgItem()
        item.setSharedRenderer(renderer)
        item.setZValue(z)

        # DIP: pin 1 at positions[0], no rotation needed (layout already world).
        # The SVG local frame matches DualRow pin_positions() exactly.
        p1 = positions[0]
        t = QTransform()
        t.translate(p1.x, p1.y)
        item.setTransform(t)
        return [item]


# ---------------------------------------------------------------------------
# SVG-based SIL renderer (generates SVG programmatically for any N)
# ---------------------------------------------------------------------------

class SvgSILBody(BodyShape):
    """Generate SIL header SVG on the fly for any pin count."""

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

        n = len(positions)
        pitch = 1.0  # SingleRow default; close enough for all SIL headers
        # Determine actual pitch from positions if possible
        if n >= 2:
            pitch = math.hypot(
                positions[1].x - positions[0].x,
                positions[1].y - positions[0].y,
            )
            if pitch < 1e-6:
                pitch = 1.0

        fill_hex = fill.name()
        fill_a = f"{fill.alphaF() * alpha:.3f}"
        border_hex = border.name()
        border_a = f"{border.alphaF() * alpha:.3f}"
        pin_hex = fill.name()
        slot = theme.sil_slot_size
        m = slot / 2 + 0.05
        body_h = (n - 1) * pitch + 2 * m
        r = theme.body_corner_radius

        slots = []
        pin_circles = []
        for i in range(n):
            y = i * pitch
            hs = slot / 2
            slots.append(
                f'<rect id="slot-{i+1}" x="{-hs:.3f}" y="{y-hs:.3f}"'
                f' width="{slot:.3f}" height="{slot:.3f}"'
                f' fill="{fill_hex}" fill-opacity="0.5"'
                f' stroke="{border_hex}" stroke-width="0.03"/>'
            )
            pin_circles.append(
                f'<circle id="pin-{i+1}" cx="0" cy="{y:.3f}" r="0.14"'
                f' fill="{pin_hex}" fill-opacity="{alpha:.3f}" stroke="none"/>'
            )

        pin1_marker = (
            f'<rect id="pin1-marker" x="-0.12" y="-0.12" width="0.24" height="0.24"'
            f' fill="{pin_hex}" fill-opacity="{alpha:.3f}" stroke="none"/>'
        )

        svg = f"""<svg xmlns="http://www.w3.org/2000/svg"
     viewBox="{-m:.3f} {-m:.3f} {2*m:.3f} {body_h:.3f}"
     data-pin-count="{n}" data-pitch="{pitch:.3f}">
  <rect id="housing"
        x="{-m:.3f}" y="{-m:.3f}" width="{2*m:.3f}" height="{body_h:.3f}"
        rx="{r}" ry="{r}"
        fill="{fill_hex}" fill-opacity="{fill_a}"
        stroke="{border_hex}" stroke-opacity="{border_a}" stroke-width="0.05"/>
  {"".join(slots)}
  {pin1_marker}
  {"".join(pin_circles)}
</svg>"""

        xml_bytes = svg.encode("utf-8")
        renderer = QSvgRenderer(QByteArray(xml_bytes))
        item = QGraphicsSvgItem()
        item.setSharedRenderer(renderer)
        item.setZValue(z)

        p1 = positions[0]
        # Rotate to align with pin row direction
        if n >= 2:
            angle_deg = math.degrees(
                math.atan2(
                    positions[1].y - p1.y,
                    positions[1].x - p1.x,
                )
            )
        else:
            angle_deg = 0.0
        t = QTransform()
        t.translate(p1.x, p1.y)
        t.rotate(angle_deg)
        item.setTransform(t)
        return [item]


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
        path.setFillRule(Qt.FillRule.OddEvenFill)
        path.addRoundedRect(QRectF(lx, ty, rx - lx, by - ty), r, r)
        # Subtract notch via even-odd fill rule
        path.addEllipse(QRectF(
            mid_x - notch_r, ty - notch_r,
            2 * notch_r, 2 * notch_r
        ))

        body = QGraphicsPathItem(path)
        body.setBrush(QBrush(fill))
        body.setPen(bp)
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


def _svg_shape(name: str) -> BodyShape | None:
    """Load SvgBodyShape from assets, return None if file missing."""
    path = _ASSETS / f"{name}.svg"
    if path.exists():
        return SvgBodyShape(path)
    return None


def body_shape_for(defn: ComponentDefinition) -> BodyShape:
    """Return the appropriate BodyShape for *defn*.

    Prefers SVG-based renderers (parametric, editable in Inkscape).
    Falls back to QPainterPath renderers when no SVG template is found.
    """
    fp = defn.footprint
    cls = defn.component_class

    if isinstance(fp, SingleRow):
        if fp.n == 2:
            if cls in _AXIAL_CLASSES:
                return _svg_shape("axial") or AxialBody()
            if cls in _DIODE_CLASSES:
                return _svg_shape("diode") or DiodeBody()
            if cls in _RADIAL_CLASSES:
                return _svg_shape("radial") or RadialBody()
        if fp.n == 3 and cls in _TRANSISTOR_CLASSES:
            return _svg_shape("to92") or TO92Body()
        # Generic single-row: connector / switch / other
        return SvgSILBody()

    if isinstance(fp, DualRow):
        return SvgDIPBody(fp.n_per_row, fp.rows_spacing)

    return GenericBody()
