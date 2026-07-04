# SPDX-License-Identifier: MIT
"""Renderer base classes, shared paint helpers, and the dispatch registry.

``PackageRenderer`` carries everything shared across body shapes: colour /
appearance resolution, the canonical local→world placement, the radial
(top-view) drawing machinery used by every shape, and the bbox fallback.
Concrete shapes override :meth:`PackageRenderer.render_body`; the radial
top-view branch is handled once in :meth:`PackageRenderer.draw` (it is a
property of the lead arrangement, not of one shape).

Registration is declarative: a subclass that sets ``renders = "<name>"`` is
entered into ``BODY_RENDERERS`` the moment the class body executes — dropping
a plugin file into a library's ``renderers/`` directory is all it takes (see
:mod:`.discovery`).  Dispatch (:func:`renderer_for`) walks the package's
``lineage`` leaf-first, so the most specific renderer wins and unknown names
degrade towards the per-shape base renderer — rendering never blocks on an
unknown package.
"""

from __future__ import annotations

import math
from abc import ABC, abstractmethod
from typing import ClassVar

import structlog
from PySide6.QtCore import QRectF, Qt
from PySide6.QtGui import QBrush, QColor, QPen, QTransform
from PySide6.QtWidgets import (
    QGraphicsEllipseItem,
    QGraphicsItem,
    QGraphicsLineItem,
    QGraphicsPathItem,
    QGraphicsRectItem,
)

from protoboard.domain.components.appearance import Appearance
from protoboard.domain.components.definition import ComponentClass
from protoboard.domain.components.package.body import (
    EdgeStyle,
    LeadArrangement,
)
from protoboard.domain.components.package.shapes import (
    Cylinder as _PkgCylinder,
)
from protoboard.domain.components.package.shapes import (
    Disc as _PkgDisc,
)
from protoboard.domain.components.package.shapes import (
    Package,
)
from protoboard.domain.components.package.shapes import (
    Prism as _PkgPrism,
)
from protoboard.domain.layout.placement import ComponentPlacement
from protoboard.geometry import Point, Transform2D
from protoboard.gui.renderers.packages.canvas import (
    BodyCanvas,
    _body_pen,
    qcolor_from_css,
)
from protoboard.gui.renderers.prism_silhouette import rounded_rect_path
from protoboard.gui.theme import DEFAULT_THEME, Theme

__all__ = [
    "BodyCanvas",
    "BodyShape",
    "GenericBody",
    "PackageRenderer",
    "qcolor_from_css",
    "renderer_for",
]

_log = structlog.get_logger(__name__)


def _qtransform(t: Transform2D) -> QTransform:
    """Bridge a domain :class:`Transform2D` to a Qt ``QTransform``.

    Qt's constructor takes column-major ``(m11, m12, m21, m22, dx, dy)`` mapping
    ``x' = m11·x + m21·y + dx``, ``y' = m12·x + m22·y + dy``.  Our coefficients
    ``(a, b, c, d, tx, ty)`` map ``x' = a·x + b·y + tx``, ``y' = c·x + d·y + ty``
    — the transpose of the linear part, so the arguments interleave as below.
    """
    a, b, c, d, tx, ty = t.coeffs()
    return QTransform(a, c, b, d, tx, ty)


def _lead_pen(theme: Theme, alpha: float) -> QPen:
    c = QColor(theme.lead_color)
    c.setAlphaF(alpha)
    return QPen(c, theme.lead_width)


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


# ---------------------------------------------------------------------------
# Base class (interface shared with the legacy hand-coded bodies)
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
# GenericBody — bare bbox rectangle (no package resolved)
# ---------------------------------------------------------------------------

