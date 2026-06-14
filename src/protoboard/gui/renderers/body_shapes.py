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

from protoboard.domain.components.definition import (
    ComponentClass,
    ComponentDefinition,
)
from protoboard.domain.components.footprints import (
    DualRow,
    QuadRow,
    SingleRow,
    TO220Footprint,
    TwoPin,
)
from protoboard.domain.components.two_pin_body import (
    BodyShape as _TPShape,
    LeadArrangement as _TPArrangement,
    TwoPinBodyPackage,
    TwoPinBodySpec,
)
from protoboard.domain.components.instance import ComponentInstance
from protoboard.domain.components.package.shapes import (
    Cylinder as _PkgCylinder,
    Package,
    Prism as _PkgPrism,
)
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
#
# TODO: Replace the four ``Svg*`` renderers (SvgBodyShape, SvgDIPBody,
# SvgSILBody, SvgQuadDIPBody) with a parametric ``MultiPinParametricBody``
# mirroring the TwoPinParametricBody refactor.  Tracked in docs/todo.md
# ("Parametric renderer for DIP / QIL / SIL packages").  ``SvgBodyShape``
# is already dead code (no body_shape_for branch returns it) and could
# be removed today as the quick-win mentioned in that TODO entry.
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
# TwoPinParametricBody — renders any TwoPinBodySpec
# ---------------------------------------------------------------------------


