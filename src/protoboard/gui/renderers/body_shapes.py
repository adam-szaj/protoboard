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

from protoboard.domain.components.body_type import BodyTypeSpec, BodyVariant
from protoboard.domain.components.definition import (
    ComponentClass,
    ComponentDefinition,
)
from protoboard.domain.components.footprints import (
    DualRow,
    QuadRow,
    SingleRow,
    TO220Footprint,
)
from protoboard.domain.components.instance import ComponentInstance
from protoboard.domain.layout.placement import ComponentPlacement
from protoboard.geometry import Point
from protoboard.gui.theme import DEFAULT_THEME, Theme

# ---------------------------------------------------------------------------
# Assets directory
# ---------------------------------------------------------------------------

from protoboard.paths import BUILTIN_ASSETS_DIR as _BUILTIN_ASSETS_DIR

_ASSETS: Path = _BUILTIN_ASSETS_DIR / "components"

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
# SVG namespace helper
# ---------------------------------------------------------------------------

_SVG_NS = "http://www.w3.org/2000/svg"
ET.register_namespace("", _SVG_NS)


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

    SVG units are millimetres.  The template encodes only the physical body
    (no leads, no pin markers).  ``data-body-width-mm`` and
    ``data-body-height-mm`` attributes on the root ``<svg>`` element carry the
    canvas dimensions so the renderer can convert mm → raster units.

    Leads are drawn as ``QGraphicsLineItem`` objects in code; their length
    adjusts to the actual pin spacing so the body size stays physically constant
    regardless of how far apart the pins are placed.
    """

    def __init__(self, template_path: Path) -> None:
        self._path = template_path
        self._template: ET.ElementTree = ET.parse(template_path)
        root = self._template.getroot()
        self._body_w_mm = float(root.get("data-body-width-mm", "4"))
        self._body_h_mm = float(root.get("data-body-height-mm", "10"))

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

        if len(positions) < 2:
            return GenericBody().draw(positions, fill, border, alpha, z, theme)

        p1 = positions[0]
        p2 = positions[1]
        dx = p2.x - p1.x
        dy = p2.y - p1.y
        dist = math.hypot(dx, dy)
        if dist < 1e-9:
            return GenericBody().draw(positions, fill, border, alpha, z, theme)

        mm = theme.raster_pitch_mm
        body_len_r = self._body_h_mm / mm   # body height in raster units
        half       = body_len_r / 2.0

        mid_x = (p1.x + p2.x) / 2.0
        mid_y = (p1.y + p2.y) / 2.0
        # SVG long axis is Y (height).  To align it with pin1→pin2 we need the
        # Y axis to point in the (dx, dy) direction, which requires rotating
        # the X axis there first (+90° extra vs the standard atan2 formula).
        angle_deg = math.degrees(math.atan2(dy, dx)) + 90.0

        items: list[QGraphicsItem] = []

        # ------------------------------------------------------------------
        # Leads — only when pins are farther apart than the body length
        # ------------------------------------------------------------------
        if dist > body_len_r + 1e-6:
            unit_x = dx / dist
            unit_y = dy / dist
            attach = dist / 2.0 - half   # lead length on each side

            lead_pen = _lead_pen(theme, alpha)
            l1 = QGraphicsLineItem(
                p1.x, p1.y,
                p1.x + unit_x * attach, p1.y + unit_y * attach,
            )
            l2 = QGraphicsLineItem(
                p2.x - unit_x * attach, p2.y - unit_y * attach,
                p2.x, p2.y,
            )
            for ln in (l1, l2):
                ln.setPen(lead_pen)
                ln.setZValue(z - 0.01)
            items.extend([l1, l2])

        # ------------------------------------------------------------------
        # SVG body — fixed physical size, placed at midpoint
        # ------------------------------------------------------------------
        tree = copy.deepcopy(self._template)
        root = tree.getroot()
        self._patch_body_colour(root, fill, border, alpha)

        xml_bytes = ET.tostring(root, encoding="unicode",
                                xml_declaration=False).encode("utf-8")

        item = QGraphicsSvgItem()
        renderer = QSvgRenderer(QByteArray(xml_bytes), item)
        item.setSharedRenderer(renderer)
        item._svg_renderer = renderer
        item.setZValue(z)

        # SVG is in mm; scene is in raster units.
        # scale = raster / mm so 1 mm → 1/mm raster.
        # SVG canvas centre is at (body_w_mm/2, body_h_mm/2).
        # We want that point at world (mid_x, mid_y) after rotation.
        #
        # Transform chain (Qt applies right-to-left):
        #   translate(mid)                   — place centre at midpoint
        #   rotate(angle_deg)                — align body with pin axis
        #   scale(1/mm, 1/mm)                — mm → raster units
        #   translate(-w_mm/2, -h_mm/2)      — SVG origin to canvas centre
        s = 1.0 / mm
        t = QTransform()
        t.translate(mid_x, mid_y)
        t.rotate(angle_deg)
        t.scale(s, s)
        t.translate(-self._body_w_mm / 2.0, -self._body_h_mm / 2.0)
        item.setTransform(t)

        items.append(item)
        return items

    # ------------------------------------------------------------------
    # Patching helpers
    # ------------------------------------------------------------------

    def _patch_body_colour(
        self, root: ET.Element, fill: QColor, border: QColor, alpha: float
    ) -> None:
        fill_hex  = fill.name()
        fill_a    = fill.alphaF() * alpha
        border_hex = border.name()
        border_a  = border.alphaF() * alpha

        for elem_id in ("body",):
            el = _find_id(root, elem_id)
            if el is not None:
                el.set("fill",         fill_hex)
                el.set("fill-opacity", f"{fill_a:.3f}")
                el.set("stroke",       border_hex)
                el.set("stroke-opacity", f"{border_a:.3f}")


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

        fill = QColor(0x22, 0x22, 0x44)
        fill.setAlphaF(alpha*_big_body_alpha)
        fill_hex = fill.name()
        fill_a = f"{fill.alphaF() * alpha:.3f}"
        border_hex = border.name()
        border_a = f"{border.alphaF() * alpha:.3f}"
        pin_hex = "#606060" #fill.name()  # placeholder; caller patches per-net

        margin = 0.5
        body_h = (n - 1) + 2 * margin
        notch_r = theme.dip_notch_radius
        r = theme.body_corner_radius
        mid_x = rs / 2.0

        # Build SVG string directly
        pin_circles = []
        # Left column: pins 1..n at x=0, y=0..(n-1)
        for i in range(1,n):
            pin_circles.append(
                f'<circle id="pin-{i+1}" cx="0" cy="{i}" r="0.14"'
                f' fill="{pin_hex}" fill-opacity="{alpha:.3f}" stroke="{border_hex}" stroke-width="0.05"/>'
            )
        # Right column: pins n+1..2n at x=rs, y=(n-1)..0
        for i in range(n):
            pid = n + i + 1
            pin_circles.append(
                f'<circle id="pin-{pid}" cx="{rs}" cy="{n-1-i}" r="0.14"'
                f' fill="{pin_hex}" fill-opacity="{alpha:.3f}" stroke="{border_hex}" stroke-width="0.05"/>'
            )

        pin1_marker = (
            f'<rect id="pin1-marker" x="-0.12" y="-0.12" width="0.24" height="0.24"'
            f' fill="{pin_hex}" fill-opacity="{alpha:.3f}" stroke="{border_hex}" stroke-width="0.05"/>'
        )

        # Notch: semicircle cut into the top edge at pin-1 side.
        # Two-arc technique: start at left edge of notch, arc through top,
        # end at right edge — both points on the body top edge (y=-margin).
        nx0 = mid_x - notch_r   # left notch point
        nx1 = mid_x + notch_r   # right notch point
        svg = f"""<svg xmlns="http://www.w3.org/2000/svg"
     viewBox="{-margin} {-margin} {rs + 2*margin:.3f} {body_h:.3f}"
     width="{rs + 2*margin:.3f}" height="{body_h:.3f}"
     data-pin-count="{2*n}" data-n-per-row="{n}" data-rows-spacing="{rs}">
  <path id="body" fill-rule="evenodd"
        d="M {r},{-margin}
           L {nx0},{-margin}
           A {notch_r},{notch_r} 0 0,0 {nx1},{-margin}
           L {rs-r},{-margin} Q {rs},{-margin} {rs},{-margin+r}
           L {rs},{n-1+margin-r} Q {rs},{n-1+margin} {rs-r},{n-1+margin}
           L {r},{n-1+margin} Q {0},{n-1+margin} {0},{n-1+margin-r}
           L {0},{-margin+r} Q {0},{-margin} {r},{-margin} Z"
        fill="{fill_hex}" fill-opacity="{fill_a}"
        stroke="{border_hex}" stroke-opacity="{border_a}" stroke-width="0.05"/>
  {pin1_marker}
  {"".join(pin_circles)}
