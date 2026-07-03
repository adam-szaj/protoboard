# SPDX-License-Identifier: MIT
"""Prism renderer — DIP / SIL / dev-board silhouettes and the SMD chip."""

from __future__ import annotations

from PySide6.QtCore import QRectF
from PySide6.QtGui import QBrush, QColor, QPainterPath, QTransform
from PySide6.QtWidgets import QGraphicsItem, QGraphicsPathItem

from protoboard.domain.components.definition import ComponentClass
from protoboard.domain.components.package.shapes import (
    Prism as _PkgPrism,
)
from protoboard.geometry import Point
from protoboard.gui.renderers.packages.base import (
    PackageRenderer,
    _body_pen,
)
from protoboard.gui.renderers.prism_silhouette import silhouette_for_prism
from protoboard.gui.theme import Theme


class PrismRenderer(PackageRenderer):
    """Multi-pin prism (DIP / SIL / quad / dev-board module / SMD chip)."""

    renders = "prism"

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
        if isinstance(pkg, _PkgPrism) and len(positions) >= 2:
            prism_items = self._draw_prism(
                pkg, positions, fill, border, alpha, z, theme,
            )
            if prism_items is not None:
                return prism_items
        return self._draw_bbox(positions, fill, border, alpha, z, theme)

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
        # A two-pin prism is an SMD chip (0805 / 0603): a flat body that lies on
        # the two pads, centred on their midpoint and aligned to the pad axis
        # (so ortho and diagonal placements both render correctly).  It has no
        # column silhouette.
        if pkg.pin_count() == 2 and len(positions) >= 2:
            return self._draw_smd_chip(pkg, positions, fill, border, alpha, z, theme)

        # A dev-board module renders as a plain PCB rectangle — no DIP notch /
        # SIL slots — regardless of how its pins cluster into columns.  Suppress
        # the silhouette so the caller falls through to the plain bbox body, and
        # mark the pins with a faint pad-sized active-area ring (drawn there).
        if self._component_class is ComponentClass.DEVBOARD:
            sil = None
        else:
            # Declared body kind (dip_ic notch / dip_switch plain / …) when the
            # package carries one; otherwise the column-guessed silhouette.
            sil = silhouette_for_prism(pkg)
        if sil is None:
            return None
        # An appearance corner-radius override (mm) reaches the DIP/SIL body via
        # the silhouette builder, converted to holes; absent → the builder keeps
        # using theme.body_corner_radius (the notch radius is unaffected either way).
        corner_scalar = self._appearance.corner_radius_mm_scalar
        corner_holes = (
            corner_scalar / theme.raster_pitch_mm
            if corner_scalar is not None
            else None
        )
        drawing = sil.build(pkg, theme, corner_r=corner_holes)

        # Place the local-frame silhouette (pin 1 at the origin) through the
        # SAME local→world transform the pins use in pin_world_position:
        # reflect about the package flip axis on the bottom side, rotate, then
        # translate to the anchor.  Reconstructing orientation from the world
        # pins alone can't recover the reflection's chirality, which is why the
        # bottom-side DIP body used to drift to pin 1's old corner.
        place = self._body_placement(positions)

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
    # SMD chip — flat two-terminal body centred on the pad pair
    # ------------------------------------------------------------------

    def _draw_smd_chip(
        self,
        pkg: _PkgPrism,
        positions: list[Point],
        fill: QColor,
        border: QColor,
        alpha: float,
        z: float,
        theme: Theme,
    ) -> list[QGraphicsItem]:
        """A surface-mount chip (0805 / 0603): a flat rounded rectangle centred
        on the two pads and aligned to the pad axis.

        The chip's two terminals end on the pads, so the body bridges them: its
        length spans the pad gap plus a small overhang (the terminals tuck just
        under the chip ends), its width is the package body width.  Aligning to
        the pad axis makes ortho and diagonal placements both render correctly.
        No leads and no pin-1 marker — a chip carries neither.
        """
        mm = theme.raster_pitch_mm
        bb = pkg.bbox()
        body_w = bb.width    # across the terminal axis (package width)
        body_len = bb.height  # along the terminal axis (package length)

        p1, p2 = positions[0], positions[1]
        axis = p2 - p1  # Point − Point → Vector
        dist = axis.length()
        if dist < 1e-9:
            return self._draw_bbox(positions, fill, border, alpha, z, theme)

        mid_x = (p1.x + p2.x) / 2.0
        mid_y = (p1.y + p2.y) / 2.0
        angle_deg = axis.angle_deg()
        # The chip is drawn at its declared body length, centred on the pad
        # midpoint — a real SMD chip is shorter than the pad pitch and sits
        # between the pads, leaving the metallised pads visible at each end.
        # (It is *not* stretched to span the pad gap, which read as a long thin
        # rectangle.)
        length = body_len
        r = min(self._effective_corner_radius_mm() / mm, body_w / 2.0, length / 2.0)

        path = QPainterPath()
        path.addRoundedRect(
            QRectF(-length / 2.0, -body_w / 2.0, length, body_w), r, r,
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
        return [body]