class TwoPinParametricBody(BodyShape):
    """Render a two-pin component from its :class:`TwoPinBodySpec`.

    Dispatches on ``spec.lead_arrangement`` × ``spec.shape``:

    * AXIAL × CYLINDER → side view: lead, capsule, lead.
    * AXIAL × PRISM    → side view: lead, rounded box, lead.
    * AXIAL × DISC     → side view: lead, thin puck rectangle, lead.
    * RADIAL × CYLINDER → top view: solid circle on the anchor pad with
                          a single lead reaching the other pad.
    * RADIAL × PRISM    → top view: solid rectangle.
    * RADIAL × DISC     → top view: solid circle (CANDY variant gets a
                          darker rim ring to suggest the dome).

    Polar components get a cathode stripe in AXIAL mode and a hollow
    ring vs solid fill discriminator in RADIAL mode (matches the legacy
    CylinderBody convention so existing screenshots stay recognisable).

    ``vertical_flipped`` swaps which pad is the anchor — used by
    ComponentPlacement to mount a polar AXIAL part the other way round
    in horizontal layouts (mirrors the cathode stripe).
    """

    # ComponentClasses that show a polarity marker — diodes get a
    # cathode stripe (AXIAL) or solid-vs-hollow disc (RADIAL); polar
    # electrolytic caps share the same treatment.  Everything else
    # draws plain.
    _POLAR_CLASSES = frozenset({
        ComponentClass.DIODE,
        ComponentClass.DIODE_ZENER,
        ComponentClass.DIODE_LED,
        ComponentClass.CAPACITOR_POLAR,
    })

    def __init__(
        self,
        spec: TwoPinBodySpec,
        component_class: ComponentClass = ComponentClass.OTHER,
        vertical_flipped: bool = False,
    ) -> None:
        self._spec = spec
        self._component_class = component_class
        self._vertical_flipped = vertical_flipped

    @property
    def _is_polar(self) -> bool:
        """Class-flavour polarity flag — drives cathode stripe / hollow-ring."""
        return self._component_class in self._POLAR_CLASSES

    def _material_palette(
        self, theme: Theme, fallback_fill: QColor, fallback_border: QColor,
    ) -> tuple[QColor, QColor, float]:
        """Return ``(fill, border, fill_alpha_mult)`` from theme.material_palette.

        Falls back to the caller-supplied colours when the spec's
        material is missing from the theme palette (custom themes that
        only override a subset).
        """
        entry = theme.material_palette.get(self._spec.material.value)
        if entry is None:
            return fallback_fill, fallback_border, 1.0
        fill_hex, border_hex, alpha_mult = entry
        return QColor(fill_hex), QColor(border_hex), alpha_mult

    # ------------------------------------------------------------------
    # BodyShape interface
    # ------------------------------------------------------------------

    def drawn_pin_numbers(
        self,
        positions: list[Point],
        theme: Theme = DEFAULT_THEME,
    ) -> frozenset[int]:
        # Only the radial-mount top-view occludes the anchor pad with a
        # solid body silhouette; component_renderer skips its own marker
        # there so the body is the dot.  Axial / side-view modes leave
        # the pads clear.
        if self._spec.lead_arrangement is _TPArrangement.RADIAL:
            anchor_pin = 2 if self._vertical_flipped else 1
            return frozenset({anchor_pin})
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

        # ``vertical_flipped`` swaps the anchor end so the polarity
        # marker mirrors when the part is rotated 180° on the board.
        if self._vertical_flipped:
            p1, p2 = positions[1], positions[0]
        else:
            p1, p2 = positions[0], positions[1]

        dx = p2.x - p1.x
        dy = p2.y - p1.y
        dist = math.hypot(dx, dy)
        if dist < 1e-9:
            return GenericBody().draw(positions, fill, border, alpha, z, theme)

        if self._spec.lead_arrangement is _TPArrangement.AXIAL:
            return self._draw_axial(
                p1, p2, dx, dy, dist, fill, border, alpha, z, theme,
            )
        return self._draw_radial(
            p1, p2, dist, fill, border, alpha, z, theme,
        )

    # ------------------------------------------------------------------
    # AXIAL rendering — side-view body between the two pads
    # ------------------------------------------------------------------

    def _axial_dims(self, theme: Theme) -> tuple[float, float]:
        """Return (body_length, body_width) in raster units for the AXIAL
        side-view bounding box.  ``length`` is along the lead axis,
        ``width`` is the visible thickness.
        """
        mm = theme.raster_pitch_mm
        spec = self._spec
        if spec.shape is _TPShape.CYLINDER:
            assert spec.length_mm is not None and spec.diameter_mm is not None
            return spec.length_mm / mm, spec.diameter_mm / mm
        if spec.shape is _TPShape.PRISM:
            assert spec.length_mm is not None and spec.width_mm is not None
            return spec.length_mm / mm, spec.width_mm / mm
        # DISC — side view shows thickness along axis, diameter across.
        assert spec.thickness_mm is not None and spec.diameter_mm is not None
        return spec.thickness_mm / mm, spec.diameter_mm / mm

    def _axial_corner_radius(
        self, body_len: float, body_w: float, theme: Theme,
    ) -> float:
        spec = self._spec
        if spec.shape is _TPShape.CYLINDER:
            # Cylinders look like capsules — fully rounded ends.
            return min(body_w / 2.0, theme.body_corner_radius)
        if spec.shape is _TPShape.PRISM:
            # Prisms follow the theme's edge-style hint.
            return theme.body_corner_radius if spec.edge.value == "rounded" else 0.0
        # DISC side view → flat rectangle, slight rounding looks natural.
        return min(body_w * 0.1, theme.body_corner_radius)

    def _draw_axial(
        self,
        p1: Point, p2: Point,
        dx: float, dy: float, dist: float,
        fill: QColor, border: QColor, alpha: float, z: float,
        theme: Theme,
    ) -> list[QGraphicsItem]:
        body_len, body_w = self._axial_dims(theme)
        mid_x = (p1.x + p2.x) / 2.0
        mid_y = (p1.y + p2.y) / 2.0
        angle_deg = math.degrees(math.atan2(dy, dx))

        # Material palette decides the default body fill / border;
        # caller-supplied colours are used when no palette entry matches.
        mat_fill, mat_border, mat_alpha = self._material_palette(
            theme, fill, border,
        )

        items: list[QGraphicsItem] = []
        lead_pen = _lead_pen(theme, alpha)
        bp = _body_pen(mat_border, alpha)

        # Leads — only when pad spacing exceeds the body length.
        if dist > body_len + 1e-6:
            unit_x = dx / dist
            unit_y = dy / dist
            attach = dist / 2.0 - body_len / 2.0
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

        # Body silhouette.
        body_path = QPainterPath()
        r = self._axial_corner_radius(body_len, body_w, theme)
        body_path.addRoundedRect(
            QRectF(-body_len / 2.0, -body_w / 2.0, body_len, body_w), r, r,
        )

        body = QGraphicsPathItem(body_path)
        fill_c = QColor(mat_fill)
        fill_c.setAlphaF(fill_c.alphaF() * alpha * mat_alpha)
        body.setBrush(QBrush(fill_c))
        body.setPen(bp)
        body.setZValue(z)

        t = QTransform()
        t.translate(mid_x, mid_y)
        t.rotate(angle_deg)
        body.setTransform(t)
        items.append(body)

        # Polar cathode stripe — class flavour decision.  The stripe
        # always sits at the p2 end of the body in local coordinates;
        # ``_vertical_flipped`` was absorbed into the p1/p2 swap upstream.
        if self._is_polar:
            stripe_w = min(body_len * 0.12, body_w * 0.4)
            stripe_x = body_len / 2.0 - stripe_w
            stripe_path = QPainterPath()
            stripe_path.addRect(
                QRectF(stripe_x, -body_w / 2.0, stripe_w, body_w),
            )
            stripe = QGraphicsPathItem(stripe_path)
            dark = QColor(theme.body_led_stripe_color)
            dark.setAlphaF(alpha)
            stripe.setBrush(QBrush(dark))
            stripe.setPen(_body_pen(mat_border, alpha, width=0.0))
            stripe.setZValue(z + 0.01)
            t_stripe = QTransform()
            t_stripe.translate(mid_x, mid_y)
            t_stripe.rotate(angle_deg)
            stripe.setTransform(t_stripe)
            items.append(stripe)
        return items

    # ------------------------------------------------------------------
    # RADIAL rendering — top-view body on the anchor pad
    # ------------------------------------------------------------------

    def _draw_radial(
        self,
        p1: Point, p2: Point,
        dist: float,
        fill: QColor, border: QColor, alpha: float, z: float,
        theme: Theme,
    ) -> list[QGraphicsItem]:
        """Top-down silhouette on the anchor pad with a single lead
        reaching the other pad."""
        items: list[QGraphicsItem] = []
        lead_pen = _lead_pen(theme, alpha)

        lead = QGraphicsLineItem(p1.x, p1.y, p2.x, p2.y)
        lead.setPen(lead_pen)
        lead.setZValue(z - 0.01)
        items.append(lead)

        spec = self._spec
        mm = theme.raster_pitch_mm
        # ``upper_pin`` is the pin sticking up out of the body in the
        # top-down view — used by the polar disc to choose solid vs
        # hollow.  For ``vertical_flipped=False`` anchor is pin 1 and
        # pin 2 stands proud.
        upper_pin = 1 if self._vertical_flipped else 2

        # Material palette decides the base body colour.
        mat_fill, mat_border, mat_alpha = self._material_palette(
            theme, fill, border,
        )

        if spec.shape is _TPShape.CYLINDER or spec.shape is _TPShape.DISC:
            assert spec.diameter_mm is not None
            diam = spec.diameter_mm / mm
            items.append(self._radial_disc_or_cylinder(
                p1, diam, mat_fill, mat_border, alpha, z, theme,
                upper_pin, mat_alpha,
            ))
            return items

        # PRISM — top view is a rectangle whose long side aligns with
        # the lead direction; centred on the anchor pad.
        assert spec.length_mm is not None and spec.width_mm is not None
        length_r = spec.length_mm / mm
        width_r = spec.width_mm / mm
        dx, dy = p2.x - p1.x, p2.y - p1.y
        angle_deg = math.degrees(math.atan2(dy, dx))
        rect_path = QPainterPath()
        r_corner = theme.body_corner_radius if spec.edge.value == "rounded" else 0.0
        rect_path.addRoundedRect(
            QRectF(-length_r / 2.0, -width_r / 2.0, length_r, width_r),
            r_corner, r_corner,
        )
        body = QGraphicsPathItem(rect_path)
        fill_c = QColor(mat_fill)
        fill_c.setAlphaF(fill_c.alphaF() * alpha * mat_alpha)
        body.setBrush(QBrush(fill_c))
        body.setPen(_body_pen(mat_border, alpha))
        body.setZValue(z)
        t = QTransform()
        t.translate(p1.x, p1.y)
        t.rotate(angle_deg)
        body.setTransform(t)
        items.append(body)
        return items

    def _radial_disc_or_cylinder(
        self,
        anchor: Point,
        diam: float,
        fill: QColor, border: QColor, alpha: float, z: float,
        theme: Theme,
        upper_pin: int,
        mat_alpha: float,
    ) -> QGraphicsItem:
        """Build the filled disc / cylinder cap for radial-mounted parts.

        Non-polar parts → plain filled circle in the material palette.
        Polar parts (``_is_polar``) use the legacy CylinderBody
        discriminator: cathode (pin 2) on top → solid dark circle;
        anode (pin 1) on top → light fill with a thick rim (hollow ring
        look).
        """
        r = diam / 2.0
        circle = QGraphicsEllipseItem(QRectF(
            anchor.x - r, anchor.y - r, diam, diam,
        ))
        circle.setZValue(z)

        if not self._is_polar:
            fill_c = QColor(fill)
            fill_c.setAlphaF(fill_c.alphaF() * alpha * mat_alpha)
            circle.setBrush(QBrush(fill_c))
            circle.setPen(_body_pen(border, alpha))
            return circle

        if upper_pin == 2:
            # Cathode visible from above → solid dark disc.
            dark = QColor(theme.body_diode_cathode_color)
            dark.setAlphaF(alpha)
            circle.setBrush(QBrush(dark))
            circle.setPen(_body_pen(border, alpha))
        else:
            # Anode visible from above → light fill with thick rim.
            light = QColor(theme.body_diode_anode_color)
            circle.setBrush(QBrush(light))
            rim = QColor(border)
            rim.setAlphaF(alpha)
            circle.setPen(QPen(rim, diam * 0.15))
        return circle


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


