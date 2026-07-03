# SPDX-License-Identifier: MIT
"""Per-package body shape renderers — compatibility surface.

The implementation lives in :mod:`protoboard.gui.renderers.packages`: one
renderer class per package lineage node (``CylinderRenderer`` /
``PrismRenderer`` / ``DiscRenderer`` + user plugins), dispatched by
:func:`~protoboard.gui.renderers.packages.renderer_for` over the package's
``lineage``.  This module keeps the historical entry points stable:

- :func:`body_shape_for` — the production dispatch (unchanged signature);
- :func:`PackageBody` — constructor-compatible factory returning the
  lineage-dispatched renderer (tests build bodies directly through it);
- ``GenericBody`` — the bare-bbox fallback for a component with no package;
- ``TO92Body`` / ``TO220Body`` — legacy hand-coded renderers, no longer in
  the production dispatch (kept until their behaviour tests are repointed at
  the package renderers; see docs/todo.md).

Usage::

    shape = body_shape_for(defn)
    items = shape.draw(positions, fill, border, alpha, z, theme)
    for item in items:
        scene.addItem(item)
"""

from __future__ import annotations

import math

from PySide6.QtCore import QRectF
from PySide6.QtGui import QBrush, QColor, QPainterPath, QTransform
from PySide6.QtWidgets import (
    QGraphicsItem,
    QGraphicsLineItem,
    QGraphicsPathItem,
)

from protoboard.domain.components.appearance import Appearance
from protoboard.domain.components.definition import ComponentDefinition
from protoboard.domain.components.instance import ComponentInstance
from protoboard.domain.components.package.family import PackageFamily
from protoboard.domain.layout.placement import ComponentPlacement
from protoboard.geometry import Point
from protoboard.gui.renderers.packages import (
    BodyShape,
    GenericBody,
    PackageRenderer,
)
from protoboard.gui.renderers.packages.base import (
    _big_body_alpha,
    _body_pen,
    _lead_pen,
)
from protoboard.gui.theme import DEFAULT_THEME, Theme

__all__ = [
    "BodyShape",
    "GenericBody",
    "PackageBody",
    "TO92Body",
    "TO220Body",
    "body_shape_for",
]


# Historically ``PackageBody`` was one class whose ``draw`` branched on the
# package shape; the branches are now ``PackageRenderer`` subclasses and the
# dispatch lives in ``PackageRenderer.__new__`` (constructing the base class
# yields the lineage-matched subclass).  The old name stays valid for both
# construction (``PackageBody(pkg)``) and ``isinstance`` checks.
PackageBody = PackageRenderer


# ---------------------------------------------------------------------------
# TO92Body — legacy hand-coded transistor body (tests only; production TO-92
# renders through the flat-sided Cylinder package)
#
#  [1] [2] [3]
#   |   |   |
#  ╔═══D═══╗   (D-shape: flat bottom, rounded top)
# ---------------------------------------------------------------------------

class TO92Body(BodyShape):
    def draw(
        self,
        positions: list[Point],
        fill: QColor,
        border: QColor,
        alpha: float,
        z: float,
        theme: Theme = DEFAULT_THEME,
    ) -> list[QGraphicsItem]:
        if len(positions) < 3:
            return GenericBody().draw(positions, fill, border, alpha, z, theme)

        fill = QColor(0x40, 0x40, 0x40)
        fill.setAlphaF(alpha * _big_body_alpha)
        # Pin 2 is the middle pin; body centred above it.
        p_mid = positions[1]

        pin_dx = positions[2].x - positions[0].x
        pin_dy = positions[2].y - positions[0].y
        perp_len = math.hypot(pin_dx, pin_dy)
        if perp_len < 1e-6:
            return GenericBody().draw(positions, fill, border, alpha, z, theme)
        row = positions[2] - positions[0]  # Point − Point → Vector
        # Perpendicular pointing left of row direction
        perp_x = -row.dy / perp_len
        perp_y = -row.dx / perp_len

        mm = theme.raster_pitch_mm
        w = theme.to92_body_width_mm / mm
        h = theme.to92_body_height_mm / mm
        body_offset = 0.0

        cx = p_mid.x + perp_x * body_offset
        cy = p_mid.y + perp_y * body_offset

        row_angle = row.angle_deg()

        # D-shape path (flat on pin side, rounded on top)
        path = QPainterPath()
        path.moveTo(-w / 2, h / 2)
        path.lineTo(w / 2, h / 2)
        path.lineTo(w / 2, 0)
        path.arcTo(QRectF(-w / 2, -h / 2, w, h), 0, 180)
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
            ex = p.x + perp_x * body_offset
            ey = p.y + perp_y * body_offset
            line = QGraphicsLineItem(p.x, p.y, ex, ey)
            line.setPen(lead)
            line.setZValue(z - 0.01)
            items.append(line)

        items.append(body)
        return items