</svg>"""

        xml_bytes = svg.encode("utf-8")
        item = QGraphicsSvgItem()
        renderer = QSvgRenderer(QByteArray(xml_bytes), item)
        item.setSharedRenderer(renderer)
        item._svg_renderer = renderer
        item.setZValue(z)

        # Scale: QSvgRenderer rounds viewBox to integer pixels, so we must
        # compensate to keep 1 raster unit == 1 scene unit.
        vb = renderer.viewBoxF()
        br = item.boundingRect()
        sx = vb.width()  / br.width()  if br.width()  > 1e-9 else 1.0
        sy = vb.height() / br.height() if br.height() > 1e-9 else 1.0

        # pin-1 in item-pixel space is at (margin, margin); scale to raster.
        pin1_item_x = margin / sx
        pin1_item_y = margin / sy

        # Derive rotation from the direction pin1→pin2 (first column).
        p1 = positions[0]
        if len(positions) >= 2:
            dx = positions[1].x - p1.x
            dy = positions[1].y - p1.y
            angle_deg = -math.degrees(math.atan2(dx, dy))
        else:
            angle_deg = 0.0

        t = QTransform()
        t.translate(p1.x, p1.y)
        t.rotate(angle_deg)
        t.scale(sx, sy)
        t.translate(-pin1_item_x, -pin1_item_y)
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
            f'<rect id="pin1-marker" x="-0.08" y="-0.08" width="0.16" height="0.16"'
            f' fill="{pin_hex}" fill-opacity="{alpha:.3f}" stroke="none"/>'
        )

        svg = f"""<svg xmlns="http://www.w3.org/2000/svg"
     viewBox="{-m:.3f} {-m:.3f} {2*m:.3f} {body_h:.3f}"
     width="{2*m:.3f}" height="{body_h:.3f}"
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
        item = QGraphicsSvgItem()
        renderer = QSvgRenderer(QByteArray(xml_bytes), item)
        item.setSharedRenderer(renderer)
        item._svg_renderer = renderer
        item.setZValue(z)

        # QSvgRenderer.defaultSize() rounds viewBox dimensions to integer pixels,
        # so QGraphicsSvgItem.boundingRect() is in pixels, not raster units.
        # We must scale the item so that 1 item-pixel == 1 raster unit.
        # viewBoxF gives the true SVG dimensions; boundingRect gives the pixel size.
        vb = renderer.viewBoxF()
        br = item.boundingRect()
        sx = vb.width()  / br.width()  if br.width()  > 1e-9 else 1.0
        sy = vb.height() / br.height() if br.height() > 1e-9 else 1.0
        # pin1 in item-space: (m, m) in pixels, which maps to (m*sx, m*sy) in raster
        pin1_item_x = m / sx
        pin1_item_y = m / sy

        p1 = positions[0]
        if n >= 2:
            dx = positions[1].x - p1.x
            dy = positions[1].y - p1.y
            angle_deg = -math.degrees(math.atan2(dx, dy))
        else:
            angle_deg = 0.0
        t = QTransform()
        t.translate(p1.x, p1.y)
        t.rotate(angle_deg)
        t.scale(sx, sy)
        t.translate(-pin1_item_x, -pin1_item_y)
        item.setTransform(t)
        return [item]


