# SPDX-License-Identifier: MIT
"""TO-220 renderer plugin — the radial prism body plus the metal mounting tab.

This file doubles as the reference for user renderer plugins: it lives in the
builtin library's ``renderers/`` directory and is loaded by the exact same
discovery mechanism as user plugins (``load_renderer_plugins``).
``@register_renderer(family="TO-220")`` registers the class for the whole
TO-220 package family (the family id sits on the package ``lineage`` just
above the variant), so every variant gets the tab unless a more specific
renderer claims its leaf.  Other axes: ``kind=`` (a package kind such as
``dip_ic``), ``package_name=`` (a generic such as ``prism_smd``),
``shape_name=`` (a shape root) and ``component_class=`` (dispatch on the
component's class — wins over every lineage entry).

The base ``PrismRenderer`` draws the radial rectangle (the plastic body,
centred on the pad centroid, aligned to the lead axis); this subclass appends
the characteristic light metal tab strip along the body's back edge — the
behaviour the legacy hand-coded ``TO220Body`` had, now restored as a plugin
(docs/todo.md "TO-220 metal tab render").

A plugin extends the base drawing through the :class:`BodyCanvas` API:
``axis_canvas`` gives a canvas in the pad-axis frame (origin at the pad
centroid, +x along the lead axis); ``add<Shape>`` calls handle path / brush /
pen / alpha / z / transform in one go, and ``zAbove()`` stacks the detail just
above what the base drew.
"""

from __future__ import annotations

from PySide6.QtWidgets import QGraphicsItem

from protoboard.domain.components.package.shapes import (
    Prism,
    lead_axis_extents,
)
from protoboard.geometry import Point
from protoboard.gui.renderers.packages import PrismRenderer, register_renderer


@register_renderer(family="TO-220")
class TO220Renderer(PrismRenderer):
    """TO-220 (and friends): the prism body + a metal tab on the back edge."""

    #: Light metal tab colour (the exposed heatsink flange).
    TAB_COLOR = "#dddddd"

    def render_body(self, positions: list[Point]) -> list[QGraphicsItem]:
        items = super().render_body(positions)

        # Geometry mirrors the radial-rectangle frame the base class used: the
        # frame origin is the pad centroid, local +x runs along the lead axis
        # (pin 1 → last pin), so the body spans ``length × width`` around it.
        # The tab is a ``length × tab_h`` strip attached to the −y long edge —
        # flipping the part (``vertical_flipped`` reverses the pad axis) turns
        # the tab to the other side, matching the physical package flip.
        pkg = self._pkg
        if not isinstance(pkg, Prism) or len(positions) < 2:
            return items
        if self._vertical_flipped:
            p1, p2 = positions[1], positions[0]
        else:
            p1, p2 = positions[0], positions[1]
        if (p2 - p1).length() < 1e-9:
            return items

        # (along, across) the lead axis — the same split the base radial
        # rectangle draws with, so the tab hugs the body exactly.  Raw
        # ``bbox().width/height`` must NOT be read as across/along: the bbox
        # is oriented by the dominant pin-cloud axis, which for TO-220's
        # x-running pin row is the transpose of the historical along=y
        # convention (the tab used to float outside the body, half its length).
        body_len, body_w = lead_axis_extents(pkg)

        theme = self.paint.theme
        canvas = self.axis_canvas(positions, p1, p2)
        canvas.track(items)
        canvas.setFillColor(self.TAB_COLOR)
        tab_h = canvas.mm(theme.to220_tab_thickness_mm)
        # Just above the plastic body so the strip reads as a separate part.
        canvas.addRect(
            (-body_len / 2.0, -body_w / 2.0 + tab_h),
            (body_len / 2.0, -body_w / 2.0),
            round=canvas.mm(0.1),
            pen_width=theme.body_tab_pen_width,
            z=canvas.zAbove(),
        )
        items.extend(canvas.items)
        return items
