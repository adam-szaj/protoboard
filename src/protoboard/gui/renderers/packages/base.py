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
from PySide6.QtGui import QBrush, QColor, QPainterPath, QPen, QTransform
from PySide6.QtWidgets import (
    QAbstractGraphicsShapeItem,
    QGraphicsEllipseItem,
    QGraphicsItem,
    QGraphicsLineItem,
    QGraphicsPathItem,
    QGraphicsRectItem,
)

from protoboard.domain.components.appearance import Appearance
from protoboard.domain.components.definition import ComponentClass
from protoboard.domain.components.package.body import (
    DiscProfile,
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
from protoboard.geometry import Point, Transform2D, Vector
from protoboard.gui.theme import DEFAULT_THEME, Theme

_log = structlog.get_logger(__name__)

_big_body_alpha: float = 0.90


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


def _body_pen(border: QColor, alpha: float, width: float = 0.06) -> QPen:
    c = QColor(border)
    c.setAlphaF(alpha)
    return QPen(c, width)


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
        ComponentClass.DIODE_LED,
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
            override_fill = QColor(override)
            if override_fill.isValid():
                return override_fill, _darken(override_fill)
        pkg_color = getattr(self._pkg, "color", None)
        if pkg_color is None:
            return fill, border
        body_fill = QColor(pkg_color)
        if not body_fill.isValid():
            return fill, border
        return body_fill, _darken(body_fill)

    def _effective_corner_radius_mm(self) -> float:
        """Corner radius (mm): the appearance override wins (0 = sharp, > 0 =
        rounded), else the package's own edge/radius via ``_package_corner_radius``.

        Only the first radius is used (per-corner render is deferred); the full
        tuple is preserved on the appearance value object."""
        scalar = self._appearance.corner_radius_mm_scalar
        if scalar is not None:
            return scalar
        return _package_corner_radius(self._pkg)

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
        # Radial 2-pin: top-view silhouette on the anchor pad (vertical_flipped
        # swaps which pad anchors).  Everything else is a side view.
        if self._arrangement is LeadArrangement.RADIAL and len(positions) >= 2:
            if self._vertical_flipped:
                p1, p2 = positions[1], positions[0]
            else:
                p1, p2 = positions[0], positions[1]
            if math.hypot(p2.x - p1.x, p2.y - p1.y) >= 1e-9:
                # The ``-`` marker sits towards the physical cathode (pin 2),
                # independent of the p1/p2 swap above.
                neg_pin = positions[1]
                return self._draw_radial(
                    p1, p2, neg_pin, fill, border, alpha, z, theme,
                    pads=positions,
                )
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
        """The shape-specific (non-radial) body. Default: the bbox rectangle."""
        return self._draw_bbox(positions, fill, border, alpha, z, theme)

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
        """Half-extent (in holes) of the radial body across the lead axis."""
        pkg = self._pkg
        if isinstance(pkg, (_PkgCylinder, _PkgDisc)):
            return pkg.diameter_mm / mm / 2.0
        # Prism: half its length (the bbox dimension along the lead axis).
        return pkg.bbox().height / 2.0

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

        mm = theme.raster_pitch_mm
        # Centroid of all pads — the body's geometric centre.
        mid = Point(
            sum(p.x for p in all_pads) / len(all_pads),
            sum(p.y for p in all_pads) / len(all_pads),
        )
        # Direction from the body centre towards the negative lead — the side
        # the ``-`` segment marker goes on.
        neg_angle = (neg_pin - mid).angle_deg()

        pkg = self._pkg
        if isinstance(pkg, _PkgDisc):
            # A disc cap mounts on its edge: from above it is a thin side
            # profile, not the round face — a lens (its diameter along the lead
            # axis, its thickness across).
            axis_angle = (p2 - p1).angle_deg()
            items.append(self._radial_disc_side(
                mid, pkg.diameter_mm / mm, pkg.thickness_mm / mm, axis_angle,
                pkg.profile, fill, border, alpha, z, theme,
            ))
            return items
        if isinstance(pkg, _PkgCylinder):
            diam = pkg.diameter_mm / mm
            world_axis_deg = (p2 - p1).angle_deg()
            flat = self._flat_side_world(pkg, world_axis_deg)
            items.extend(self._radial_disc(
                mid, diam, neg_angle, fill, border, alpha, z, theme,
                flat=flat,
            ))
            return items

        # Prism radial → rectangle aligned with the lead direction, centred on
        # the midpoint between the pads.
        bb = pkg.bbox()
        length_r, width_r = bb.height, bb.width
        angle_deg = (p2 - p1).angle_deg()
        rect_path = QPainterPath()
        r = self._effective_corner_radius_mm() / mm
        rect_path.addRoundedRect(
            QRectF(-length_r / 2.0, -width_r / 2.0, length_r, width_r), r, r,
        )
        body = QGraphicsPathItem(rect_path)
        fill_c = QColor(fill)
        fill_c.setAlphaF(fill_c.alphaF() * alpha)
        body.setBrush(QBrush(fill_c))
        body.setPen(_body_pen(border, alpha))
        body.setZValue(z)
        t = QTransform()
        t.translate(mid.x, mid.y)
        t.rotate(angle_deg)
        body.setTransform(t)
        items.append(body)
        return items

    def _flat_side_world(
        self, pkg: _PkgCylinder, world_axis_deg: float,
    ) -> tuple[float, float] | None:
        """The flat side as ``(normal_angle_deg, offset_holes)`` in world space.

        ``None`` for a full circle (no flat side). ``flat_normal`` is given in
        the package's *local* frame (the same frame the lead offsets use), so we
        rotate it by the local→world rotation. That rotation is the difference
        between the world pad axis (``world_axis_deg``) and the local pad axis
        derived from ``pkg.pin_positions()`` — the pads carry the part's
        rotation/flip, so the flat side follows the body automatically.
        """
        if pkg.flat_radius_mm is None:
            return None
        local = pkg.pin_positions()
        if len(local) < 2:
            return None
        axis = local[-1] - local[0]  # Point − Point → Vector
        if axis.length() < 1e-9:
            return None
        local_axis_deg = axis.angle_deg()
        rot = math.radians(world_axis_deg - local_axis_deg)
        n = pkg.flat_normal
        # On the bottom side the body is physically flipped: reflect the flat
        # normal about the package flip axis (the same reflection the pins
        # undergo in pin_world_position), so the flat face turns to the correct
        # side.  Applied as a direction (the linear part; translation ignored).
        if self._side == "bottom":
            n = pkg.flip_transform().apply(n)
        nx = n.x * math.cos(rot) - n.y * math.sin(rot)
        ny = n.x * math.sin(rot) + n.y * math.cos(rot)
        normal_deg = Vector(nx, ny).angle_deg()
        return normal_deg, pkg.flat_radius_mm / DEFAULT_THEME.raster_pitch_mm

    def _radial_disc(
        self,
        centre: Point,
        diam: float,
        neg_angle_deg: float,
        fill: QColor, border: QColor, alpha: float, z: float,
        theme: Theme,
        *,
        flat: tuple[float, float] | None = None,
    ) -> list[QGraphicsItem]:
        """Top-view (looking at the body face) disc centred between the pads.

        Non-polar parts draw a plain filled circle. For polar parts the body
        face shows no end-cap marker (both leads exit the same face), so the
        negative terminal is marked the way a real radial electrolytic is: a
        dark circular segment (a chord-cut slice of the disc) on the side of the
        ``-`` lead, leaving the rest of the disc in the body colour.

        ``flat`` (``(normal_angle_deg, offset_holes)``) cuts a chord off the
        circle — a D-shaped cross-section such as a TO-92 — leaving the body a
        full circle when ``None``.
        """
        r = diam / 2.0
        fill_c = QColor(fill)
        fill_c.setAlphaF(fill_c.alphaF() * alpha)
        body: QAbstractGraphicsShapeItem
        if flat is not None:
            body = self._d_shape(centre, r, flat[0], flat[1])
        else:
            body = QGraphicsEllipseItem(
                QRectF(centre.x - r, centre.y - r, diam, diam),
            )
        body.setZValue(z)
        body.setBrush(QBrush(fill_c))
        body.setPen(_body_pen(border, alpha))
        if not self._is_polar:
            return [body]
        return [body, self._disc_segment(centre, r, neg_angle_deg, theme, alpha, z)]

    def _d_shape(
        self, centre: Point, radius: float, normal_deg: float, offset: float,
    ) -> QGraphicsPathItem:
        """A circle with one chord cut off, normal to ``normal_deg`` at distance
        ``offset`` (holes) from the centre. The remaining arc spans the far side;
        the chord is the flat face."""
        # Half-angle subtended by the chord: the chord sits at +offset along the
        # flat normal, so the arc we KEEP runs the long way around the far side.
        c = max(-1.0, min(1.0, offset / radius))
        half = math.degrees(math.acos(c))
        rect = QRectF(-radius, -radius, 2 * radius, 2 * radius)
        path = QPainterPath()
        # Arc from +half to (360 − half) around the back, then the chord closes
        # it. Angles are in the local frame where the flat normal points +x;
        # Qt arc angles are counter-clockwise from +x.
        path.arcMoveTo(rect, half)
        path.arcTo(rect, half, 360.0 - 2.0 * half)
        path.closeSubpath()
        item = QGraphicsPathItem(path)
        t = QTransform()
        t.translate(centre.x, centre.y)
        t.rotate(normal_deg)
        item.setTransform(t)
        return item

    def _radial_disc_side(
        self,
        centre: Point,
        length: float,   # the disc diameter, along the lead axis
        width: float,    # the disc thickness, across the lead axis
        axis_angle_deg: float,
        profile: DiscProfile,
        fill: QColor, border: QColor, alpha: float, z: float,
        theme: Theme,
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

    def _disc_segment(
        self,
        centre: Point,
        radius: float,
        neg_angle_deg: float,
        theme: Theme,
        alpha: float,
        z: float,
    ) -> QGraphicsItem:
        """A dark circular segment cut by a chord perpendicular to the lead
        axis, on the negative-lead side — the radial ``-`` polarity marker."""
        # Chord at 0.45·radius from the centre → the slice is ~the outer third
        # of the disc, on the side ``neg_angle_deg`` points to (local +x after
        # the rotation below).
        d = 0.45 * radius
        half = math.degrees(math.acos(max(-1.0, min(1.0, d / radius))))
        rect = QRectF(-radius, -radius, 2 * radius, 2 * radius)
        path = QPainterPath()
        path.arcMoveTo(rect, -half)
        path.arcTo(rect, -half, 2 * half)
        path.closeSubpath()
        seg = QGraphicsPathItem(path)
        dark = QColor(theme.body_diode_cathode_color)
        dark.setAlphaF(alpha)
        seg.setBrush(QBrush(dark))
        seg.setPen(_body_pen(dark, alpha, width=0.0))
        seg.setZValue(z + 0.01)
        t = QTransform()
        t.translate(centre.x, centre.y)
        t.rotate(neg_angle_deg)
        seg.setTransform(t)
        return seg

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
            # a rectangle hugging the pin cloud (same as GenericBody).
            generic: list[QGraphicsItem] = GenericBody().draw(
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
        r = self._effective_corner_radius_mm() / theme.raster_pitch_mm
        local_rect = QRectF(box.x, box.y, box.width, box.height)
        place = self._body_placement(positions)
        path = QPainterPath()
        if r > 0:
            path.addRoundedRect(local_rect, r, r)
        else:
            path.addRect(local_rect)
        path_item = QGraphicsPathItem(place.map(path))
        fill_c = QColor(fill)
        fill_c.setAlphaF(fill_c.alphaF() * alpha)
        path_item.setBrush(QBrush(fill_c))
        path_item.setPen(_body_pen(border, alpha))
        path_item.setZValue(z)
        items: list[QGraphicsItem] = [path_item]
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
