# SPDX-License-Identifier: MIT
"""Prism renderer — DIP / SIL / dev-board silhouettes and the SMD chip."""

from __future__ import annotations

from typing import ClassVar

from PySide6.QtWidgets import QGraphicsItem

from protoboard.domain.components.definition import ComponentClass
from protoboard.domain.components.package.shapes import (
    Prism as _PkgPrism,
)
from protoboard.geometry import Point
from protoboard.gui.renderers.packages.base import (
    PackageRenderer,
    register_renderer,
)
from protoboard.gui.renderers.prism_silhouette import (
    PrismSilhouette,
    silhouette_for,
)


class PrismRenderer(PackageRenderer):
    """Multi-pin prism (DIP / SIL / quad / dev-board module / SMD chip)."""

    # Re-annotated so subclasses may register several names (a tuple) without
    # mypy narrowing the attribute to ``str`` from this assignment.
    renders: ClassVar[str | tuple[str, ...] | None] = "prism"

    def _silhouette(self, pkg: _PkgPrism) -> PrismSilhouette | None:
        """The body silhouette — the kind renderers' override point.

        **Declared beats guessed.** A package the author declared explicitly
        (an inline ``package:`` block, an authored family variant) renders as
        exactly what was written — the plain bbox rectangle — unless a
        renderer registered for its ``kind`` / family / lineage provides a
        declared look (``prism_kinds``: DIP notch, SIL housing, plain
        connector; generated ``dip-N`` / ``sil-N`` families carry their kind
        on the lineage). Only the synthesized ``_generic`` body of a bare
        ``footprint:`` part — where nothing was declared — falls back to the
        pin-column guess (``silhouette_for``), preserving the historical look
        of footprint-only DIP/SIL parts.  ``None`` → the plain bbox rectangle.
        """
        from protoboard.domain.components.package.kind import (
            is_generic_package,
        )

        if is_generic_package(pkg):
            return silhouette_for(pkg)
        return None

    def render_body(self, positions: list[Point]) -> list[QGraphicsItem]:
        radial = self._maybe_radial(positions)
        if radial is not None:
            return radial
        pkg = self._pkg
        if isinstance(pkg, _PkgPrism) and len(positions) >= 2:
            prism_items = self._draw_prism(pkg, positions)
            if prism_items is not None:
                return prism_items
        return self._draw_bbox(positions)

    # ------------------------------------------------------------------
    # Prism — DIP / SIL / quad silhouette placed onto the pins
    # ------------------------------------------------------------------

    def _draw_prism(
        self,
        pkg: _PkgPrism,
        positions: list[Point],
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
            return self._draw_smd_chip(pkg, positions)

        # The silhouette comes from ``_silhouette`` — the column guess here, a
        # declared look in the kind renderer subclasses, suppressed entirely in
        # the class-dispatched :class:`DevboardRenderer`.
        sil = self._silhouette(pkg)
        if sil is None:
            return None
        # An appearance corner-radius override (mm, per corner) reaches the
        # DIP/SIL body via the silhouette builder, converted to holes; absent →
        # the builder keeps using theme.body_corner_radius (the notch radius is
        # unaffected either way).
        corners = self._appearance.corner_radius_mm_corners
        theme = self.paint.theme
        mm = theme.raster_pitch_mm
        corner_holes: tuple[float, float, float, float] | None = (
            (corners[0] / mm, corners[1] / mm, corners[2] / mm, corners[3] / mm)
            if corners is not None
            else None
        )
        drawing = sil.build(pkg, theme, corner_r=corner_holes)

        # Draw on a canvas in the package-local frame (pin 1 at the origin),
        # placed through the SAME local→world transform the pins use in
        # pin_world_position: reflect about the package flip axis on the
        # bottom side, rotate, then translate to the anchor.  Reconstructing
        # orientation from the world pins alone can't recover the reflection's
        # chirality, which is why the bottom-side DIP body used to drift to
        # pin 1's old corner.
        canvas = self.body_canvas(positions)
        canvas.addPath(drawing.body)
        if drawing.marker is not None:
            canvas.addPath(drawing.marker, fill=self.paint.border,
                           pen_width=0.0, z_offset=0.01)
        return canvas.items

    # ------------------------------------------------------------------
    # SMD chip — flat two-terminal body centred on the pad pair
    # ------------------------------------------------------------------

    def _draw_smd_chip(
        self,
        pkg: _PkgPrism,
        positions: list[Point],
    ) -> list[QGraphicsItem]:
        """A surface-mount chip (0805 / 0603): a flat rounded rectangle centred
        on the two pads and aligned to the pad axis.

        The chip's two terminals end on the pads, so the body bridges them: its
        length spans the pad gap plus a small overhang (the terminals tuck just
        under the chip ends), its width is the package body width.  Aligning to
        the pad axis makes ortho and diagonal placements both render correctly.
        No leads and no pin-1 marker — a chip carries neither.
        """
        from protoboard.domain.components.package.shapes import (
            local_size_holes_or_none,
        )
        s = local_size_holes_or_none(pkg)  # local frame: lx along the pads
        if s is None:
            # No explicit dims/margin to size the chip — degrade to the bbox
            # fallback, which handles the same case with a generic body.
            return self._draw_bbox(positions)
        body_len, body_w = s.lx, s.ly

        p1, p2 = positions[0], positions[1]
        axis = p2 - p1  # Point − Point → Vector
        dist = axis.norm()
        if dist < 1e-9:
            return self._draw_bbox(positions)

        # The chip is drawn at its declared body length, centred on the pad
        # midpoint — a real SMD chip is shorter than the pad pitch and sits
        # between the pads, leaving the metallised pads visible at each end.
        # (It is *not* stretched to span the pad gap, which read as a long thin
        # rectangle.)
        canvas = self.axis_canvas(positions[:2], p1, p2)
        tl, tr, br, bl = self._effective_corner_radii_mm()
        canvas.addRect(
            (-body_len / 2.0, -body_w / 2.0),
            (body_len / 2.0, body_w / 2.0),
            round=(canvas.mm(tl), canvas.mm(tr), canvas.mm(br), canvas.mm(bl)),
        )
        return canvas.items


@register_renderer(component_class=ComponentClass.DEVBOARD)
class DevboardRenderer(PrismRenderer):
    """Dev-board module: a plain PCB rectangle plus faint per-pin pad rings.

    Registered on the **component-class axis** — any package a devboard
    carries renders plain (no DIP notch / SIL housing, however its pins
    cluster into columns), because the trait belongs to the part's class, not
    the body lineage.  This registration replaces the historical hardcoded
    ``component_class == DEVBOARD`` gates in ``PrismRenderer._draw_prism`` and
    ``PackageRenderer._draw_bbox``.
    """

    def _silhouette(self, pkg: _PkgPrism) -> PrismSilhouette | None:
        del pkg
        return None

    def _draw_bbox(self, positions: list[Point]) -> list[QGraphicsItem]:
        # A dev-board's pins sit on the PCB's top surface — mark each with a
        # faint pad-sized ring (the click-active area) drawn on top of the
        # body.  The real pin glyph / modifier is drawn by the component
        # renderer on a higher layer, so these rings are additive.
        items = super()._draw_bbox(positions)
        items.extend(self._pad_active_rings(positions))
        return items
