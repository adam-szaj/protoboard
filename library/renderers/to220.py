# SPDX-License-Identifier: MIT
"""TO-220 renderer plugin — the radial prism body plus the metal mounting tab.

This file doubles as the reference for user renderer plugins: it lives in the
builtin library's ``renderers/`` directory and is loaded by the exact same
discovery mechanism as user plugins (``load_renderer_plugins``).  Declaring
``renders = "TO-220"`` registers the class for the whole TO-220 package family
(the family id sits on the package ``lineage`` just above the variant), so
every variant gets the tab unless a more specific renderer claims its leaf.

The base ``PrismRenderer`` draws the radial rectangle (the plastic body,
centred on the pad centroid, aligned to the lead axis); this subclass appends
the characteristic light metal tab strip along the body's back edge — the
behaviour the legacy hand-coded ``TO220Body`` had, now restored as a plugin
(docs/todo.md "TO-220 metal tab render").
"""

from __future__ import annotations

from PySide6.QtCore import QRectF
from PySide6.QtGui import QBrush, QColor, QPainterPath, QTransform
from PySide6.QtWidgets import QGraphicsItem, QGraphicsPathItem

from protoboard.domain.components.package.shapes import Prism
from protoboard.geometry import Point
from protoboard.gui.renderers.packages import PrismRenderer
from protoboard.gui.renderers.packages.base import _body_pen
from protoboard.gui.theme import Theme


class TO220Renderer(PrismRenderer):
    """TO-220 (and friends): the prism body + a metal tab on the back edge."""

    renders = "TO-220"

    def render_body(
        self,
        positions: list[Point],
        fill: QColor,
        border: QColor,
        alpha: float,
        z: float,
        theme: Theme,
    ) -> list[QGraphicsItem]:
        items = super().render_body(positions, fill, border, alpha, z, theme)
        tab = self._metal_tab(positions, border, alpha, z, theme)
        if tab is not None:
            items.append(tab)
        return items

    def _metal_tab(
        self,
        positions: list[Point],
        border: QColor,
        alpha: float,
        z: float,
        theme: Theme,
    ) -> QGraphicsItem | None:
        """The light metal tab strip along the body's back edge.

        Geometry mirrors the radial-rectangle frame the base class used: the
        frame origin is the pad centroid, local +x runs along the lead axis
        (pin 1 → last pin), so the body spans ``length × width`` around it.
        The tab is a ``length × tab_h`` strip attached to the +y long edge —
        flipping the part (``vertical_flipped`` reverses the pad axis) turns
        the tab to the other side, matching the physical package flip.
        """
        pkg = self._pkg
        if not isinstance(pkg, Prism) or len(positions) < 2:
            return None
        mm = theme.raster_pitch_mm
        bb = pkg.bbox()
        body_len = bb.height   # along the lead axis (local +x in this frame)
        body_w = bb.width      # across it (local +y)
        tab_h = theme.to220_tab_height_mm / mm

        # Same anchor pair the radial branch used (vertical_flipped swaps).
        if self._vertical_flipped:
            p1, p2 = positions[1], positions[0]
        else:
            p1, p2 = positions[0], positions[1]
        axis = p2 - p1  # Point − Point → Vector
        if axis.length() < 1e-9:
            return None
        all_pads = positions
        mid = Point(
            sum(p.x for p in all_pads) / len(all_pads),
            sum(p.y for p in all_pads) / len(all_pads),
        )

        path = QPainterPath()
        corner = 0.1 / mm
        path.addRoundedRect(
            QRectF(-body_len / 2.0, body_w / 2.0 - tab_h, body_len, tab_h),
            corner, corner,
        )
        tab = QGraphicsPathItem(path)
        tab_color = QColor(0xDD, 0xDD, 0xDD)
        tab_color.setAlphaF(tab_color.alphaF() * alpha)
        tab.setBrush(QBrush(tab_color))
        tab.setPen(_body_pen(border, alpha, width=theme.body_tab_pen_width))
        # Just above the plastic body so the strip reads as a separate part.
        tab.setZValue(z + 0.005)
        t = QTransform()
        t.translate(mid.x, mid.y)
        t.rotate(axis.angle_deg())
        tab.setTransform(t)
        return tab