# ---------------------------------------------------------------------------
# SVG-based Quad-DIP renderer (4 columns: L_outer, L_inner, R_inner, R_outer)
# ---------------------------------------------------------------------------


class SvgQuadDIPBody(BodyShape):
    """Renderer for QuadRow footprints (QIL / quad in-line modules).

    Draws:
    - 4 columns of pad circles, positions taken directly from pin_positions()
    - A filled rectangle covering the main PCB body (between inner columns)
      with rounded corners and a notch at the top (pin-1 side)
    - Two thin strip rectangles flanking the body (outer goldpin rows)
    - Pin-1 corner marker
    """

    def __init__(
        self,
        n_per_col: int,
        col_spacings: tuple[float, float, float],
        pitch: float = 1.0,
    ) -> None:
        self._n = n_per_col
        self._s0, self._s1, self._s2 = col_spacings
        self._pitch = pitch

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
        s0, s1, s2 = self._s0, self._s1, self._s2
        pitch = self._pitch

        if len(positions) < 4 * n:
            return GenericBody().draw(positions, fill, border, alpha, z, theme)

        fill = QColor(0x22, 0x22, 0x44, 0xe0)
        fill.setAlphaF(alpha*_big_body_alpha)
        fill_hex = fill.name()
        fill_a = f"{fill.alphaF() * alpha:.3f}"
        border_hex = border.name()
        border_a = f"{border.alphaF() * alpha:.3f}"
        pin_hex = "#606060"

        # Column x-positions (in raster units)
        x0 = 0.0
        x1 = s0
        x2 = s0 + s1
        x3 = s0 + s1 + s2

        body_margin = 0.5
        y_top = -body_margin
        y_bot = (n - 1) * pitch + body_margin
        body_h = y_bot - y_top
        total_w = x3

        # Body covers the inner columns (x1..x2) — main PCB chip area
        r = theme.body_corner_radius
        notch_r = theme.dip_notch_radius
        mid_x = (x1 + x2) / 2.0
        nx0 = mid_x - notch_r
        nx1 = mid_x + notch_r

        # Outer strip width: fits snugly around the goldpin rows
        strip_w = 0.3

        # --- Build pin circles ---
        circles: list[str] = []

        # L_outer (pins 1..n): col x0, y top→bottom
        for i in range(n):
            pid = i + 1
            cx = f"{i}" if i > 0 else "0"
            circles.append(
                f'<circle id="pin-{pid}" cx="{x0}" cy="{i * pitch}" r="0.14"'
                f' fill="{pin_hex}" fill-opacity="{alpha:.3f}"'
                f' stroke="{border_hex}" stroke-width="0.05"/>'
            )

        # L_inner (pins n+1..2n): col x1, y top→bottom
        for i in range(n):
            pid = n + i + 1
            circles.append(
                f'<circle id="pin-{pid}" cx="{x1}" cy="{i * pitch}" r="0.14"'
                f' fill="{pin_hex}" fill-opacity="{alpha:.3f}"'
                f' stroke="{border_hex}" stroke-width="0.05"/>'
            )

        # R_inner (pins 2n+1..3n): col x2, y bottom→top
        for i in range(n):
            pid = 2 * n + i + 1
            circles.append(
                f'<circle id="pin-{pid}" cx="{x2}" cy="{(n - 1 - i) * pitch}" r="0.14"'
                f' fill="{pin_hex}" fill-opacity="{alpha:.3f}"'
                f' stroke="{border_hex}" stroke-width="0.05"/>'
            )

        # R_outer (pins 3n+1..4n): col x3, y bottom→top
        for i in range(n):
            pid = 3 * n + i + 1
            circles.append(
                f'<circle id="pin-{pid}" cx="{x3}" cy="{(n - 1 - i) * pitch}" r="0.14"'
                f' fill="{pin_hex}" fill-opacity="{alpha:.3f}"'
                f' stroke="{border_hex}" stroke-width="0.05"/>'
            )

        pin1_marker = (
            f'<rect id="pin1-marker" x="{x0 - 0.12}" y="-0.12" width="0.24" height="0.24"'
            f' fill="{pin_hex}" fill-opacity="{alpha:.3f}"'
            f' stroke="{border_hex}" stroke-width="0.05"/>'
        )

        svg = f"""<svg xmlns="http://www.w3.org/2000/svg"
     viewBox="{x0 - body_margin} {y_top} {total_w + 2 * body_margin:.3f} {body_h:.3f}"
     width="{total_w + 2 * body_margin:.3f}" height="{body_h:.3f}">
  <!-- Left outer goldpin strip -->
  <rect x="{x0 - strip_w / 2:.3f}" y="{y_top:.3f}"
        width="{strip_w:.3f}" height="{body_h:.3f}" rx="0.15"
        fill="{fill_hex}" fill-opacity="{fill.alphaF() * alpha * 0.6:.3f}"
        stroke="{border_hex}" stroke-opacity="{border_a}" stroke-width="0.04"/>
  <!-- Right outer goldpin strip -->
  <rect x="{x3 - strip_w / 2:.3f}" y="{y_top:.3f}"
        width="{strip_w:.3f}" height="{body_h:.3f}" rx="0.15"
        fill="{fill_hex}" fill-opacity="{fill.alphaF() * alpha * 0.6:.3f}"
        stroke="{border_hex}" stroke-opacity="{border_a}" stroke-width="0.04"/>
  <!-- Main PCB body (between inner columns, with notch) -->
  <path id="body" fill-rule="evenodd"
        d="M {x1 + r},{y_top}
           L {nx0},{y_top}
           A {notch_r},{notch_r} 0 0,0 {nx1},{y_top}
           L {x2 - r},{y_top} Q {x2},{y_top} {x2},{y_top + r}
           L {x2},{y_bot - r} Q {x2},{y_bot} {x2 - r},{y_bot}
           L {x1 + r},{y_bot} Q {x1},{y_bot} {x1},{y_bot - r}
           L {x1},{y_top + r} Q {x1},{y_top} {x1 + r},{y_top} Z"
        fill="{fill_hex}" fill-opacity="{fill_a}"
        stroke="{border_hex}" stroke-opacity="{border_a}" stroke-width="0.05"/>
  {pin1_marker}
  {"".join(circles)}
</svg>"""

        xml_bytes = svg.encode("utf-8")
        item = QGraphicsSvgItem()
        renderer = QSvgRenderer(QByteArray(xml_bytes), item)
        item.setSharedRenderer(renderer)
        item._svg_renderer = renderer  # type: ignore[attr-defined]
        item.setZValue(z)

        vb = renderer.viewBoxF()
        br = item.boundingRect()
        sx = vb.width()  / br.width()  if br.width()  > 1e-9 else 1.0
        sy = vb.height() / br.height() if br.height() > 1e-9 else 1.0

        # pin-1 in item-pixel space
        pin1_item_x = (x0 - (x0 - body_margin)) / sx  # = body_margin / sx
        pin1_item_y = body_margin / sy

        # Rotation from pin1→pin2 direction (both in L_outer column, y increases)
        p1 = positions[0]
        if len(positions) >= 2:
            dx = positions[1].x - p1.x
            dy = positions[1].y - p1.y
            angle_deg = -math.degrees(math.atan2(dx, dy))
        else:
            angle_deg = 0.0

        t = QTransform()
        t.translate(p1.x, p1.y)
        t.rotate(angle_deg)
        t.scale(sx, sy)
        t.translate(-pin1_item_x, -pin1_item_y)
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
        body_len = dist * 0.50   # fallback: 50% of pin distance
        body_w   = 0.40
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
        body_len = dist * 0.50
        body_w   = 0.40 * 0.85
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

        fill = QColor(0x33, 0x33, 0x66)
        fill.setAlphaF(alpha*_big_body_alpha)
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
        slot_fill.setAlphaF(slot_fill.alphaF() * _big_body_alpha)
        slot_border = QColor(border)
        slot_border.setAlphaF(alpha * theme.body_slot_opacity)
        slot_pen = QPen(slot_border, theme.body_slot_pen_width)
        hs = slot / 2
        for p in positions:
            sq = QGraphicsRectItem(QRectF(p.x - hs, p.y - hs, slot, slot))
            sq.setBrush(QBrush(slot_fill))
            sq.setPen(slot_pen)
            sq.setZValue(z + 0.01)
            items.append(sq)

        return items


