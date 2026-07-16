# SPDX-License-Identifier: MIT
"""Renderer base classes, shared paint helpers, and the dispatch registry.

``PackageRenderer`` carries everything shared across body shapes: colour /
appearance resolution, the canonical local→world placement, the radial
(top-view) drawing machinery used by every shape, and the bbox fallback.
Concrete shapes override :meth:`PackageRenderer.render_body`; the radial
top-view branch is handled once in :meth:`PackageRenderer.draw` (it is a
property of the lead arrangement, not of one shape).

Registration is declarative, two spellings: the :func:`register_renderer`
decorator with **typed axes** (``@register_renderer(kind="dip_ic")``,
``@register_renderer(component_class="devboard")``) is the preferred one; a
subclass that sets ``renders = "<name>"`` (the historical lineage-bag
spelling) registers the same way the moment the class body executes.  Either
way, dropping a plugin file into a library's ``renderers/`` directory is all
it takes (see :mod:`.discovery`).

Dispatch (:func:`renderer_for`): a ``component_class`` registration wins
first (the class is the part author's intent, whatever body it carries),
then the package's ``lineage`` is walked leaf-first, so the most specific
renderer wins and unknown names degrade towards the per-shape base renderer
— rendering never blocks on an unknown package or class.
"""

from __future__ import annotations

import math
from abc import ABC, abstractmethod
from collections.abc import Callable, Iterator
from contextlib import contextmanager
from dataclasses import replace
from typing import ClassVar, TypeVar

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
from protoboard.domain.components.instance import ComponentInstance
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
    bbox_or_none,
)
from protoboard.domain.components.package.shapes import (
    Prism as _PkgPrism,
)
from protoboard.domain.components.value_text import body_print_text
from protoboard.domain.layout.placement import ComponentPlacement
from protoboard.geometry import Point, Transform2D
from protoboard.gui.renderers.packages.body_label import make_body_label
from protoboard.gui.renderers.packages.canvas import (
    BodyCanvas,
    PaintState,
    _as_qcolor,
    _body_pen,
    qcolor_from_css,
)
from protoboard.gui.qt_geometry import to_qtransform
from protoboard.gui.renderers.prism_silhouette import rounded_rect_path
from protoboard.controller.theme import DEFAULT_THEME, Theme
from protoboard.gui.renderers.text_contrast import best_ink
from protoboard.services.debug_trace import traced

__all__ = [
    "BodyCanvas",
    "BodyShape",
    "GenericBody",
    "PackageRenderer",
    "PaintState",
    "qcolor_from_css",
    "register_renderer",
    "renderer_for",
]

_log = structlog.get_logger(__name__)


def _qtransform(t: Transform2D) -> QTransform:
    """Bridge a domain :class:`Transform2D` to a Qt ``QTransform``.

    Delegates to the canonical seam bridge in ``gui.qt_geometry``.
    """
    return to_qtransform(t)


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

# lineage name → renderer class. Populated declaratively — by the
# ``register_renderer`` decorator's lineage axes (shape_name / package_name /
# kind / family) or by ``PackageRenderer.__init_subclass__`` (``renders``);
# later registrations win (a user plugin overrides a builtin, mirroring the
# library-loader precedence).
BODY_RENDERERS: dict[str, type[PackageRenderer]] = {}

# component class → renderer class. The class axis of the decorator — a
# registration here wins the dispatch outright (see ``renderer_for``): the
# component class is the part author's intent for the WHOLE part, whatever
# body lineage it happens to carry (a devboard stays a plain PCB whether its
# package is a bare prism or extends someone's DIP base). Classes are open
# strings (a library may ship ``component_class: led_bar`` + a renderer
# plugin for it without touching this build's enum).
CLASS_RENDERERS: dict[str, type[PackageRenderer]] = {}


def _register(
    registry: dict[str, type[PackageRenderer]],
    axis: str,
    key: str,
    cls: type[PackageRenderer],
) -> None:
    """Enter *cls* under *key*; later registrations win, collisions are loud."""
    prev = registry.get(key)
    if prev is not None and prev is not cls:
        _log.warning(
            "body renderer overridden",
            axis=axis,
            key=key,
            old=f"{prev.__module__}.{prev.__qualname__}",
            new=f"{cls.__module__}.{cls.__qualname__}",
        )
    registry[key] = cls