# ---------------------------------------------------------------------------
# TO220Body — legacy hand-coded power package (tests only; the production
# TO-220 will render through a Prism package + the TO-220 renderer plugin)
#
#  [1] [2] [3]
#   |   |   |
#  ╔═══════════╗
#  ║           ║  ← rectangular body + flat metal tab on top
#  ╚═══════════╝
#  ════════════    ← tab (thinner rect)
# ---------------------------------------------------------------------------

class TO220Body(BodyShape):
    def draw(
        self,
        positions: list[Point],
        fill: QColor,
        border: QColor,
        alpha: float,
        z: float,
        theme: Theme = DEFAULT_THEME,
    ) -> list[QGraphicsItem]:
        if len(positions) < 3:
            return GenericBody().draw(positions, fill, border, alpha, z, theme)

        row = positions[2] - positions[0]  # Point − Point → Vector
        perp_len = row.length()
        if perp_len < 1e-6:
            return GenericBody().draw(positions, fill, border, alpha, z, theme)

        # Perpendicular unit vector pointing "away" from user (left of row dir)
        perp_x = -row.dy / perp_len
        perp_y = row.dx / perp_len

        p_mid = positions[1]
        row_angle = row.angle_deg()

        mm = theme.raster_pitch_mm
        w = theme.to220_body_width_mm / mm
        h = theme.to220_body_height_mm / mm
        tab_h = theme.to220_tab_height_mm / mm
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
        tab_path.addRoundedRect(
            QRectF(-w / 2 - overhang, -tab_h / 2, w + 2 * overhang, tab_h),
            0.1 / mm, 0.1 / mm,
        )
        tab = QGraphicsPathItem(tab_path)
        tab_color = QColor(0xdd, 0xdd, 0xdd)
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

def body_shape_for(
    defn: ComponentDefinition,
    placement: ComponentPlacement | None = None,
    instance: ComponentInstance | None = None,
    package_families: dict[str, PackageFamily] | None = None,
) -> BodyShape:
    """Return the appropriate :class:`BodyShape` for *defn*.

    A parametric package (resolved from the instance/definition/catalogue via
    :func:`resolve_component_package`) is the source of truth for the body and
    renders through the lineage-dispatched :class:`PackageRenderer` subclass
    (see :func:`protoboard.gui.renderers.packages.renderer_for`); a part
    without a package draws a generic bbox.
    """
    cls = defn.component_class
    vf = placement.vertical_flipped if placement is not None else False
    side = placement.side if placement is not None else "top"
    # Visual-only override (fill colour, corner radius) layered over the
    # package/theme defaults — instance wins over definition, per field.
    eff_appearance = Appearance.resolve(
        instance.appearance if instance is not None else None,
        defn.appearance,
    )

    # A parametric package (new spec model) is the source of truth for the
    # body geometry when one resolves: the instance's package_variant, the
    # definition's package_family_ref (both against the package-family
    # catalogue), or an inline defn.package — see resolve_component_package.
    # Polarity / anchor come from the class and placement, not the
    # (class-agnostic) package.
    from protoboard.domain.components.package import resolve_component_package
    pkg = resolve_component_package(defn, instance, package_families or {})
    if pkg is not None:
        return PackageBody(
            pkg, component_class=cls, vertical_flipped=vf,
            appearance=eff_appearance, side=side,
            placement=placement,
        )

    # No explicit package resolved: a footprint is only a pin cloud (pin count
    # + spacing) and does not determine a body — the same footprint fits many
    # bodies (a SIL-3 is a TO-92, a header, a trimmer…). A part that wants a
    # specific silhouette declares it via ``package`` / ``package_family_ref``
    # (resolved above). Without one, draw the generic bbox body.
    return GenericBody()