# ---------------------------------------------------------------------------
# CylinderBody — axial cylindrical component (resistor, diode, inductor, cap)
#
# Renders either horizontally (body between leads) or vertically (cylinder
# seen from above, one lead from centre to second pad).
# ---------------------------------------------------------------------------

class CylinderBody(BodyShape):
    """Render a cylindrical axial component with fixed physical dimensions.

    Horizontal mode (dist >= threshold):
      anchor_pin ─── lead ─── [body] ─── lead ─── other_pin

    Vertical mode (dist < threshold):
      Cylinder viewed from above: filled circle on the anchor_pin pad,
      thin lead from circle centre to the other pad.
    """

    def __init__(self, variant: BodyVariant, polar: bool, vertical_flipped: bool = False) -> None:
        self._variant = variant
        self._polar = polar
        self._vertical_flipped = vertical_flipped

    def drawn_pin_numbers(
        self,
        positions: list[Point],
        theme: Theme = DEFAULT_THEME,
    ) -> frozenset[int]:
        # In vertical mode only the cylinder-pad pin is drawn by this shape;
        # the lead-end pin is left for component_renderer so it gets net colour.
        if len(positions) >= 2:
            mm = theme.raster_pitch_mm
            dist = math.hypot(positions[1].x - positions[0].x,
                              positions[1].y - positions[0].y)
            threshold = (self._variant.length_mm + 1.0) / mm
            if dist < threshold:
                cylinder_pin = 2 if self._vertical_flipped else 1
                return frozenset({cylinder_pin})
        return frozenset()

    def draw(
        self,
        positions: list[Point],
        fill: QColor,
        border: QColor,
        alpha: float,
        z: float,
        theme: Theme = DEFAULT_THEME,
    ) -> list[QGraphicsItem]:
        if len(positions) < 2:
            return GenericBody().draw(positions, fill, border, alpha, z, theme)

        if self._vertical_flipped:
            p1, p2 = positions[1], positions[0]  # cylinder over pin2
        else:
            p1, p2 = positions[0], positions[1]  # cylinder over pin1 (default)
        dx = p2.x - p1.x
        dy = p2.y - p1.y
        dist = math.hypot(dx, dy)
        if dist < 1e-9:
            return GenericBody().draw(positions, fill, border, alpha, z, theme)

        mm      = theme.raster_pitch_mm
        v       = self._variant
        len_r   = v.length_mm   / mm   # body length in raster units
        diam_r  = v.diameter_mm / mm   # body diameter in raster units

        threshold = (v.length_mm + 1.0) / mm   # 0.5 mm margin each side
        if dist < threshold:
            return self._draw_vertical(p1, p2, diam_r, fill, border, alpha, z, theme)
        else:
            return self._draw_horizontal(
                p1, p2, dx, dy, dist, len_r, diam_r, fill, border, alpha, z, theme
            )

    # ------------------------------------------------------------------
    # Horizontal rendering
    # ------------------------------------------------------------------

    def _draw_horizontal(
        self,
        p1: Point, p2: Point,
        dx: float, dy: float, dist: float,
        len_r: float, diam_r: float,
        fill: QColor, border: QColor, alpha: float, z: float,
        theme: Theme,
    ) -> list[QGraphicsItem]:
        half = len_r / 2.0
        mid_x = (p1.x + p2.x) / 2.0
        mid_y = (p1.y + p2.y) / 2.0
        angle_deg = math.degrees(math.atan2(dy, dx))

        items: list[QGraphicsItem] = []
        lead_pen = _lead_pen(theme, alpha)
        bp = _body_pen(border, alpha)

        # Leads — only when pins are farther apart than the body length
        if dist > len_r + 1e-6:
            unit_x = dx / dist
            unit_y = dy / dist
            attach = dist / 2.0 - half
            l1 = QGraphicsLineItem(
                p1.x, p1.y,
                p1.x + unit_x * attach, p1.y + unit_y * attach,
            )
            l2 = QGraphicsLineItem(
                p2.x - unit_x * attach, p2.y - unit_y * attach,
                p2.x, p2.y,
            )
            for ln in (l1, l2):
                ln.setPen(lead_pen)
                ln.setZValue(z - 0.01)
            items.extend([l1, l2])

        # Body — rounded rectangle (cylinder cross-section)
        r = min(diam_r / 2.0, theme.body_corner_radius)
        body_path = QPainterPath()
        body_path.addRoundedRect(
            QRectF(-len_r / 2.0, -diam_r / 2.0, len_r, diam_r), r, r
        )

        # Polar marker: dark stripe at cathode end.
        # p1→p2 direction: p2 side is at +len_r/2 in local frame.
        # When vertical_flipped, p1↔p2 were swapped, so cathode (pin2) is now p1 → left side.
        stripe_x = -len_r / 2.0 if self._vertical_flipped else len_r / 2.0 - min(len_r * 0.12, diam_r * 0.4)
        if self._polar:
            stripe_w = min(len_r * 0.12, diam_r * 0.4)
            body_path.addRect(
                QRectF(stripe_x, -diam_r / 2.0, stripe_w, diam_r)
            )

        body = QGraphicsPathItem(body_path)
        fill_c = QColor(fill)
        fill_c.setAlphaF(fill_c.alphaF() * alpha)
        body.setBrush(QBrush(fill_c))
        body.setPen(bp)
        body.setZValue(z)

        if self._polar:
            # Paint cathode stripe dark
            stripe_w = min(len_r * 0.12, diam_r * 0.4)
            stripe_path = QPainterPath()
            stripe_path.addRect(
                QRectF(stripe_x, -diam_r / 2.0, stripe_w, diam_r)
            )
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

        t = QTransform()
        t.translate(mid_x, mid_y)
        t.rotate(angle_deg)
        body.setTransform(t)
        items.append(body)
        return items

    # ------------------------------------------------------------------
    # Vertical rendering — cylinder seen from above
    # ------------------------------------------------------------------

    def _draw_vertical(
        self,
        p1: Point, p2: Point,
        diam_r: float,
        fill: QColor, border: QColor, alpha: float, z: float,
        theme: Theme,
    ) -> list[QGraphicsItem]:
        # p1/p2 are already swapped by draw() when vertical_flipped=True, so
        # p1 is always the pad where the cylinder sits, p2 is the lead end.
        # upper_pin_number tells which real pin number is at the lead end (top).
        bottom_pin_pos = p1
        top_pin_pos    = p2
        # When not flipped: cylinder over pin1 (anoda), lead to pin2 (katoda) → katoda on top
        # When flipped:     cylinder over pin2 (katoda), lead to pin1 (anoda) → anoda on top
        upper_pin_number = 1 if self._vertical_flipped else 2

        items: list[QGraphicsItem] = []
        lead_pen = _lead_pen(theme, alpha)

        # Thin lead from circle centre to top-pin pad
        lead = QGraphicsLineItem(
            bottom_pin_pos.x, bottom_pin_pos.y,
            top_pin_pos.x, top_pin_pos.y,
        )
        lead.setPen(lead_pen)
        lead.setZValue(z - 0.01)
        items.append(lead)

        # Circle on bottom-pin pad
        r = diam_r / 2.0
        circle = QGraphicsEllipseItem(QRectF(
            bottom_pin_pos.x - r, bottom_pin_pos.y - r, diam_r, diam_r
        ))
        circle.setZValue(z)

        if not self._polar:
            # Non-polar: plain filled circle with thin border
            fill_c = QColor(fill)
            fill_c.setAlphaF(fill_c.alphaF() * alpha)
            circle.setBrush(QBrush(fill_c))
            circle.setPen(_body_pen(border, alpha))
        elif upper_pin_number == 2:
            # Cathode/(-) visible from above → solid dark circle
            dark = QColor(theme.body_diode_cathode_color)
            dark.setAlphaF(alpha)
            circle.setBrush(QBrush(dark))
            circle.setPen(_body_pen(border, alpha))
        else:
            # Anode/(+) visible from above → hollow ring (polarity indicator)
            dark = QColor(theme.body_diode_anode_color)
            circle.setBrush(QBrush(dark))
            thick_pen = QPen(border, diam_r * 0.15)
            thick_pen.setColor(border)
            c = QColor(border)
            c.setAlphaF(alpha)
            thick_pen.setColor(c)
            circle.setPen(thick_pen)

        items.append(circle)
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