class GenericBody(BodyShape):
    """Bare-bbox fallback body for a component with no resolved package.

    Honours the visual :class:`Appearance` override the same way
    ``PackageRenderer`` does: an override colour replaces the caller-supplied
    fill (border derived by darkening), an override corner radius rounds the
    bbox rectangle per corner. Without an override the historical plain
    rectangle is drawn unchanged.
    """

    def __init__(self, appearance: Appearance = Appearance()) -> None:
        self._appearance = appearance

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
        override = self._appearance.color
        if override is not None:
            override_fill = qcolor_from_css(override)
            if override_fill.isValid():
                fill, border = override_fill, _darken(override_fill)
        margin = 0.35
        xs = [p.x for p in positions]
        ys = [p.y for p in positions]
        x0 = min(xs) - margin
        y0 = min(ys) - margin
        w = max(xs) - min(xs) + 2 * margin
        h = max(ys) - min(ys) + 2 * margin
        corners = self._appearance.corner_radius_mm_corners
        if corners is not None and any(corners):
            mm = theme.raster_pitch_mm
            tl, tr, br, bl = corners
            path_item = QGraphicsPathItem(rounded_rect_path(
                QRectF(x0, y0, w, h),
                (tl / mm, tr / mm, br / mm, bl / mm),
            ))
            path_item.setBrush(QBrush(fill))
            path_item.setPen(_body_pen(border, alpha))
            path_item.setZValue(z)
            return [path_item]
        rect = QGraphicsRectItem(QRectF(x0, y0, w, h))
        rect.setBrush(QBrush(fill))
        rect.setPen(_body_pen(border, alpha))
        rect.setZValue(z)
        return [rect]


# ---------------------------------------------------------------------------
# Registry
# ---------------------------------------------------------------------------

# lineage name → renderer class. Populated declaratively by
# ``PackageRenderer.__init_subclass__``; later registrations win (a user
# plugin overrides a builtin, mirroring the library-loader precedence).
BODY_RENDERERS: dict[str, type[PackageRenderer]] = {}