def _resolve_two_pin_spec(
    defn: ComponentDefinition,
    instance: ComponentInstance | None,
    packages: dict[str, TwoPinBodyPackage],
) -> TwoPinBodySpec | None:
    """Resolve the :class:`TwoPinBodySpec` to render *defn* with.

    Packages (``DO-35``, ``DIN-0207``, ``HC-49`` …) describe physical
    bodies and know nothing about the electrical class of the part that
    uses them.  A library entry pins the package + variant via
    ``ComponentDefinition.default_body_variant``; an instance may
    override per part via ``ComponentInstance.body_variant``.

    Resolution order:

    1. Explicit ``instance.body_variant`` of form ``"package_id/variant_id"``.
    2. Library default ``ComponentDefinition.default_body_variant``.
    3. ``None`` — caller falls back to :class:`GenericBody`.

    There is no implicit class-to-package mapping: classes select
    flavour (cathode stripe, polarity marker, …), packages select the
    physical body.
    """
    # 1. Explicit per-instance variant.
    if instance is not None and instance.body_variant:
        parts = instance.body_variant.split("/", 1)
        if len(parts) == 2:
            pkg = packages.get(parts[0])
            if pkg is not None:
                v = pkg.get_variant(parts[1])
                if v is not None:
                    return v

    # 2. Library default declared on the component definition.
    if defn.default_body_variant:
        parts = defn.default_body_variant.split("/", 1)
        if len(parts) == 2:
            pkg = packages.get(parts[0])
            if pkg is not None:
                v = pkg.get_variant(parts[1])
                if v is not None:
                    return v
    return None


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
    """Render a component body from its :class:`Package` geometry."""

    def __init__(self, package: Package) -> None:
        self._pkg = package

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
        if isinstance(self._pkg, _PkgCylinder) and len(positions) >= 2:
            return self._draw_cylinder(
                self._pkg, positions, fill, border, alpha, z, theme,
            )
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

        # Capsule: a rounded rectangle, fully rounded ends.
        r = min(body_w / 2.0, body_len / 2.0)
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