def _resolve_cylinder(
    defn: ComponentDefinition,
    instance: ComponentInstance | None,
    body_types: dict[str, BodyTypeSpec],
    polar: bool,
    vertical_flipped: bool = False,
) -> CylinderBody | None:
    """Look up a BodyVariant and return CylinderBody, or None if not found."""
    family_key = _CLASS_TO_FAMILY.get(defn.component_class)
    if family_key is None:
        return None
    spec = body_types.get(family_key)
    if spec is None:
        return None

    variant: BodyVariant | None = None
    if instance and instance.body_variant:
        parts = instance.body_variant.split("/", 1)
        if len(parts) == 2 and parts[0] == family_key:
            variant = spec.get_variant(parts[1])
    if variant is None:
        variant = spec.default_variant()

    return CylinderBody(variant, polar, vertical_flipped=vertical_flipped)


# Map component class → body-type family name
_CLASS_TO_FAMILY: dict[ComponentClass, str] = {
    ComponentClass.RESISTOR:           "resistor_axial",
    ComponentClass.INDUCTOR:           "inductor_axial",
    ComponentClass.FUSE:               "resistor_axial",   # same shape as resistor
    ComponentClass.CAPACITOR_POLAR:    "capacitor_axial",
    ComponentClass.CAPACITOR_NONPOLAR: "capacitor_axial",
    ComponentClass.DIODE:              "diode_axial",
    ComponentClass.DIODE_ZENER:        "diode_axial",
    ComponentClass.DIODE_LED:          "diode_axial",
}

