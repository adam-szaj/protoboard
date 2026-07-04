# SPDX-License-Identifier: MIT
"""Disc renderer — ceramic disc cap, button cell, disc varistor.

A disc is radial by construction, so nearly every draw goes through the base
class's radial branch; the disc's silhouette there is the edge-mounted lens
side profile drawn here.  The non-radial fallback is the bbox rectangle from
the base.
"""

from __future__ import annotations

from PySide6.QtCore import QRectF
from PySide6.QtGui import QBrush, QColor, QPainterPath, QTransform
from PySide6.QtWidgets import QGraphicsItem, QGraphicsPathItem

from protoboard.domain.components.package.body import DiscProfile
from protoboard.domain.components.package.shapes import (
    Disc as _PkgDisc,
)
from protoboard.geometry import Point
from protoboard.gui.renderers.packages.base import PackageRenderer
from protoboard.gui.renderers.packages.canvas import _body_pen
from protoboard.gui.theme import Theme


class DiscRenderer(PackageRenderer):
    """Disc package — an edge-mounted lens as the radial top-view profile."""

    renders = "disc"

    def _radial_silhouette(
        self,
        mid: Point, p1: Point, p2: Point, neg_pin: Point,
        fill: QColor, border: QColor, alpha: float, z: float,
        theme: Theme,
    ) -> list[QGraphicsItem]:
        pkg = self._pkg
        if not isinstance(pkg, _PkgDisc):
            return super()._radial_silhouette(
                mid, p1, p2, neg_pin, fill, border, alpha, z, theme,
            )
        # A disc cap mounts on its edge: from above it is a thin side profile,
        # not the round face — a lens (its diameter along the lead axis, its
        # thickness across).
        mm = theme.raster_pitch_mm
        axis_angle = (p2 - p1).angle_deg()
        return [self._radial_disc_side(
            mid, pkg.diameter_mm / mm, pkg.thickness_mm / mm, axis_angle,
            pkg.profile, fill, border, alpha, z,
        )]

    def _radial_body_radius(self, mm: float) -> float:
        pkg = self._pkg
        if isinstance(pkg, _PkgDisc):
            return pkg.diameter_mm / mm / 2.0
        return super()._radial_body_radius(mm)

    def _radial_disc_side(
        self,
        centre: Point,
        length: float,   # the disc diameter, along the lead axis
        width: float,    # the disc thickness, across the lead axis
        axis_angle_deg: float,
        profile: DiscProfile,
        fill: QColor, border: QColor, alpha: float, z: float,
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