def _package_corner_radius(pkg: Package) -> float:
    """Corner radius (mm) declared by the package shape, or 0."""
    if isinstance(pkg, _PkgCylinder):
        return pkg.edge_radius_mm
    if isinstance(pkg, _PkgPrism):
        return pkg.corner_radius_mm
    return 0.0


def body_shape_for(
    defn: ComponentDefinition,
    placement: ComponentPlacement | None = None,
    instance: ComponentInstance | None = None,
    two_pin_families: dict[str, TwoPinBodyPackage] | None = None,
) -> BodyShape:
    """Return the appropriate :class:`BodyShape` for *defn*.

    Two-pin components (``TwoPin`` footprint) dispatch through
    :class:`TwoPinParametricBody` when a matching :class:`TwoPinBodySpec`
    is resolvable from *two_pin_families*; without a spec they fall back
    to :class:`GenericBody`.

    Larger packages keep their dedicated renderers: TO-92 for 3-pin
    transistor, TO-220 for power packages, SVG-based DIP for two-row
    dual in-line packages, SIL header for three-or-more in-line headers.
    """
    # A parametric package (new spec model) is the source of truth for the
    # body geometry when present; legacy per-footprint renderers are the
    # fallback for definitions that predate it.
    if defn.package is not None:
        return PackageBody(defn.package)

    fp  = defn.footprint
    cls = defn.component_class
    fams = two_pin_families or {}
    vf = placement.vertical_flipped if placement is not None else False

    if isinstance(fp, TO220Footprint):
        return TO220Body()

    if isinstance(fp, TwoPin):
        spec = _resolve_two_pin_spec(defn, instance, fams)
        if spec is not None:
            return TwoPinParametricBody(
                spec, component_class=cls, vertical_flipped=vf,
            )
        return GenericBody()

    if isinstance(fp, SingleRow):
        # Single-row footprints are 3-or-more by convention (TwoPin
        # owns the n=2 case).  Transistors get TO-92, other SILs use
        # the headed renderer.
        if fp.n == 3 and cls in _TRANSISTOR_CLASSES:
            return TO92Body()
        # Legacy escape hatch: if a caller still constructs SingleRow(n=2)
        # in Python (older tests / migration tooling), dispatch through
        # the parametric body when a spec resolves so the visual stays
        # consistent.
        if fp.n == 2:
            spec = _resolve_two_pin_spec(defn, instance, fams)
            if spec is not None:
                return TwoPinParametricBody(
                    spec, component_class=cls, vertical_flipped=vf,
                )
            return GenericBody()
        return SvgSILBody()

    if isinstance(fp, DualRow):
        return SvgDIPBody(fp.n_per_row, fp.rows_spacing)

    if isinstance(fp, QuadRow):
        return SvgQuadDIPBody(fp.n_per_col, fp.col_spacings, fp.pitch)

    return GenericBody()