_POLAR_CLASSES = frozenset({
    ComponentClass.CAPACITOR_POLAR,
    ComponentClass.DIODE,
    ComponentClass.DIODE_ZENER,
    ComponentClass.DIODE_LED,
})


def body_shape_for(
    defn: ComponentDefinition,
    placement: ComponentPlacement | None = None,
    instance: ComponentInstance | None = None,
    body_types: dict[str, BodyTypeSpec] | None = None,
) -> BodyShape:
    """Return the appropriate BodyShape for *defn*.

    When *body_types* is provided, cylindrical 2-pin components use
    ``CylinderBody`` with the correct physical variant.  Otherwise falls
    back to QPainterPath renderers.
    """
    fp  = defn.footprint
    cls = defn.component_class
    bt  = body_types or {}

    if isinstance(fp, TO220Footprint):
        return TO220Body()

    if isinstance(fp, SingleRow):
        if fp.n == 2 and cls in (_AXIAL_CLASSES | _DIODE_CLASSES | _RADIAL_CLASSES):
            polar = cls in _POLAR_CLASSES
            vf = placement.vertical_flipped if placement is not None else False
            cyl = _resolve_cylinder(defn, instance, bt, polar, vertical_flipped=vf)
            if cyl is not None:
                return cyl
            # Fallback when no body_types loaded
            if cls in _AXIAL_CLASSES:
                return AxialBody()
            if cls in _DIODE_CLASSES:
                return DiodeBody()
            return RadialBody()

        if fp.n == 3 and cls in _TRANSISTOR_CLASSES:
            return TO92Body()
        return SvgSILBody()

    if isinstance(fp, DualRow):
        return SvgDIPBody(fp.n_per_row, fp.rows_spacing)

    if isinstance(fp, QuadRow):
        return SvgQuadDIPBody(fp.n_per_col, fp.col_spacings, fp.pitch)

    return GenericBody()