def _axis_keys(value: str | tuple[str, ...] | None) -> tuple[str, ...]:
    if value is None:
        return ()
    return (value,) if isinstance(value, str) else tuple(value)


_RendererT = TypeVar("_RendererT", bound="type[PackageRenderer]")


def register_renderer(
    *,
    shape_name: str | tuple[str, ...] | None = None,
    package_name: str | tuple[str, ...] | None = None,
    kind: str | tuple[str, ...] | None = None,
    family: str | tuple[str, ...] | None = None,
    component_class: str | tuple[str, ...] | None = None,
) -> Callable[[_RendererT], _RendererT]:
    """Register a :class:`PackageRenderer` subclass on one or more typed axes.

    The preferred registration spelling — same registries as the historical
    ``renders = "<name>"`` attribute, but each key says *what it is*::

        @register_renderer(kind="dip_ic")             # a package kind
        @register_renderer(family="TO-220")           # a catalogue family id
        @register_renderer(package_name="prism_smd")  # a generic package name
        @register_renderer(shape_name="prism")        # a shape root
        @register_renderer(component_class="led_bar") # a component class

    The first four are **lineage axes**: package kinds, family ids, generic
    package names and shape roots all appear on a package's ``lineage``, so
    their keys share ``BODY_RENDERERS`` and resolve leaf-first, exactly like
    ``renders``.  ``component_class`` is the **class axis**
    (``CLASS_RENDERERS``): it dispatches on the *component's* declared class
    rather than the package, and wins over every lineage entry.  Each axis
    accepts one name or a tuple; at least one axis is required.  Later
    registrations win (a plugin overrides a builtin); a collision on the same
    key logs a warning.
    """
    lineage_keys: tuple[tuple[str, str], ...] = tuple(
        (axis, key)
        for axis, value in (
            ("shape_name", shape_name),
            ("package_name", package_name),
            ("kind", kind),
            ("family", family),
        )
        for key in _axis_keys(value)
    )
    class_keys = _axis_keys(component_class)
    if not lineage_keys and not class_keys:
        raise ValueError("register_renderer: at least one axis is required")

    def _decorate(cls: _RendererT) -> _RendererT:
        for axis, key in lineage_keys:
            _register(BODY_RENDERERS, axis, key, cls)
        for key in class_keys:
            _register(CLASS_RENDERERS, "component_class", key, cls)
        return cls

    return _decorate


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
        # registered subclass — class axis first, then the package's lineage —
        # so ``PackageRenderer(pkg)`` (the historical ``PackageBody(pkg)``)
        # transparently yields a ``CylinderRenderer`` / ``PrismRenderer`` /
        # plugin instance.  The ``component_class`` argument (positional or
        # keyword, mirroring ``__init__``) feeds the class axis.  Constructing
        # a concrete subclass directly bypasses the dispatch.
        if cls is not PackageRenderer:
            return object.__new__(cls)
        cc = args[0] if args else kwargs.get("component_class")
        target = renderer_for(
            package, component_class=cc if isinstance(cc, str) else None,
        )
        instance: PackageRenderer = object.__new__(target)
        return instance

    def __init_subclass__(cls, **kwargs: object) -> None:
        super().__init_subclass__(**kwargs)
        declared = cls.__dict__.get("renders")
        if declared is None:
            return
        for key in _axis_keys(declared):
            _register(BODY_RENDERERS, "renders", key, cls)

    def __init__(
        self,
        package: Package,
        component_class: str = ComponentClass.OTHER,
        vertical_flipped: bool = False,
        appearance: Appearance = Appearance(),
        side: str = "top",
        placement: ComponentPlacement | None = None,
        instance: ComponentInstance | None = None,
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
        # The placed part being drawn, when there is one (None in bare
        # package previews / tests). The escape hatch for renderers that need
        # the part's identity beyond the distilled facts above: a *bound*
        # instance reaches its definition (part name, pin_names) and its
        # ``value`` (e.g. a resistance for colour-band rendering) — see
        # ``ComponentInstance.bind``. Renderers must tolerate ``None``.
        self._instance = instance
        # Per-draw paint parameters; set by draw() for the duration of the
        # call (see the ``paint`` property).
        self._paint: PaintState | None = None

    @property
    def paint(self) -> PaintState:
        """The paint parameters of the draw in progress.

        Resolved fill/border, layer alpha, base Z and the theme — set by
        :meth:`draw` before :meth:`render_body` runs, so overrides and
        helpers read them here instead of threading five parameters.
        """
        if self._paint is None:
            raise RuntimeError(
                "paint is only available inside draw(); call draw(), "
                "not render_body(), on a fresh renderer"
            )
        return self._paint

    @contextmanager
    def paint_override(
        self,
        *,
        fill: QColor | str | None = None,
        border: QColor | str | None = None,
    ) -> Iterator[None]:
        """Temporarily replace the paint colours — e.g. to re-colour the base
        body from a subclass while keeping one's own details in the resolved
        colours::

            with self.paint_override(fill="#000000"):
                items = super().render_body(positions)
        """
        prev = self.paint
        state = prev
        if fill is not None:
            state = replace(state, fill=_as_qcolor(fill))
        if border is not None:
            state = replace(state, border=_as_qcolor(border))
        self._paint = state
        try:
            yield
        finally:
            self._paint = prev

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
        self._paint = PaintState(
            fill=fill, border=border, alpha=alpha, z=z, theme=theme,
        )
        items = self.render_body(positions)
        label = self._maybe_value_label(positions)
        if label is not None:
            items.append(label)
        return items

    @traced()
    def render_body(self, positions: list[Point]) -> list[QGraphicsItem]:
        """The body silhouette — THE override point for renderer subclasses.

        The draw parameters (resolved fill/border, alpha, z, theme) live on
        ``self.paint``; a subclass adds geometry by extending the base
        drawing::

            def render_body(self, positions):
                items = super().render_body(positions)
                items.append(...)   # e.g. the TO-220 metal tab
                return items

        The default draws the radial top-view when the arrangement calls for
        it, else the bbox rectangle.  Shape subclasses keep the same radial
        precedence and insert their side-view drawing between the two.
        """
        radial = self._maybe_radial(positions)
        if radial is not None:
            return radial
        return self._draw_bbox(positions)

    def _maybe_value_label(
        self, positions: list[Point],
    ) -> QGraphicsItem | None:
        """The component value printed on the body, or ``None``.

        Pinned to the body's local frame via ``_body_placement`` so it
        inherits the body's rotation and flip and stays fixed relative to the
        body geometry (not the pin row — matters for bendable pins).
        Scale-to-fit within the body's bbox, capped at
        ``theme.body_print_max_height``; the SMD chip keeps its own
        uncapped scale-to-fit code label.
        """
        if self._instance is None:
            return None
        text = body_print_text(self._component_class, self._instance.value)
        if text is None:
            return None
        box = bbox_or_none(self._pkg)
        if box is None:
            return None
        if box.width <= 0.0 or box.height <= 0.0:
            return None
        from protoboard.domain.components.package.shapes import (
            lead_axis_extents,
        )
        along, across = lead_axis_extents(self._pkg)
        if along <= 0.0 or across <= 0.0:
            return None
        return make_body_label(
            text,
            best_ink(self.paint.fill),
            self._body_placement(positions),
            box.center,
            self._pin_row_angle_deg(),
            avail_len=along * 0.8,
            avail_w=across * 0.75,
            max_height=self.paint.theme.body_print_max_height,
            alpha=self.paint.alpha,
            z=self.paint.z + 3 * BodyCanvas.Z_STEP,
        )

    def _pin_row_angle_deg(self) -> float:
        """Direction (local frame, degrees) of pin 1 → pin 2 — the axis the
        printed value runs along.

        Packages are NOT consistent about which local axis carries the pin
        row: a TO-92/TO-220/SMD chip's single row runs along local +x, but a
        DIP's two-column row runs along local +y (see ``Prism.bbox``'s "along
        the pin axis is the dominant axis of the pin cloud" note). Deriving
        the angle from the actual pin positions — rather than assuming +x —
        keeps the label aligned with the row for every package.
        """
        pins = self._pkg.pin_positions()
        if len(pins) < 2:
            return 0.0
        return (pins[1] - pins[0]).angle_deg()

    def _maybe_radial(
        self,
        positions: list[Point],
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
        return self._draw_radial(p1, p2, neg_pin, pads=positions)

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
        ``draw`` in tests / previews), ``positions`` IS the package-local pin
        cloud (there is no placement to project it through), and every
        local-frame geometry builder (``Prism.bbox()``, ``Cylinder.bbox()``,
        the radial silhouette anchor) already self-orients from that same
        local pin cloud — so the fallback only translates to pin 1; it must
        NOT also rotate by the pin1→pin2 heading, or packages whose local pin
        row runs along +y (a DIP) get double-rotated relative to their
        already-oriented bbox.
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
        place = QTransform()
        place.translate(p1.x, p1.y)
        return place

    # ------------------------------------------------------------------
    # Canvas factories — the drawing API for subclasses and plugins
    # ------------------------------------------------------------------

    def body_canvas(
        self,
        positions: list[Point],
        *,
        units: str = "holes",
    ) -> BodyCanvas:
        """A :class:`BodyCanvas` in the package-local frame (pin 1 at the
        origin), mapped through the SAME local→world transform as the pins —
        rotation and the bottom-side reflection included
        (:meth:`_body_placement`). The transform is baked into the item paths
        (the historical ``place.map`` convention of this frame).  Paint state
        comes from ``self.paint``; ``units="mm"`` makes the canvas convert
        geometry inputs from millimetres."""
        paint = self.paint
        return BodyCanvas(paint.theme, fill=paint.fill, border=paint.border,
                          alpha=paint.alpha, z=paint.z,
                          transform=self._body_placement(positions), bake=True,
                          units=units)

    def axis_canvas(
        self,
        positions: list[Point],
        p1: Point,
        p2: Point,
        *,
        units: str = "holes",
    ) -> BodyCanvas:
        """A :class:`BodyCanvas` in the pad-axis frame: origin at the pad
        centroid, local +x along the *p1*→*p2* lead axis — the frame the
        radial / SMD-chip bodies (and the TO-220 tab) draw in.  Paint state
        comes from ``self.paint``; ``units="mm"`` makes the canvas convert
        geometry inputs from millimetres."""
        paint = self.paint
        mid_x = sum(p.x for p in positions) / len(positions)
        mid_y = sum(p.y for p in positions) / len(positions)
        t = QTransform()
        t.translate(mid_x, mid_y)
        t.rotate((p2 - p1).angle_deg())
        return BodyCanvas(paint.theme, fill=paint.fill, border=paint.border,
                          alpha=paint.alpha, z=paint.z, transform=t,
                          units=units)

    # ------------------------------------------------------------------
    # Radial — top-view silhouette centred between the pads (all shapes)
    # ------------------------------------------------------------------

    def _draw_radial(
        self,
        p1: Point, p2: Point, neg_pin: Point,
        *,
        pads: list[Point] | None = None,
    ) -> list[QGraphicsItem]:
        # The body sits over every pad (a 3-lead TO-92 is centred on its three
        # pins, not just the first two); a lead stub is drawn to each.  ``pads``
        # defaults to the anchor pair for the legacy two-pin path.
        paint = self.paint
        all_pads = pads if pads is not None else [p1, p2]
        items: list[QGraphicsItem] = []
        for a, b in zip(all_pads, all_pads[1:]):
            lead = QGraphicsLineItem(a.x, a.y, b.x, b.y)
            lead.setPen(_lead_pen(paint.theme, paint.alpha))
            lead.setZValue(paint.z - 0.01)
            items.append(lead)

        # Centroid of all pads — the body's geometric centre.
        mid = Point(
            sum(p.x for p in all_pads) / len(all_pads),
            sum(p.y for p in all_pads) / len(all_pads),
        )
        items.extend(self._radial_silhouette(mid, p1, p2, neg_pin))
        return items

    def _radial_silhouette(
        self,
        mid: Point, p1: Point, p2: Point, neg_pin: Point,
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
        paint = self.paint
        length_r, width_r = lead_axis_extents(self._pkg)
        t = QTransform()
        t.translate(mid.x, mid.y)
        t.rotate((p2 - p1).angle_deg())
        canvas = BodyCanvas(paint.theme, fill=paint.fill, border=paint.border,
                            alpha=paint.alpha, z=paint.z, transform=t)
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

    @traced()
    def _draw_bbox(self, positions: list[Point]) -> list[QGraphicsItem]:
        box = bbox_or_none(self._pkg)
        if box is None:
            # No derivable body extent — fall back to a rectangle hugging the
            # pin cloud (same as GenericBody); the appearance rides along so
            # the radius override still applies (fill/border are already
            # resolved by draw()).
            paint = self.paint
            generic: list[QGraphicsItem] = GenericBody(self._appearance).draw(
                positions, paint.fill, paint.border, paint.alpha, paint.z,
                paint.theme,
            )
            return generic

        # bbox() is in the package-local frame (pin 1 at the origin); its x/y is
        # the body's top-left there (negative for a body centred on the pin
        # cloud).  Build the rectangle in that LOCAL frame and place it through
        # the SAME local→world transform the pins use (rotation + bottom-side
        # reflection): reconstructing the rect in world coords and only shifting
        # it by the anchor draws an axis-aligned box that ignores the placement
        # rotation — the body then stays upright while the pins rotate.
        canvas = self.body_canvas(positions)
        tl, tr, br, bl = self._effective_corner_radii_mm()
        canvas.addRect(
            (box.x, box.y),
            (box.x + box.width, box.y + box.height),
            round=(canvas.mm(tl), canvas.mm(tr), canvas.mm(br), canvas.mm(bl)),
        )
        items: list[QGraphicsItem] = canvas.items
        return items

    def _pad_active_rings(self, positions: list[Point]) -> list[QGraphicsItem]:
        """A faint pad-sized ring per pin, marking the click-active area."""
        paint = self.paint
        pad_r = paint.theme.pad_radius
        ring_c = QColor(paint.border)
        ring_c.setAlphaF(ring_c.alphaF() * paint.alpha * 0.35)
        pen = QPen(ring_c, paint.theme.pad_outline_width)
        rings: list[QGraphicsItem] = []
        for p in positions:
            ring = QGraphicsEllipseItem(
                QRectF(p.x - pad_r, p.y - pad_r, 2 * pad_r, 2 * pad_r)
            )
            ring.setPen(pen)
            ring.setBrush(QBrush(Qt.BrushStyle.NoBrush))
            ring.setZValue(paint.z + 0.02)
            rings.append(ring)
        return rings


def renderer_for(
    pkg: Package, component_class: str | None = None,
) -> type[PackageRenderer]:
    """The most specific registered renderer for *pkg*.

    A ``component_class`` registration wins outright (the class is the part
    author's intent for the whole part, whatever body it carries); then the
    package ``lineage`` is walked leaf-first (variant → family → extends chain
    → shape root).  An unknown name simply steps outward, so rendering never
    blocks on a package or class the registry does not know.  Packages without
    a lineage (hand-built in tests) fall back to their shape root.
    """
    if component_class is not None:
        by_class = CLASS_RENDERERS.get(component_class)
        if by_class is not None:
            return by_class
    for name in reversed(getattr(pkg, "lineage", ()) or ()):
        renderer = BODY_RENDERERS.get(name)
        if renderer is not None:
            return renderer
    if isinstance(pkg, _PkgCylinder):
        return BODY_RENDERERS["cylinder"]
    if isinstance(pkg, _PkgDisc):
        return BODY_RENDERERS["disc"]
    return BODY_RENDERERS["prism"]