class PackageRenderer(BodyShape):
    """Render a component body from its :class:`Package` geometry.

    Presentation (colour, edge) comes from the package; polarity and which
    pad is the anchor are *not* package properties — a package is class
    agnostic (the same DO-41 hosts a diode, a Zener or a fuse), so the
    polarity marker is driven by ``component_class`` and ``vertical_flipped``.

    Subclasses override :meth:`render_body` (and register via ``renders``);
    the radial top-view branch and the bbox fallback live here because they
    apply to every shape.
    """

    #: Lineage name(s) this class renders. Declaring it on a subclass
    #: registers the class in ``BODY_RENDERERS`` at class-creation time.
    #: The attribute is intentionally looked up on ``cls.__dict__`` only —
    #: a subclass that does NOT declare its own ``renders`` inherits its
    #: parent's *behaviour* without stealing its registry slot.
    renders: ClassVar[str | tuple[str, ...] | None] = None

    # ComponentClasses that show a polarity marker — diodes get a cathode
    # stripe (axial) or a solid-vs-hollow disc (radial); polar electrolytics
    # share the treatment.
    _POLAR_CLASSES = frozenset({
        ComponentClass.DIODE,
        ComponentClass.DIODE_ZENER,
        ComponentClass.CAPACITOR_POLAR,
    })

    def __new__(
        cls,
        package: Package,
        *args: object,
        **kwargs: object,
    ) -> PackageRenderer:
        # Constructing the base class dispatches to the most specific
        # registered subclass for the package's lineage — ``PackageRenderer(
        # pkg)`` (the historical ``PackageBody(pkg)``) transparently yields a
        # ``CylinderRenderer`` / ``PrismRenderer`` / plugin instance.
        # Constructing a concrete subclass directly bypasses the dispatch.
        del args, kwargs  # consumed by __init__
        target = renderer_for(package) if cls is PackageRenderer else cls
        instance: PackageRenderer = object.__new__(target)
        return instance

    def __init_subclass__(cls, **kwargs: object) -> None:
        super().__init_subclass__(**kwargs)
        declared = cls.__dict__.get("renders")
        if declared is None:
            return
        keys = (declared,) if isinstance(declared, str) else tuple(declared)
        for key in keys:
            prev = BODY_RENDERERS.get(key)
            if prev is not None and prev is not cls:
                _log.info(
                    "body renderer overridden",
                    key=key,
                    old=f"{prev.__module__}.{prev.__qualname__}",
                    new=f"{cls.__module__}.{cls.__qualname__}",
                )
            BODY_RENDERERS[key] = cls

    def __init__(
        self,
        package: Package,
        component_class: ComponentClass = ComponentClass.OTHER,
        vertical_flipped: bool = False,
        appearance: Appearance = Appearance(),
        side: str = "top",
        placement: ComponentPlacement | None = None,
    ) -> None:
        self._pkg = package
        self._component_class = component_class
        self._vertical_flipped = vertical_flipped
        self._appearance = appearance
        # Board side of the placement. On the bottom the body is physically
        # flipped, so an asymmetric silhouette (a TO-92 flat face, a TO-220 tab)
        # faces the opposite way — the flat side's normal is mirrored across the
        # lead axis. See _flat_side_world.
        self._side = side
        self._placement = placement

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

        Precedence: the per-component / per-instance ``appearance`` colour wins;
        then the package's own ``color`` (the border is derived by darkening the
        fill); otherwise the caller-supplied colours are used unchanged.
        """
        override = self._appearance.color
        if override is not None:
            override_fill = qcolor_from_css(override)
            if override_fill.isValid():
                return override_fill, _darken(override_fill)
        pkg_color = getattr(self._pkg, "color", None)
        if pkg_color is None:
            return fill, border
        body_fill = qcolor_from_css(pkg_color)
        if not body_fill.isValid():
            return fill, border
        return body_fill, _darken(body_fill)

    def _effective_corner_radii_mm(self) -> tuple[float, float, float, float]:
        """Per-corner radii (mm), (top-left, top-right, bottom-right,
        bottom-left): the appearance override wins (0 = sharp, > 0 = rounded,
        CSS ``border-radius`` expansion), else the package's own edge/radius
        via ``_package_corner_radius`` replicated to all four corners."""
        corners = self._appearance.corner_radius_mm_corners
        if corners is not None:
            return corners
        r = _package_corner_radius(self._pkg)
        return (r, r, r, r)

    # ------------------------------------------------------------------
    # Template: radial branch here, shape-specific body in render_body()
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
        if not positions:
            return []
        fill, border = self._resolve_colors(fill, border)
        return self.render_body(positions, fill, border, alpha, z, theme)

    def render_body(
        self,
        positions: list[Point],
        fill: QColor,
        border: QColor,
        alpha: float,
        z: float,
        theme: Theme,
    ) -> list[QGraphicsItem]:
        """The body silhouette — THE override point for renderer subclasses.

        A subclass adds geometry by extending the base drawing::

            def render_body(self, positions, fill, border, alpha, z, theme):
                items = super().render_body(positions, fill, border, alpha, z, theme)
                items.append(...)   # e.g. the TO-220 metal tab
                return items

        The default draws the radial top-view when the arrangement calls for
        it, else the bbox rectangle.  Shape subclasses keep the same radial
        precedence and insert their side-view drawing between the two.
        """
        radial = self._maybe_radial(positions, fill, border, alpha, z, theme)
        if radial is not None:
            return radial
        return self._draw_bbox(positions, fill, border, alpha, z, theme)

    def _maybe_radial(
        self,
        positions: list[Point],
        fill: QColor,
        border: QColor,
        alpha: float,
        z: float,
        theme: Theme,
    ) -> list[QGraphicsItem] | None:
        """The radial top-view branch, or ``None`` when it does not apply.

        Radial 2-pin: top-view silhouette on the anchor pad (vertical_flipped
        swaps which pad anchors).  Everything else is a side view drawn by the
        shape subclass.
        """
        if self._arrangement is not LeadArrangement.RADIAL or len(positions) < 2:
            return None
        if self._vertical_flipped:
            p1, p2 = positions[1], positions[0]
        else:
            p1, p2 = positions[0], positions[1]
        if math.hypot(p2.x - p1.x, p2.y - p1.y) < 1e-9:
            return None
        # The ``-`` marker sits towards the physical cathode (pin 2),
        # independent of the p1/p2 swap above.
        neg_pin = positions[1]
        return self._draw_radial(
            p1, p2, neg_pin, fill, border, alpha, z, theme,
            pads=positions,
        )

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
        """Half-extent (in holes) of the radial body across the lead axis.

        The default is half the bbox length along the lead axis (the prism /
        generic silhouette); round shapes override with their radius.
        """
        del mm  # used by the round-shape overrides (mm → holes conversion)
        from protoboard.domain.components.package.shapes import (
            lead_axis_extents,
        )
        along, _across = lead_axis_extents(self._pkg)
        return along / 2.0

    def _body_placement(self, positions: list[Point]) -> QTransform:
        """Local→world placement for the silhouette (pin 1 at the local origin).

        With a placement, this is the canonical ``placement_transform`` — the
        exact pipeline the pins go through, so the body follows the pins on both
        sides (the bottom-side reflection included).  Without one (a bare
        ``draw`` in tests / previews), reconstruct the top-side placement from
        the pin heading: translate to pin 1, rotate by the pin1→pin2 heading.
        """
        if self._placement is not None:
            from protoboard.domain.components.package.adapter import (
                PackageFootprint,
            )
            from protoboard.domain.layout.placement import placement_transform
            return _qtransform(
                placement_transform(PackageFootprint(self._pkg), self._placement)
            )
        p1 = positions[0]
        dx = positions[1].x - p1.x if len(positions) > 1 else 0.0
        dy = positions[1].y - p1.y if len(positions) > 1 else 0.0
        angle_deg = -math.degrees(math.atan2(dx, dy)) if (dx or dy) else 0.0
        place = QTransform()
        place.translate(p1.x, p1.y)
        place.rotate(angle_deg)
        return place

    # ------------------------------------------------------------------
    # Canvas factories — the drawing API for subclasses and plugins
    # ------------------------------------------------------------------

    def body_canvas(
        self,
        positions: list[Point],
        fill: QColor,
        border: QColor,
        alpha: float,
        z: float,
        theme: Theme,
    ) -> BodyCanvas:
        """A :class:`BodyCanvas` in the package-local frame (pin 1 at the
        origin), mapped through the SAME local→world transform as the pins —
        rotation and the bottom-side reflection included
        (:meth:`_body_placement`). The transform is baked into the item paths
        (the historical ``place.map`` convention of this frame)."""
        return BodyCanvas(theme, fill=fill, border=border, alpha=alpha, z=z,
                          transform=self._body_placement(positions), bake=True)

    def axis_canvas(
        self,
        positions: list[Point],
        p1: Point,
        p2: Point,
        fill: QColor,
        border: QColor,
        alpha: float,
        z: float,
        theme: Theme,
    ) -> BodyCanvas:
        """A :class:`BodyCanvas` in the pad-axis frame: origin at the pad
        centroid, local +x along the *p1*→*p2* lead axis — the frame the
        radial / SMD-chip bodies (and the TO-220 tab) draw in."""
        mid_x = sum(p.x for p in positions) / len(positions)
        mid_y = sum(p.y for p in positions) / len(positions)
        t = QTransform()
        t.translate(mid_x, mid_y)
        t.rotate((p2 - p1).angle_deg())
        return BodyCanvas(theme, fill=fill, border=border, alpha=alpha, z=z,
                          transform=t)

    # ------------------------------------------------------------------
    # Radial — top-view silhouette centred between the pads (all shapes)
    # ------------------------------------------------------------------

    def _draw_radial(
        self,
        p1: Point, p2: Point, neg_pin: Point,
        fill: QColor, border: QColor, alpha: float, z: float,
        theme: Theme,
        *,
        pads: list[Point] | None = None,
    ) -> list[QGraphicsItem]:
        # The body sits over every pad (a 3-lead TO-92 is centred on its three
        # pins, not just the first two); a lead stub is drawn to each.  ``pads``
        # defaults to the anchor pair for the legacy two-pin path.
        all_pads = pads if pads is not None else [p1, p2]
        items: list[QGraphicsItem] = []
        for a, b in zip(all_pads, all_pads[1:]):
            lead = QGraphicsLineItem(a.x, a.y, b.x, b.y)
            lead.setPen(_lead_pen(theme, alpha))
            lead.setZValue(z - 0.01)
            items.append(lead)

        # Centroid of all pads — the body's geometric centre.
        mid = Point(
            sum(p.x for p in all_pads) / len(all_pads),
            sum(p.y for p in all_pads) / len(all_pads),
        )
        items.extend(self._radial_silhouette(
            mid, p1, p2, neg_pin, fill, border, alpha, z, theme,
        ))
        return items

    def _radial_silhouette(
        self,
        mid: Point, p1: Point, p2: Point, neg_pin: Point,
        fill: QColor, border: QColor, alpha: float, z: float,
        theme: Theme,
    ) -> list[QGraphicsItem]:
        """Top-view body silhouette centred on ``mid`` — the per-shape hook.

        The default draws the package bbox as a rounded rectangle aligned with
        the lead direction (the prism / generic look).  Round cross-sections
        (cylinder disc, disc lens) override in their shape renderer; ``neg_pin``
        lets a polar override place the ``-`` marker.
        """
        del neg_pin  # only round polar silhouettes mark the negative lead
        from protoboard.domain.components.package.shapes import (
            lead_axis_extents,
        )
        length_r, width_r = lead_axis_extents(self._pkg)
        t = QTransform()
        t.translate(mid.x, mid.y)
        t.rotate((p2 - p1).angle_deg())
        canvas = BodyCanvas(theme, fill=fill, border=border, alpha=alpha, z=z,
                            transform=t)
        tl, tr, br, bl = self._effective_corner_radii_mm()
        canvas.addRect(
            (-length_r / 2.0, -width_r / 2.0),
            (length_r / 2.0, width_r / 2.0),
            round=(canvas.mm(tl), canvas.mm(tr), canvas.mm(br), canvas.mm(bl)),
        )
        return canvas.items

    # ------------------------------------------------------------------
    # Generic — bbox() rectangle in holes (fallback for every shape)
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
            # a rectangle hugging the pin cloud (same as GenericBody); the
            # appearance rides along so the radius override still applies
            # (fill/border are already resolved by draw()).
            generic: list[QGraphicsItem] = GenericBody(self._appearance).draw(
                positions, fill, border, alpha, z, theme,
            )
            return generic

        # bbox() is in the package-local frame (pin 1 at the origin); its x/y is
        # the body's top-left there (negative for a body centred on the pin
        # cloud).  Build the rectangle in that LOCAL frame and place it through
        # the SAME local→world transform the pins use (rotation + bottom-side
        # reflection): reconstructing the rect in world coords and only shifting
        # it by the anchor draws an axis-aligned box that ignores the placement
        # rotation — the body then stays upright while the pins rotate.
        canvas = self.body_canvas(positions, fill, border, alpha, z, theme)
        tl, tr, br, bl = self._effective_corner_radii_mm()
        canvas.addRect(
            (box.x, box.y),
            (box.x + box.width, box.y + box.height),
            round=(canvas.mm(tl), canvas.mm(tr), canvas.mm(br), canvas.mm(bl)),
        )
        items: list[QGraphicsItem] = canvas.items
        # A dev-board's pins sit on the PCB's top surface — mark each with a
        # faint pad-sized ring (the click-active area) drawn on top of the body.
        # The real pin glyph / modifier is drawn by the component renderer on a
        # higher layer, so these rings are additive and not claimed.
        if self._component_class is ComponentClass.DEVBOARD:
            items.extend(self._pad_active_rings(positions, border, alpha, z, theme))
        return items

    def _pad_active_rings(
        self,
        positions: list[Point],
        border: QColor,
        alpha: float,
        z: float,
        theme: Theme,
    ) -> list[QGraphicsItem]:
        """A faint pad-sized ring per pin, marking the click-active area."""
        pad_r = theme.pad_radius
        ring_c = QColor(border)
        ring_c.setAlphaF(ring_c.alphaF() * alpha * 0.35)
        pen = QPen(ring_c, theme.pad_outline_width)
        rings: list[QGraphicsItem] = []
        for p in positions:
            ring = QGraphicsEllipseItem(
                QRectF(p.x - pad_r, p.y - pad_r, 2 * pad_r, 2 * pad_r)
            )
            ring.setPen(pen)
            ring.setBrush(QBrush(Qt.BrushStyle.NoBrush))
            ring.setZValue(z + 0.02)
            rings.append(ring)
        return rings


def renderer_for(pkg: Package) -> type[PackageRenderer]:
    """The most specific registered renderer for *pkg*.

    Walks the package ``lineage`` leaf-first (variant → family → extends chain
    → shape root); an unknown name simply steps outward, so rendering never
    blocks on a package the registry does not know.  Packages without a
    lineage (hand-built in tests) fall back to their shape root.
    """
    for name in reversed(getattr(pkg, "lineage", ()) or ()):
        renderer = BODY_RENDERERS.get(name)
        if renderer is not None:
            return renderer
    if isinstance(pkg, _PkgCylinder):
        return BODY_RENDERERS["cylinder"]
    if isinstance(pkg, _PkgDisc):
        return BODY_RENDERERS["disc"]
    return BODY_RENDERERS["prism"]
