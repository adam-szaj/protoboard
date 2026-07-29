# SPDX-License-Identifier: MIT
"""Tests for body_shapes.py — shape dispatch, geometry helpers, item counts."""

from __future__ import annotations

import uuid
from typing import TYPE_CHECKING

import pytest
from PySide6.QtGui import QColor
from PySide6.QtWidgets import (
    QAbstractGraphicsShapeItem,
    QGraphicsEllipseItem,
    QGraphicsItem,
    QGraphicsLineItem,
    QGraphicsPathItem,
    QGraphicsRectItem,
)
from pytestqt.qtbot import QtBot

from protoboard.domain.components.definition import (
    ComponentClass,
    ComponentDefinition,
)
from protoboard.domain.components.footprints import (
    DIL,
    SIL,
    GenericFootprint,
)
from protoboard.domain.components.instance import ComponentInstance
from protoboard.domain.components.package.generators import (
    prism_from_footprint as _prism_from_footprint,
)
from protoboard.domain.components.package.kind import (
    GeneratorContext,
    build_kind_package,
)
from protoboard.domain.components.package.loader import build_package


def _kind_pkg(kind: str, pins: int, values: dict | None = None) -> Package:
    """Build a kind body via the (ctx, schema)->ctx API and return its package.

    Test adapter for the Phase-2 generator signature: the behaviour assertions
    below are unchanged; only the call shape moved."""
    ctx = build_kind_package(
        kind, GeneratorContext(pin_count=pins, values=values or {}))
    assert ctx.package is not None
    return ctx.package
from protoboard.domain.layout.placement import ComponentPlacement
from protoboard.geometry import Point
from protoboard.gui.renderers.body_shapes import (
    BodyShape,
    GenericBody,
    PackageBody,
    body_shape_for,
)
from protoboard.gui.renderers.prism_silhouette import (
    DipSilhouette,
    SilSilhouette,
    silhouette_for,
)
from protoboard.controller.theme import DEFAULT_THEME

if TYPE_CHECKING:
    from protoboard.domain.components.package import PackageFamily
    from protoboard.domain.components.package.shapes import Package, Prism

# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

_RED = QColor(200, 50, 50)
_DARK = QColor(30, 30, 30)
_Z = 5.0


def _flat(result: tuple[list, list]) -> list:
    """Combine a ``draw() → (body_items, label_items)`` result into one flat
    list, so tests that inspect drawn geometry see every item as before."""
    return result[0] + result[1]


def _pts(*coords: tuple[float, float]) -> list[Point]:
    return [Point(x, y) for x, y in coords]


def _path_is_rounded(item: QGraphicsPathItem) -> bool:
    """True when the item's path has curved (rounded) corners.

    The bbox body draws through a placed ``QPainterPath`` (so it can rotate with
    the pins), so square vs rounded corners is a path-geometry distinction, not a
    ``QGraphicsRectItem`` vs ``QGraphicsPathItem`` type distinction: a square
    corner is straight line/move elements, a rounded one adds ``CurveToElement``.
    """
    from PySide6.QtGui import QPainterPath

    path = item.path()
    curve = QPainterPath.ElementType.CurveToElement
    return any(path.elementAt(i).type == curve for i in range(path.elementCount()))


def _largest_path(items: list[QGraphicsItem]) -> QGraphicsPathItem:
    """The biggest ``QGraphicsPathItem`` — the body fill, not the pin-1 marker."""
    paths = [it for it in items if isinstance(it, QGraphicsPathItem)]
    return max(
        paths,
        key=lambda it: it.sceneBoundingRect().width()
        * it.sceneBoundingRect().height(),
    )


def _make_defn(
    cls: ComponentClass,
    n_pins: int,
    fp_type: str = "SIL",
) -> ComponentDefinition:
    if fp_type == "DIP":
        fp: object = DIL(n=n_pins)
    else:
        # ``SIL`` / ``TwoPin`` test markers are both a single-row pin cloud now;
        # a footprint is only pin count + spacing.
        fp = SIL(n=n_pins)
    return ComponentDefinition.from_footprint(
        name="TEST",
        component_class=cls,
        footprint=fp,  # type: ignore[arg-type]
        pin_names={},
    )


def _make_placement() -> ComponentPlacement:
    return ComponentPlacement(
        component_id=uuid.uuid4(),
        position=Point(0, 0),
        rotation=0,
        side="top",
    )


def _cylinder_package(edge_radius: float = 0.0) -> Package:
    """An axial cylinder (resistor-like) built through the package loader.

    ``edge_radius`` defaults to 0 → a rectangular body; pass diameter/2 for a
    fully-rounded capsule (the glass-diode look)."""
    return build_package(
        {
            "shape": "cylinder",
            "dims": {
                "diameter": 2.5,
                "length": 6.3,
                "edge_radius": edge_radius
            },
            "bend": 0,
            "leads": {
                "direction": [0, 0, -1],
                "body_offset": {
                    "unit": 2.5,
                    "value": [[4, 0]]
                },
                "board_offset": "default",
            },
        },
        "TEST_CYL",
    )


def _prism_package() -> Package:
    """A DIP-8 style prism built through the package loader.

    Declared as a DIP via the lineage (kind ``dip_ic``): the base renderer no
    longer column-guesses a silhouette for explicitly declared packages.
    """
    import dataclasses
    from protoboard.domain.components.package.shapes import Prism as _Prism
    pkg = build_package(
        {
            "shape": "prism",
            "dims": {
                "width": 7.62,
                "length": 10.0
            },
            "bend": 0,
            "leads": {
                "direction": [0, 0, -1],
                "body_offset": {
                    "unit": 2.5,
                    "value": [[3, [0, 1]], [3, 0], [3, [0, -1]]],
                },
                "board_offset": "default",
            },
        },
        "TEST_DIP",
    )
    assert isinstance(pkg, _Prism)
    return dataclasses.replace(pkg, lineage=("prism", "dip_ic"))


def _smd_chip_package() -> Package:
    """A two-pin SMD chip (0805-like) prism: flat axial body, 1-hole pins."""
    return build_package(
        {
            "shape": "prism",
            "dims": {
                "length": 2.0,
                "width": 1.25,
                "corner_radius": 0.12
            },
            "color": "#cdd2d6",
            "edge": "sharp",
            "arrangement": "axial",
            "bend": 0,
            "leads": {
                "direction": [[0, 0, -1], [0, 0, -1]],
                "body_offset": {
                    "unit": 2.5,
                    "value": [[1, 0]]
                },
                "board_offset": "default",
            },
        },
        "TEST_SMD",
    )


def _colored_cylinder_package(color: str = "#d6c08c",
                              edge: str = "rounded") -> Package:
    """An axial cylinder declaring an explicit body colour and edge style."""
    return build_package(
        {
            "shape": "cylinder",
            "dims": {
                "diameter": 2.5,
                "length": 6.3
            },
            "bend": 0,
            "color": color,
            "edge": edge,
            "leads": {
                "direction": [0, 0, -1],
                "body_offset": {
                    "unit": 2.5,
                    "value": [[4, 0]]
                },
                "board_offset": "default",
            },
        },
        "TEST_CYL_COL",
    )


def _colored_prism_package(color: str = "#d6c08c",
                           edge: str = "sharp") -> Package:
    return build_package(
        {
            "shape": "prism",
            "dims": {
                "width": 7.62,
                "length": 10.0,
                "corner_radius": 1.0
            },
            "bend": 0,
            "color": color,
            "edge": edge,
            "leads": {
                "direction": [0, 0, -1],
                "body_offset": {
                    "unit": 2.5,
                    "value": [[3, [0, 1]], [3, 0], [3, [0, -1]]],
                },
                "board_offset": "default",
            },
        },
        "TEST_DIP_COL",
    )


def _bbox_prism_positions() -> list[Point]:
    """Pins for a three-column prism — no DIP/SIL silhouette matches it, so the
    body renders via the plain bbox path (where edge style still applies)."""
    return _pts((0, 0), (0, 1), (1, 0), (1, 1), (2, 0), (2, 1))


def _bbox_prism_package(color: str = "#d6c08c",
                        edge: str = "sharp") -> Package:
    """A three-column prism (silhouette_for → None → bbox rendering)."""
    return build_package(
        {
            "shape": "prism",
            "dims": {
                "width": 7.62,
                "length": 7.62,
                "corner_radius": 1.0
            },
            "bend": 0,
            "color": color,
            "edge": edge,
            "leads": {
                "direction": [0, 0, -1],
                # 3 columns: down, across, down, across, down (6 pins).
                "body_offset": {
                    "unit": 2.5,
                    "value": [[0, 1], [1, -1], [0, 1], [1, -1], [0, 1]],
                },
                "board_offset": "default",
            },
        },
        "TEST_BBOX_PRISM",
    )


# ---------------------------------------------------------------------------
# GenericBody
# ---------------------------------------------------------------------------


class TestGenericBody:

    def test_empty_positions_returns_no_items(self, qtbot: QtBot) -> None:
        items = _flat(GenericBody().draw([], _RED, _DARK, 1.0, _Z))
        assert items == []

    def test_single_position_returns_rect(self, qtbot: QtBot) -> None:
        items = _flat(GenericBody().draw(_pts((0, 0)), _RED, _DARK, 1.0, _Z))
        assert len(items) == 1
        assert isinstance(items[0], QGraphicsRectItem)

    def test_two_pins_horizontal(self, qtbot: QtBot) -> None:
        items = _flat(
            GenericBody().draw(_pts((0, 0), (2, 0)), _RED, _DARK, 1.0, _Z))
        assert len(items) == 1
        rect = items[0]
        assert isinstance(rect, QGraphicsRectItem)
        bbox = rect.rect()
        assert bbox.width() >= 2.0

    def test_z_value_is_set(self, qtbot: QtBot) -> None:
        items = _flat(GenericBody().draw(_pts((0, 0)), _RED, _DARK, 1.0, _Z))
        assert items[0].zValue() == pytest.approx(_Z)

    def test_appearance_color_override(self, qtbot: QtBot) -> None:
        # The bare-bbox fallback honours the style override like PackageBody:
        # the declared colour replaces the caller-supplied fill.
        from protoboard.domain.components.appearance import Appearance
        items = _flat(GenericBody(Appearance(color="#123456")).draw(
            _pts((0, 0), (2, 2)), _RED, _DARK, 1.0, _Z))
        body = items[0]
        assert isinstance(body, QGraphicsRectItem)
        assert body.brush().color().name() == "#123456"

    def test_appearance_corner_radius_rounds_bbox(self, qtbot: QtBot) -> None:
        from protoboard.domain.components.appearance import Appearance
        items = _flat(GenericBody(Appearance(corner_radius_mm=(1.0,))).draw(
            _pts((0, 0), (2, 2)), _RED, _DARK, 1.0, _Z))
        body = items[0]
        assert isinstance(body, QGraphicsPathItem)
        assert _path_is_rounded(body)

    def test_no_override_keeps_plain_rect(self, qtbot: QtBot) -> None:
        from protoboard.domain.components.appearance import Appearance
        items = _flat(GenericBody(Appearance()).draw(
            _pts((0, 0), (2, 2)), _RED, _DARK, 1.0, _Z))
        assert isinstance(items[0], QGraphicsRectItem)


# Legacy TO92Body / TO220Body were deleted: TO-92 renders through the
# flat-sided radial CylinderRenderer (TestTO92PackageParity, TestFlatSide*),
# TO-220 through the renderer plugin (TestTO220PackageParity + the
# TestTO220Plugin tab tests in test_renderer_registry.py).


def _radial_cylinder_package(
    *,
    flat_radius: float | None = None,
    flat_normal: list[float] | None = None,
) -> Package:
    """A radial (top-view) cylinder built through the loader, optionally with a
    flat side (D-shape). 2 pins exiting the same face, body over the midpoint."""
    dims: dict[str, object] = {"diameter": 5.0, "length": 5.0}
    if flat_radius is not None:
        dims["flat_radius"] = flat_radius
        dims["flat_normal"] = flat_normal
    return build_package(
        {
            "shape": "cylinder",
            "dims": dims,
            "arrangement": "radial",
            "bend": 0,
            "leads": {
                "direction": [[0, 0, -1], [0, 0, -1]],
                "body_offset": {
                    "unit": 2.5,
                    "value": [[1, 0]]
                },
                "board_offset": "default",
            },
        },
        "TEST_RADIAL_CYL",
    )


def _radial_body(pkg: object) -> QGraphicsItem:
    """The single body item (ellipse or D-shape path) for a non-polar radial
    cylinder — leads aside, the first non-line item is the body."""
    items = _flat(PackageBody(pkg).draw(  # type: ignore[arg-type]
        _pts((0, 0), (0, 1)), _RED, _DARK, 1.0, _Z))
    bodies = [it for it in items if not isinstance(it, QGraphicsLineItem)]
    assert len(bodies) == 1
    return bodies[0]


class TestFlatSidedCylinderRender:

    def test_full_circle_when_no_flat(self, qtbot: QtBot) -> None:
        body = _radial_body(_radial_cylinder_package())
        assert isinstance(body, QGraphicsEllipseItem)

    def test_flat_side_draws_d_shape_path(self, qtbot: QtBot) -> None:
        body = _radial_body(
            _radial_cylinder_package(flat_radius=1.175,
                                     flat_normal=[-1.0, 0.0]))
        # A chord-cut body is a custom path, not a full ellipse.
        assert isinstance(body, QGraphicsPathItem)
        assert not isinstance(body, QGraphicsEllipseItem)

    def test_d_shape_chord_on_flat_normal_side(self, qtbot: QtBot) -> None:
        # Pins run down +y in world (0,0)->(0,1); flat_normal [-1,0] in local
        # frame. The flat chord must sit on the correct side: the bounding rect
        # of the kept arc is narrower on the flat side than a full circle.
        body = _radial_body(
            _radial_cylinder_package(flat_radius=1.175,
                                     flat_normal=[-1.0, 0.0]))
        full = _radial_body(_radial_cylinder_package())
        # The D-shape loses area on one side → its scene bounding rect is
        # strictly smaller in one dimension than the full circle's.
        fb = body.sceneBoundingRect()
        cb = full.sceneBoundingRect()
        assert fb.width() < cb.width() - 1e-6 or fb.height(
        ) < cb.height() - 1e-6

    def test_flat_side_rotates_with_pad_axis(self, qtbot: QtBot) -> None:
        # Same package, different world pad orientations → the flat chord points
        # a different world direction (the D-shape follows the part's rotation).
        pkg = _radial_cylinder_package(flat_radius=1.175,
                                       flat_normal=[-1.0, 0.0])
        items_v = _flat(PackageBody(pkg).draw(  # type: ignore[arg-type]
            _pts((0, 0), (0, 1)), _RED, _DARK, 1.0, _Z))
        items_h = _flat(PackageBody(pkg).draw(  # type: ignore[arg-type]
            _pts((0, 0), (1, 0)), _RED, _DARK, 1.0, _Z))
        body_v = next(it for it in items_v
                      if isinstance(it, QGraphicsPathItem))
        body_h = next(it for it in items_h
                      if isinstance(it, QGraphicsPathItem))
        # Different pad axis → different body transform rotation.
        assert body_v.transform().m11() != pytest.approx(body_h.transform().m11()) \
            or body_v.transform().m12() != pytest.approx(body_h.transform().m12())

    def test_flat_normal_mirrors_on_bottom_side(self, qtbot: QtBot) -> None:
        # An asymmetric body (a flat face perpendicular to the lead axis) is
        # physically flipped on the bottom side: the flat-side normal reflects
        # about the package flip axis so the face points the other way.  The
        # test package's pins run along +x, so the flip axis is parallel to x
        # (reflect y); a flat_normal of [0,-1] (perpendicular to the pin row)
        # flips sign top vs bottom.
        pkg = _radial_cylinder_package(flat_radius=1.175,
                                       flat_normal=[0.0, -1.0])
        top = PackageBody(pkg, side="top")  # type: ignore[arg-type]
        bot = PackageBody(pkg, side="bottom")  # type: ignore[arg-type]
        world_axis_deg = 0.0  # pins run along +x
        flat_top = top._flat_side_world(pkg, world_axis_deg)  # type: ignore[arg-type]
        flat_bot = bot._flat_side_world(pkg, world_axis_deg)  # type: ignore[arg-type]
        assert flat_top is not None and flat_bot is not None
        # Same offset, opposite normal direction (180° apart, mod 360).
        assert flat_top[1] == pytest.approx(flat_bot[1])
        diff = (flat_top[0] - flat_bot[0]) % 360.0
        assert diff == pytest.approx(180.0, abs=1e-6)

    def test_flat_normal_along_pin_row_unchanged_on_bottom(
        self, qtbot: QtBot
    ) -> None:
        # A flat normal PARALLEL to the pin row lies on the flip axis, so a
        # board-side flip leaves it unchanged (rotating about the row can't move
        # a direction along that row).  Pins run along +x, flat_normal [-1,0].
        pkg = _radial_cylinder_package(flat_radius=1.175,
                                       flat_normal=[-1.0, 0.0])
        top = PackageBody(pkg, side="top")  # type: ignore[arg-type]
        bot = PackageBody(pkg, side="bottom")  # type: ignore[arg-type]
        flat_top = top._flat_side_world(pkg, 0.0)  # type: ignore[arg-type]
        flat_bot = bot._flat_side_world(pkg, 0.0)  # type: ignore[arg-type]
        assert flat_top is not None and flat_bot is not None
        # Same direction (compare mod 360: 180° ≡ -180°).
        diff = (flat_top[0] - flat_bot[0]) % 360.0
        assert diff == pytest.approx(0.0, abs=1e-4) or diff == pytest.approx(
            360.0, abs=1e-4
        )


def _dip8_prism() -> Prism:
    """The prism synthesized for a DIP-8 footprint, declared as a DIP.

    The ``dip_ic`` lineage makes it a DECLARED look (like the generated
    ``dip-N`` families) — the base renderer only guesses for ``_generic``
    bodies of bare-footprint parts.
    """
    import dataclasses

    from protoboard.domain.components.package.shapes import Prism
    prism = _prism_from_footprint(DIL(n=8, rows_spacing=3))
    assert isinstance(prism, Prism)
    return dataclasses.replace(
        prism, lineage=("prism", "dip_ic"))


def _dip8_positions() -> list[Point]:
    return _pts(*([(0, i) for i in range(4)] + [(3, 3 - i) for i in range(4)]))


def _sil6_prism() -> Prism:
    """The prism synthesized for a 6-pin single-row (SIL) footprint."""
    from protoboard.domain.components.package.shapes import Prism
    prism = _prism_from_footprint(SIL(n=6))
    assert isinstance(prism, Prism)
    return prism


class TestParametricDIPBody:
    """The DIP body now renders parametrically (synthesized prism + DIP
    silhouette), replacing the legacy hand-written SVG/DIP generators.  These
    re-assert the behaviour those generators provided."""

    def test_dip_footprint_synthesizes_dip_silhouette(self) -> None:
        prism = _dip8_prism()
        assert isinstance(silhouette_for(prism),
                          DipSilhouette)  # type: ignore[arg-type]

    def test_produces_items_for_dip8(self, qtbot: QtBot) -> None:
        items = _flat(PackageBody(_dip8_prism()).draw(  # type: ignore[arg-type]
            _dip8_positions(), _RED, _DARK, 1.0, _Z))
        assert len(items) >= 1
        # The body is a path (the notched silhouette), not a plain rect.
        assert any(isinstance(it, QGraphicsPathItem) for it in items)

    def test_body_has_pin1_notch(self, qtbot: QtBot) -> None:
        # The DIP body is centred between the rows (cols at x=0 and x=3, so the
        # body spans roughly x=0.5…2.5) and sits a little past the end pins
        # (top edge near y=-0.5).  The notch (r=0.25) cuts the top edge at the
        # row midpoint x=1.5.  A point in that carved zone is OUTSIDE the body;
        # the interior and the un-notched part of the top edge stay filled.
        from PySide6.QtCore import QPointF
        items = _flat(PackageBody(_dip8_prism()).draw(  # type: ignore[arg-type]
            _dip8_positions(), _RED, _DARK, 1.0, _Z))
        body = max(
            (it for it in items if isinstance(it, QGraphicsPathItem)),
            key=lambda it: it.sceneBoundingRect().width(),
        )
        path = body.path()
        assert not path.contains(QPointF(1.5, -0.4))  # inside the notch
        assert path.contains(QPointF(1.5, 1.5))  # body interior
        # Un-notched part of the top edge (within the body, off the notch).
        assert path.contains(QPointF(0.8, -0.4))

    def test_too_few_pins_falls_back(self, qtbot: QtBot) -> None:
        # A single position can't form a body → the bbox fallback (a rect or
        # generic rectangle), never a crash.
        items = _flat(PackageBody(_dip8_prism()).draw(  # type: ignore[arg-type]
            _pts((0, 0)), _RED, _DARK, 1.0, _Z))
        assert items
        assert isinstance(items[0], (QGraphicsRectItem, QGraphicsPathItem))


class TestDeclaredKindSilhouette:
    """A package's declared ``kind`` chooses the body look (notch vs plain)
    via the renderer registry — a kind renderer's ``_silhouette`` is the
    declared look — instead of the base renderer guessing from the pin
    columns. An unknown / absent kind falls back to the column guess, so
    render never blocks."""

    def test_dip_ic_kind_selects_notch(self) -> None:
        from protoboard.gui.renderers.packages import DipIcRenderer
        pkg = _kind_pkg("dip_ic", 8)
        shape = PackageBody(pkg)  # type: ignore[arg-type]
        assert type(shape) is DipIcRenderer
        assert isinstance(shape._silhouette(pkg),  # type: ignore[arg-type]
                          DipSilhouette)

    def test_dip_switch_kind_is_plain_overriding_the_column_guess(
            self) -> None:
        # dip_switch has DIP geometry (two columns) so the COLUMN guess would put
        # a notch on it — the declared kind dispatches PlainBodyRenderer, and
        # that wins.
        from protoboard.gui.renderers.packages import PlainBodyRenderer
        from protoboard.gui.renderers.prism_silhouette import silhouette_for
        from protoboard.domain.components.package.shapes import Prism
        pkg = _kind_pkg("dip_switch", 8)
        assert isinstance(pkg, Prism)
        assert isinstance(silhouette_for(pkg),
                          DipSilhouette)
        shape = PackageBody(pkg)  # type: ignore[arg-type]
        assert type(shape) is PlainBodyRenderer
        assert shape._silhouette(pkg) is None  # type: ignore[arg-type]

    def test_unknown_kind_renders_plain_not_guessed(self) -> None:
        # A prism with an unrecognised kind (no registered renderer) renders
        # by the base PrismRenderer as the PLAIN declared rectangle — a
        # declared package is never column-guessed into a DIP notch, and an
        # unknown kind never crashes the render.
        import dataclasses

        from protoboard.gui.renderers.packages import PrismRenderer
        prism = dataclasses.replace(
            _dip8_prism(), kind="totally_new_kind",
            lineage=("prism", "totally_new_kind"),
        )
        shape = PackageBody(prism)  # type: ignore[arg-type]
        assert type(shape) is PrismRenderer
        assert shape._silhouette(prism) is None  # type: ignore[arg-type]

    def test_dip_switch_renders_without_notch(self, qtbot: QtBot) -> None:
        # End-to-end: a dip_switch body draws the plain bbox rectangle, not the
        # notched silhouette a column-guessed DIP would produce.  Both bodies are
        # placed QGraphicsPathItems (so they rotate with the pins); the
        # distinction is the notch — the plain bbox rectangle is a 4-corner path,
        # the dip_ic silhouette carries extra elements for the pin-1 notch.
        sw = _kind_pkg("dip_switch", 8)
        ic = _kind_pkg("dip_ic", 8)
        sw_body = _largest_path(_flat(PackageBody(sw).draw(  # type: ignore[arg-type]
            _dip8_positions(), _RED, _DARK, 1.0, _Z)))
        ic_body = _largest_path(_flat(PackageBody(ic).draw(  # type: ignore[arg-type]
            _dip8_positions(), _RED, _DARK, 1.0, _Z)))
        # The plain bbox rectangle is a simple 5-element closed rect; the notched
        # DIP silhouette has strictly more path elements (the notch geometry).
        assert sw_body.path().elementCount() <= 6
        assert ic_body.path().elementCount() > sw_body.path().elementCount()


class TestConnectorKind:
    """``pin_header`` / ``jst`` kinds: generated from pin count + arrangement,
    a plain rectangle (no DIP notch / SIL slots) with a per-kind fill colour."""

    def test_pin_header_single_row(self) -> None:
        pkg = _kind_pkg("pin_header", 4, {"rows": 1})
        assert pkg.pin_count() == 4
        assert pkg.color == "#222222"  # type: ignore[attr-defined]
        # 1×N → all pins in one column.
        xs = {round(p.x, 3) for p in pkg.pin_positions()}
        assert len(xs) == 1

    def test_pin_header_dual_row(self) -> None:
        pkg = _kind_pkg("pin_header", 6, {"rows": 2})
        assert pkg.pin_count() == 6
        # 2×N → two columns, 1 hole apart (connector spacing, not DIP's 3).
        xs = sorted({round(p.x, 3) for p in pkg.pin_positions()})
        assert xs == [0.0, 1.0]

    def test_pin_header_defaults_to_single_row(self) -> None:
        pkg = _kind_pkg("pin_header", 4)
        assert len({round(p.x, 3) for p in pkg.pin_positions()}) == 1

    def test_jst_is_single_row_light(self) -> None:
        pkg = _kind_pkg("jst", 3)
        assert pkg.pin_count() == 3
        assert pkg.color == "#dddddd"  # type: ignore[attr-defined]
        assert len({round(p.x, 3) for p in pkg.pin_positions()}) == 1

    def test_connectors_render_plain_no_notch(self, qtbot: QtBot) -> None:
        # Neither a 1×N (SIL-like) nor a 2×N (DIP-like) connector gets the SIL
        # slots / DIP notch the column guess would add — the kind dispatches
        # PlainBodyRenderer, whose declared silhouette is None (plain rect).
        from protoboard.gui.renderers.packages import PlainBodyRenderer
        for kind, n, params in [
            ("pin_header", 6, {
                "rows": 1
            }),
            ("pin_header", 6, {
                "rows": 2
            }),
            ("jst", 4, {}),
        ]:
            pkg = _kind_pkg(kind, n, params)
            shape = PackageBody(pkg)  # type: ignore[arg-type]
            assert type(shape) is PlainBodyRenderer, (kind, params)
            assert shape._silhouette(pkg) is None  # type: ignore[arg-type]


class TestConnectorPlacement:
    """Connectors place with no body-picker choice (like dip_ic ICs): the kind
    package is their only geometry, so preview and placed resolve identically."""

    def _setup(
        self, name: str
    ) -> tuple[ComponentDefinition, ComponentInstance, dict[str,
                                                            PackageFamily]]:
        from pathlib import Path

        from protoboard.domain.components.instance import ComponentInstance
        from protoboard.services.library.loader import load_library
        from protoboard.services.library.package_family_loader import (
            load_full_package_catalogue,
        )
        cat = load_full_package_catalogue()
        lib = load_library([Path("library")], cat)
        defn = lib.get(name)
        inst = ComponentInstance.new(name, "J1", "")
        return defn, inst, cat

    def test_connectors_have_no_footprints_list(self) -> None:
        # No footprints: list → the body picker is empty → no forced choice
        # blocks placement (the bug the user hit).
        for name in ("PinHeader_1x04", "PinHeader_2x05", "JST_PH_3pin"):
            defn, _inst, _cat = self._setup(name)
            assert not defn.footprints, name

    def test_preview_and_placed_render_same_kind_prism(self,
                                                       qtbot: QtBot) -> None:
        from protoboard.domain.components.package.shapes import Prism
        for name in ("PinHeader_1x04", "PinHeader_2x05", "JST_XH_5pin"):
            defn, inst, cat = self._setup(name)
            preview = body_shape_for(defn, package_families=cat)
            placed = body_shape_for(defn,
                                    _make_placement(),
                                    inst,
                                    package_families=cat)
            assert isinstance(preview, PackageBody), name
            assert isinstance(placed, PackageBody), name
            assert isinstance(preview._pkg,
                              Prism), name  # type: ignore[attr-defined]
            assert preview._pkg.kind in (
                "pin_header", "jst"), name  # type: ignore[attr-defined]


class TestTO92PackageParity:
    """No-silent-regression guard for the reported bug: a TO-92 transistor
    rendered the correct body in place-preview but a generic body after
    placement. Root cause was the picker guessing a ``sil-3`` package by pin
    count and stamping it on the instance, overriding the dedicated body. Now
    TO-92 is a fully-specified flat-cylinder *package* (the component declares
    ``footprints: [TO-92]``), so preview and placed resolve the SAME package.
    """

    def _setup(
        self,
    ) -> tuple[ComponentDefinition, ComponentInstance, dict[str,
                                                            PackageFamily]]:
        from pathlib import Path

        from protoboard.domain.components.instance import ComponentInstance
        from protoboard.services.library.loader import load_library
        from protoboard.services.library.package_family_loader import (
            load_full_package_catalogue,
        )
        cat = load_full_package_catalogue()
        lib = load_library([Path("library")], cat)
        defn = lib.get("2N2222")
        inst = ComponentInstance.new("2N2222",
                                     "Q1",
                                     "",
                                     package_variant="TO-92")
        return defn, inst, cat

    def test_transistor_declares_to92_footprint(self) -> None:
        defn, _inst, _cat = self._setup()
        assert defn.symbol == "Q"
        assert [(o.name, o.ref)
                for o in defn.footprints] == [("TO92", "TO-92")]

    def test_preview_and_placed_render_the_same_flat_cylinder(
        self,
        qtbot: QtBot,
    ) -> None:
        from protoboard.domain.components.package.shapes import Cylinder
        defn, inst, cat = self._setup()
        # Preview: no instance. Placed: instance carries the auto-selected
        # "TO-92" address. Both must resolve the flat-sided cylinder package.
        preview = body_shape_for(defn, package_families=cat)
        placed = body_shape_for(defn,
                                _make_placement(),
                                inst,
                                package_families=cat)
        # Both go through the parametric PackageBody (not TO92Body/GenericBody).
        assert isinstance(preview, PackageBody)
        assert isinstance(placed, PackageBody)
        assert isinstance(preview._pkg, Cylinder)  # type: ignore[attr-defined]
        assert isinstance(placed._pkg, Cylinder)  # type: ignore[attr-defined]
        assert preview._pkg.flat_radius_mm == 1.175  # type: ignore[attr-defined]
        assert placed._pkg.flat_radius_mm == 1.175  # type: ignore[attr-defined]

    def test_placed_is_not_generic_body(self, qtbot: QtBot) -> None:
        defn, inst, cat = self._setup()
        placed = body_shape_for(defn,
                                _make_placement(),
                                inst,
                                package_families=cat)
        assert not isinstance(placed, GenericBody)


class TestTO220PackageParity:
    """Same no-silent-regression guard as TO-92, for the TO-220 power package.
    Regulators (7805) and power MOSFETs (IRF540_N) declare footprints: [TO-220],
    a fully-specified prism package, so preview and placed resolve identically.
    (The metal tab is a deferred refinement — docs/todo.md; the body is a plain
    radial prism rectangle for now.)
    """

    def _setup(
        self,
        name: str = "7805"
    ) -> tuple[ComponentDefinition, ComponentInstance, dict[str,
                                                            PackageFamily]]:
        from pathlib import Path

        from protoboard.domain.components.instance import ComponentInstance
        from protoboard.services.library.loader import load_library
        from protoboard.services.library.package_family_loader import (
            load_full_package_catalogue,
        )
        cat = load_full_package_catalogue()
        lib = load_library([Path("library")], cat)
        defn = lib.get(name)
        inst = ComponentInstance.new(name, "U1", "", package_variant="TO-220")
        return defn, inst, cat

    def test_regulator_declares_to220_footprint(self) -> None:
        defn, _inst, _cat = self._setup("7805")
        assert [(o.name, o.ref)
                for o in defn.footprints] == [("TO220", "TO-220")]

    def test_preview_and_placed_render_the_same_prism(self,
                                                      qtbot: QtBot) -> None:
        from protoboard.domain.components.package.shapes import Prism
        for name in ("7805", "LM317", "IRF540_N"):
            defn, inst, cat = self._setup(name)
            preview = body_shape_for(defn, package_families=cat)
            placed = body_shape_for(defn,
                                    _make_placement(),
                                    inst,
                                    package_families=cat)
            assert isinstance(preview, PackageBody), name
            assert isinstance(placed, PackageBody), name
            assert isinstance(preview._pkg,
                              Prism), name  # type: ignore[attr-defined]
            assert isinstance(placed._pkg,
                              Prism), name  # type: ignore[attr-defined]

    def test_board_pins_one_hole_apart(self) -> None:
        from protoboard.domain.components.package.shapes import Prism
        _defn, inst, cat = self._setup("7805")
        from protoboard.domain.components.package.component_package import (
            resolve_component_package,
        )
        pkg = resolve_component_package(_defn, inst, cat)
        assert isinstance(pkg, Prism)
        from protoboard.geometry import Point
        assert pkg.pin_positions() == [
            Point(0, 0), Point(1.0, 0),
            Point(2.0, 0)
        ]


def _colored_prism(color: str) -> Package:
    """A 2-pin axial SMD-chip prism carrying its own package ``color``."""
    return build_package(
        {
            "shape": "prism",
            "dims": {
                "length": 2.0,
                "width": 1.25,
                "height": 0.55
            },
            "color": color,
            "edge": "sharp",
            "arrangement": "axial",
            "bend": 0,
            "leads": {
                "direction": [[0, 0, -1], [0, 0, -1]],
                "body_offset": {
                    "unit": 2.5,
                    "value": [[1, 0]]
                },
                "board_offset": "default",
            },
        },
        "TEST_COLORED",
    )


class TestAppearanceOverride:
    """A visual-only Appearance override (color + corner radius) on PackageBody,
    layered over the package's own color/edge. Geometry is unchanged."""

    def _body_brush(self, items: list) -> QColor:
        body = max(
            (it for it in items
             if isinstance(it, (QGraphicsPathItem, QGraphicsRectItem))),
            key=lambda it: it.sceneBoundingRect().width() * it.
            sceneBoundingRect().height(),
        )
        return body.brush().color()  # type: ignore[attr-defined]

    def test_color_override_beats_package_color(self, qtbot: QtBot) -> None:
        from protoboard.domain.components.appearance import Appearance
        pkg = _colored_prism("#cdd2d6")  # shared package color
        black = PackageBody(
            pkg,
            appearance=Appearance(color="#1a1a1a"))  # type: ignore[arg-type]
        beige = PackageBody(
            pkg,
            appearance=Appearance(color="#d9c7a3"))  # type: ignore[arg-type]
        pts = _pts((0, 0), (1, 0))
        assert self._body_brush(_flat(black.draw(pts, _RED, _DARK, 1.0,
                                                 _Z))).name() == "#1a1a1a"
        assert self._body_brush(_flat(beige.draw(pts, _RED, _DARK, 1.0,
                                                 _Z))).name() == "#d9c7a3"
        # No override → the package color shows through.
        plain = PackageBody(pkg)  # type: ignore[arg-type]
        assert self._body_brush(_flat(plain.draw(pts, _RED, _DARK, 1.0,
                                                 _Z))).name() == "#cdd2d6"

    def test_color_alpha_channel_flows_to_brush(self, qtbot: QtBot) -> None:
        # CSS #rrggbbaa (alpha last): the declared alpha lands on the body
        # brush and MULTIPLIES with the layer alpha (ghosting/dimming) rather
        # than replacing it.
        from protoboard.domain.components.appearance import Appearance
        pkg = _colored_prism("#cdd2d6")
        body = PackageBody(
            pkg,
            appearance=Appearance(color="#1a1a1a80"))  # type: ignore[arg-type]
        pts = _pts((0, 0), (1, 0))
        brush = self._body_brush(_flat(body.draw(pts, _RED, _DARK, 1.0, _Z)))
        assert brush.name() == "#1a1a1a"
        assert brush.alphaF() == pytest.approx(0x80 / 255, abs=0.01)
        dimmed = self._body_brush(_flat(body.draw(pts, _RED, _DARK, 0.5, _Z)))
        assert dimmed.alphaF() == pytest.approx(0.5 * 0x80 / 255, abs=0.01)

    def test_package_color_alpha_channel(self, qtbot: QtBot) -> None:
        # The package-level ``color:`` uses the same CSS convention.
        pkg = _colored_prism("#cdd2d680")
        brush = self._body_brush(
            _flat(PackageBody(pkg).draw(  # type: ignore[arg-type]
                _pts((0, 0), (1, 0)), _RED, _DARK, 1.0, _Z)))
        assert brush.name() == "#cdd2d6"
        assert brush.alphaF() == pytest.approx(0x80 / 255, abs=0.01)

    def test_dip_body_fill_follows_override(self, qtbot: QtBot) -> None:
        from protoboard.domain.components.appearance import Appearance
        body = PackageBody(  # type: ignore[arg-type]
            _dip8_prism(),
            component_class=ComponentClass.IC,
            appearance=Appearance(color="#3050a0"),
        )
        items = _flat(body.draw(_dip8_positions(), _RED, _DARK, 1.0, _Z))
        assert self._body_brush(items).name() == "#3050a0"

    def test_corner_radius_zero_is_square_gt0_is_rounded(self,
                                                         qtbot: QtBot) -> None:
        # The bbox/devboard path: override 0 → square body path, > 0 → rounded
        # body path.  Use a 3-column prism (no silhouette → bbox path).  The
        # body is always a placed QGraphicsPathItem (so it rotates with the
        # pins); square vs rounded is read off the path geometry.
        from protoboard.domain.components.appearance import Appearance
        prism = _bbox_prism_package()
        sharp = PackageBody(  # type: ignore[arg-type]
            prism,
            appearance=Appearance(corner_radius_mm=(0.0, )))
        rounded = PackageBody(  # type: ignore[arg-type]
            prism,
            appearance=Appearance(corner_radius_mm=(0.5, )))
        pts = _bbox_prism_positions()
        s_body = _largest_path(_flat(sharp.draw(pts, _RED, _DARK, 1.0, _Z)))
        r_body = _largest_path(_flat(rounded.draw(pts, _RED, _DARK, 1.0, _Z)))
        assert not _path_is_rounded(s_body)
        assert _path_is_rounded(r_body)

    def test_per_corner_radii_round_only_declared_corners(
        self, qtbot: QtBot,
    ) -> None:
        # CSS border-radius order (TL, TR, BR, BL): only the third value is
        # non-zero → only the bottom-right corner is cut, the other three stay
        # sharp.  Probed geometrically: a point just inside a sharp corner is
        # inside the path; the same probe at the rounded corner falls outside
        # the cut.  Identity placement (pin 1 at origin, pin 2 straight down),
        # so world path coords equal the package-local bbox.
        from PySide6.QtCore import QPointF
        from protoboard.domain.components.appearance import Appearance
        prism = _bbox_prism_package()
        body = PackageBody(  # type: ignore[arg-type]
            prism,
            appearance=Appearance(corner_radius_mm=(0.0, 0.0, 2.0, 0.0)))
        path = _largest_path(
            _flat(body.draw(_bbox_prism_positions(), _RED, _DARK, 1.0,
                            _Z))).path()
        box = prism.bbox()
        x0, y0 = box.x, box.y
        x1, y1 = box.x + box.lx, box.y + box.ly
        eps = 0.08  # holes; well inside the 2 mm (≈0.79-hole) corner cut
        assert path.contains(QPointF(x0 + eps, y0 + eps))      # TL sharp
        assert path.contains(QPointF(x1 - eps, y0 + eps))      # TR sharp
        assert path.contains(QPointF(x0 + eps, y1 - eps))      # BL sharp
        assert not path.contains(QPointF(x1 - eps, y1 - eps))  # BR rounded

    def test_per_corner_radii_reach_dip_silhouette(self, qtbot: QtBot) -> None:
        # The DIP notched silhouette takes the same per-corner radii: the
        # bottom-right corner rounds while the pin-1 notch stays carved.
        from PySide6.QtCore import QPointF
        from protoboard.domain.components.appearance import Appearance
        prism = _dip8_prism()
        body = PackageBody(  # type: ignore[arg-type]
            prism,
            component_class=ComponentClass.IC,
            appearance=Appearance(corner_radius_mm=(0.0, 0.0, 2.0, 0.0)))
        path = _largest_path(
            _flat(body.draw(_dip8_positions(), _RED, _DARK, 1.0, _Z))).path()
        box = prism.bbox()
        x0, y0 = box.x, box.y
        x1, y1 = box.x + box.lx, box.y + box.ly
        eps = 0.08
        assert not path.contains(QPointF(1.5, -0.4))            # notch carved
        assert path.contains(QPointF(x0 + eps, y1 - eps))       # BL sharp
        assert not path.contains(QPointF(x1 - eps, y1 - eps))   # BR rounded


class TestDevboardBody:
    """A devboard renders as a plain PCB rectangle — no DIP notch — even when
    its pins cluster into the two columns that would otherwise be a DIP.  The
    suppression is keyed on ``component_class``, not the pin layout."""

    def test_devboard_has_no_notch(self, qtbot: QtBot) -> None:
        from PySide6.QtCore import QPointF
        body_dev = PackageBody(  # type: ignore[arg-type]
            _dip8_prism(),
            component_class=ComponentClass.DEVBOARD)
        items = _flat(body_dev.draw(_dip8_positions(), _RED, _DARK, 1.0, _Z))
        body = max(
            (it for it in items
             if isinstance(it, (QGraphicsRectItem, QGraphicsPathItem))),
            key=lambda it: it.sceneBoundingRect().width() * it.
            sceneBoundingRect().height(),
        )
        # The point the DIP notch carves out (row midpoint, top edge) is now
        # INSIDE the plain rectangle — no notch was cut.
        if isinstance(body, QGraphicsPathItem):
            assert body.path().contains(QPointF(1.5, -0.4))
        # A plain rect body OR a rounded-rect path, never the notched silhouette.

    def test_ic_same_prism_still_notched(self, qtbot: QtBot) -> None:
        # Control: the very same prism rendered as an IC keeps the DIP notch, so
        # the gate is class-driven (devboard), not a change to the silhouette.
        from PySide6.QtCore import QPointF
        items = _flat(PackageBody(  # type: ignore[arg-type]
            _dip8_prism(),
            component_class=ComponentClass.IC).draw(_dip8_positions(), _RED,
                                                    _DARK, 1.0, _Z))
        body = max(
            (it for it in items if isinstance(it, QGraphicsPathItem)),
            key=lambda it: it.sceneBoundingRect().width(),
        )
        assert not body.path().contains(QPointF(1.5,
                                                -0.4))  # notch still carved

    def test_devboard_dispatches_class_axis_renderer(self, qtbot: QtBot) -> None:
        # The devboard look is a component-class registration
        # (@register_renderer(component_class="devboard")), threaded from the
        # PackageBody constructor through renderer_for — not a hardcoded gate.
        from protoboard.gui.renderers.packages import DevboardRenderer
        body = PackageBody(  # type: ignore[arg-type]
            _dip8_prism(),
            component_class=ComponentClass.DEVBOARD)
        assert type(body) is DevboardRenderer

    def test_pad_active_rings_one_per_pin(self, qtbot: QtBot) -> None:
        # Each pin gets a faint pad-sized ring (the click-active marker) on top
        # of the body; the body itself is one item, so rings == pin count.
        from PySide6.QtWidgets import QGraphicsEllipseItem
        positions = _dip8_positions()
        items = _flat(PackageBody(  # type: ignore[arg-type]
            _dip8_prism(),
            component_class=ComponentClass.DEVBOARD).draw(
                positions, _RED, _DARK, 1.0, _Z))
        rings = [it for it in items if isinstance(it, QGraphicsEllipseItem)]
        assert len(rings) == len(positions)


# ---------------------------------------------------------------------------
# body_shape_for dispatch
# ---------------------------------------------------------------------------


class TestFootprintOptionStyle:
    """A ``footprints:`` entry's ``style:`` colours the body of the placed
    option — instance override > option style > definition style."""

    def _defn(self):
        from protoboard.domain.components.appearance import Appearance
        from protoboard.domain.components.definition import FootprintOption
        return ComponentDefinition(
            name="R",
            component_class=ComponentClass.RESISTOR,
            pin_names={},
            package=_colored_prism("#cdd2d6"),
            footprints=(
                FootprintOption(name="DIN-0207", ref="din-0207/typ"),
                FootprintOption(name="SMD-0805", ref="smd-0805/typ",
                                style=Appearance(color="#222222")),
            ),
        )

    def _families(self):
        from protoboard.domain.components.package.family import (
            PackageFamily,
            PackageVariant,
        )
        fams = {}
        for fam in ("din-0207", "smd-0805"):
            pkg = _colored_prism("#cdd2d6")
            fams[fam] = PackageFamily(
                family_id=fam, label=fam,
                variants=(PackageVariant(variant_id="typ", label=fam,
                                         package=pkg),),
            )
        return fams

    def _instance(self, variant: str | None, appearance=None):
        import uuid as _uuid
        return ComponentInstance(
            id=_uuid.uuid4(), definition_ref="R", refdes="R1", value="10k",
            package_variant=variant, appearance=appearance,
        )

    def test_default_option_without_style_keeps_package_color(self) -> None:
        shape = body_shape_for(self._defn(), instance=self._instance(None),
                               package_families=self._families())
        assert isinstance(shape, PackageBody)
        assert shape._appearance.color is None

    def test_picked_option_style_applies(self) -> None:
        shape = body_shape_for(
            self._defn(), instance=self._instance("smd-0805/typ"),
            package_families=self._families())
        assert isinstance(shape, PackageBody)
        assert shape._appearance.color == "#222222"

    def test_instance_override_beats_option_style(self) -> None:
        from protoboard.domain.components.appearance import Appearance
        shape = body_shape_for(
            self._defn(),
            instance=self._instance("smd-0805/typ",
                                    appearance=Appearance(color="#ff0000")),
            package_families=self._families())
        assert isinstance(shape, PackageBody)
        assert shape._appearance.color == "#ff0000"

    def test_unmatched_variant_gets_no_option_style(self) -> None:
        # An address not present in the options list (e.g. a picker-built kind
        # variant) applies no per-option style.
        shape = body_shape_for(
            self._defn(), instance=self._instance("din-0207/typ"),
            package_families=self._families())
        assert isinstance(shape, PackageBody)
        assert shape._appearance.color is None


class TestBodyShapeFor:

    def test_two_pin_without_package_falls_back_to_generic(self) -> None:
        # No package_family_ref, no instance variant, no inline package.
        defn = ComponentDefinition.from_footprint(
            name="X",
            component_class=ComponentClass.RESISTOR,
            footprint=SIL(n=2),
            pin_names={},
        )
        # The definition always carries a package (the synthesized generic
        # bbox body) — the bare part renders through PackageBody over it.
        from protoboard.domain.components.package.kind import (
            is_generic_package,
        )
        shape = body_shape_for(defn)
        assert isinstance(shape, PackageBody)
        assert is_generic_package(shape._pkg)  # type: ignore[attr-defined]

    def test_bare_footprint_falls_back_to_generic(self) -> None:
        # A footprint is only a pin cloud (count + spacing) — it no longer
        # determines a body.  Without a resolved package, any bare footprint
        # (SIL-3 transistor, DIL-8 IC, …) draws the generic bbox body; a part
        # that wants TO-92 / DIP silhouette declares a package instead (see the
        # TestTO92PackageParity / TestTO220PackageParity / inline-package tests).
        for cls, fp in (
            (ComponentClass.TRANSISTOR_NPN, SIL(n=3)),
            (ComponentClass.IC, DIL(n=8)),
        ):
            defn = ComponentDefinition.from_footprint(name="U1",
                                       component_class=cls,
                                       footprint=fp,
                                       pin_names={})
            from protoboard.domain.components.package.kind import (
                is_generic_package,
            )
            shape = body_shape_for(defn)
            assert isinstance(shape, PackageBody)
            assert is_generic_package(shape._pkg)  # type: ignore[attr-defined]

    def test_devboard_with_inline_package_returns_package_body(self) -> None:
        # A devboard module carries an inline prism package, so it takes the
        # first (parametric package) dispatch arm and renders via PackageBody —
        # which, for the DEVBOARD class, draws a plain rect (no notch).
        from protoboard.domain.components.package.adapter import (
            PackageFootprint,
        )
        from protoboard.domain.components.package.loader import build_package
        pkg = build_package(
            {
                "shape": "prism",
                "dims": {
                    "width": 18.0,
                    "length": 38.1,
                    "height": 1.6
                },
                "bend": 0,
                "leads": {
                    "direction": [0, 0, -1],
                    "body_offset": {
                        "unit": 2.5,
                        "value": [[3, [0, 1]], [7, 0], [3, [0, -1]]],
                    },
                    "board_offset": "default",
                },
            },
            "TEST_DEVBOARD",
        )
        defn = ComponentDefinition(
            name="MOD1",
            component_class=ComponentClass.DEVBOARD,
            package=pkg,
            pin_names={},
        )
        shape = body_shape_for(defn)
        assert isinstance(shape, PackageBody)

    def test_placed_instance_reaches_the_renderer(self) -> None:
        # The renderer keeps the placed (bound) instance — the escape hatch
        # for plugins that draw part identity: instance.value (a resistance
        # for colour bands), instance.definition (part name), refdes.
        import uuid as _uuid
        defn = ComponentDefinition.from_footprint(
            name="R",
            component_class=ComponentClass.RESISTOR,
            footprint=SIL(n=2),
            pin_names={},
        )
        inst = ComponentInstance(
            id=_uuid.uuid4(), definition_ref="R", refdes="R1", value="10k",
        ).bind(defn, {})
        shape = body_shape_for(defn, instance=inst)
        assert isinstance(shape, PackageBody)
        assert shape._instance is inst
        # A bound instance reaches the rest: definition (part name) + value.
        assert inst.definition is defn
        assert inst.value == "10k"

    def test_single_row_footprint_synthesizes_sil_silhouette(self) -> None:
        # A single-column footprint still synthesizes a SIL silhouette when its
        # prism is built explicitly — body_shape_for no longer does this from a
        # bare footprint (it draws GenericBody; a SIL header declares a package),
        # but the prism→silhouette path that PackageBody uses is intact.
        from protoboard.domain.components.package.shapes import Prism
        prism = _prism_from_footprint(SIL(n=6))
        assert isinstance(prism, Prism)
        assert isinstance(silhouette_for(prism),
                          SilSilhouette)

    def test_generic_fallback_for_large_connector(self) -> None:
        # 10-pin SIL connector — no special shape.
        defn = _make_defn(ComponentClass.CONNECTOR, n_pins=10, fp_type="SIL")
        shape = body_shape_for(defn)
        assert isinstance(shape, BodyShape)

    def test_placement_vflipped_passed_through(self) -> None:
        defn = _make_defn(ComponentClass.DIODE, n_pins=2, fp_type="TwoPin")
        pl_normal = _make_placement()
        pl_flipped = _make_placement()
        pl_flipped.vertical_flipped = True
        # Both should return a valid BodyShape without error.
        s1 = body_shape_for(defn, placement=pl_normal)
        s2 = body_shape_for(defn, placement=pl_flipped)
        assert isinstance(s1, BodyShape)
        assert isinstance(s2, BodyShape)


# ---------------------------------------------------------------------------
# PackageBody — parametric package geometry
# ---------------------------------------------------------------------------


class TestPackageBodyDispatch:

    def test_package_wins_over_legacy_footprint(self) -> None:
        # A catalogue package on the definition takes precedence over the prism
        # synthesized from the footprint (both render via PackageBody, but the
        # catalogue geometry wins).
        defn = ComponentDefinition(
            name="U1",
            component_class=ComponentClass.IC,
            pin_names={},
            package=_prism_package(),  # type: ignore[arg-type]
        )
        shape = body_shape_for(defn)
        assert isinstance(shape, PackageBody)
        assert shape._pkg is not None  # type: ignore[attr-defined]

    def test_unsupported_footprint_falls_to_generic(self) -> None:
        # A footprint with no resolved package keeps the GenericBody bbox path.
        # (Rebuilt from the former GridFootprint(rows=2, cols=3) as an explicit
        # 2×3 pin grid via GenericFootprint — the removed grid factory had no
        # replacement.)
        from protoboard.geometry import Vector
        grid = GenericFootprint([
            Vector(0.0, 1.0),  # (0,0)->(0,1)
            Vector(0.0, 1.0),  # (0,1)->(0,2)
            Vector(1.0, -2.0),  # (0,2)->(1,0)
            Vector(0.0, 1.0),  # (1,0)->(1,1)
            Vector(0.0, 1.0),  # (1,1)->(1,2)
        ])
        defn = ComponentDefinition.from_footprint(
            name="J1",
            component_class=ComponentClass.CONNECTOR,
            footprint=grid,
            pin_names={},
        )
        from protoboard.domain.components.package.kind import (
            is_generic_package,
        )
        shape = body_shape_for(defn)
        assert isinstance(shape, PackageBody)
        assert is_generic_package(shape._pkg)  # type: ignore[attr-defined]

    def _families(self) -> dict:
        from protoboard.domain.components.package import (
            PackageFamily,
            PackageVariant,
        )
        return {
            "DO-35":
            PackageFamily(
                family_id="DO-35",
                variants=(
                    PackageVariant(
                        "typ", "typ",
                        _cylinder_package()),  # type: ignore[arg-type]
                ),
            ),
        }

    def test_family_ref_resolves_to_package_body(self) -> None:
        defn = ComponentDefinition.from_footprint(
            name="D1",
            component_class=ComponentClass.DIODE,
            footprint=SIL(n=2),
            pin_names={},
            package_family_ref="DO-35/typ",
        )
        shape = body_shape_for(defn, package_families=self._families())
        assert isinstance(shape, PackageBody)

    def test_instance_variant_resolves_to_package_body(self) -> None:
        defn = ComponentDefinition.from_footprint(
            name="D1",
            component_class=ComponentClass.DIODE,
            footprint=SIL(n=2),
            pin_names={},
        )
        inst = ComponentInstance.new(
            definition_ref="D1",
            refdes="D1",
            value="",
            package_variant="DO-35/typ",
        )
        shape = body_shape_for(
            defn,
            instance=inst,
            package_families=self._families(),
        )
        assert isinstance(shape, PackageBody)

    def test_stale_ref_without_inline_falls_to_legacy(self) -> None:
        defn = ComponentDefinition.from_footprint(
            name="D1",
            component_class=ComponentClass.DIODE,
            footprint=SIL(n=2),
            pin_names={},
            package_family_ref="NOPE/x",
        )
        # The stale ref falls through to the definition default — the
        # synthesized generic bbox body, not a catalogue silhouette.
        from protoboard.domain.components.package.kind import (
            is_generic_package,
        )
        shape = body_shape_for(defn, package_families=self._families())
        assert isinstance(shape, PackageBody)
        assert is_generic_package(shape._pkg)  # type: ignore[attr-defined]


class TestPackageBodyDraw:

    def test_cylinder_draws_body_and_leads(self, qtbot: QtBot) -> None:
        shape = PackageBody(_cylinder_package())  # type: ignore[arg-type]
        # Pads spaced wider than the body → leads appear.
        items = _flat(shape.draw(_pts((0, 0), (4, 0)), _RED, _DARK, 1.0, _Z))
        assert any(isinstance(it, QGraphicsPathItem) for it in items)
        assert any(isinstance(it, QGraphicsLineItem) for it in items)

    def test_cylinder_edge_radius_zero_is_sharp_rectangle(
        self,
        qtbot: QtBot,
    ) -> None:
        # Default edge_radius=0 → the body rectangle's corner is inside the
        # path (sharp corner), not carved away.
        shape = PackageBody(
            _cylinder_package(edge_radius=0.0))  # type: ignore[arg-type]
        items = _flat(shape.draw(_pts((0, 0), (4, 0)), _RED, _DARK, 1.0, _Z))
        body = [it for it in items if isinstance(it, QGraphicsPathItem)][-1]
        # Local body rect is (-len/2,-w/2, len, w) = (-1.26,-0.5, 2.52, 1.0).
        # Its corner (-1.26, -0.5) belongs to a sharp rectangle.
        from PySide6.QtCore import QPointF
        assert body.path().contains(QPointF(-1.26 + 0.02, -0.5 + 0.02))

    def test_cylinder_full_edge_radius_carves_corner(self,
                                                     qtbot: QtBot) -> None:
        # edge_radius = diameter/2 → capsule: the rectangle corner is carved
        # away (outside the rounded path).
        shape = PackageBody(
            _cylinder_package(edge_radius=1.25))  # type: ignore[arg-type]
        items = _flat(shape.draw(_pts((0, 0), (4, 0)), _RED, _DARK, 1.0, _Z))
        body = [it for it in items if isinstance(it, QGraphicsPathItem)][-1]
        from PySide6.QtCore import QPointF
        assert not body.path().contains(QPointF(-1.26 + 0.02, -0.5 + 0.02))

    def test_cylinder_short_spacing_renders_upright(self, qtbot: QtBot) -> None:
        shape = PackageBody(_cylinder_package())  # type: ignore[arg-type]
        # Pads closer than the body length → the part stands upright: it renders
        # as the radial top-view (a near-circular disc body + a lead stub to the
        # second pad), NOT a side-view capsule spanning the gap. (Previously this
        # drew a full-length horizontal capsule even on adjacent pads.)
        items = _flat(shape.draw(_pts((0, 0), (1, 0)), _RED, _DARK, 1.0, _Z))
        # A round body (ellipse, or a path for a D-shaped/polar body).
        assert any(isinstance(it, (QGraphicsEllipseItem, QGraphicsPathItem))
                   for it in items)
        # And a lead stub from the upright body to the adjacent pad.
        assert any(isinstance(it, QGraphicsLineItem) for it in items)
        # The body spans about the diameter (1 hole), far less than body_len.
        mm = DEFAULT_THEME.raster_pitch_mm
        body_len = _cylinder_package().length_mm / mm  # type: ignore[attr-defined]
        body = max(
            (it for it in items if isinstance(
                it, (QGraphicsEllipseItem, QGraphicsPathItem))),
            key=lambda it: it.sceneBoundingRect().width(),
        )
        assert body.sceneBoundingRect().width() < body_len - 1e-6

    def test_cylinder_coincident_pins_falls_back(self, qtbot: QtBot) -> None:
        shape = PackageBody(_cylinder_package())  # type: ignore[arg-type]
        items = _flat(shape.draw(_pts((0, 0), (0, 0)), _RED, _DARK, 1.0, _Z))
        assert items  # degenerate spacing → bbox rectangle, not a crash

    def test_prism_draws_dip_silhouette(self, qtbot: QtBot) -> None:
        # A two-column prism is a DIP: a notched body path plus a pin-1 marker.
        shape = PackageBody(_prism_package())  # type: ignore[arg-type]
        pts = _pts(*([(0, i) for i in range(4)] + [(3, 3 - i)
                                                   for i in range(4)]))
        items = _flat(shape.draw(pts, _RED, _DARK, 1.0, _Z))
        assert all(isinstance(it, QGraphicsPathItem) for it in items)
        assert len(items) == 2  # body + pin-1 marker

    def test_empty_positions_returns_no_items(self, qtbot: QtBot) -> None:
        shape = PackageBody(_cylinder_package())  # type: ignore[arg-type]
        assert _flat(shape.draw([], _RED, _DARK, 1.0, _Z)) == []

    def test_z_value_is_set(self, qtbot: QtBot) -> None:
        shape = PackageBody(_prism_package())  # type: ignore[arg-type]
        pts = _pts(*([(0, i) for i in range(4)] + [(3, 3 - i)
                                                   for i in range(4)]))
        items = _flat(shape.draw(pts, _RED, _DARK, 1.0, _Z))
        assert items[0].zValue() == _Z


class TestSmdChipBody:
    """A two-pin prism renders as a flat SMD chip centred on the pad pair."""

    def test_no_dip_silhouette_for_two_pin_prism(self) -> None:
        # A two-pin (two single-pin columns) prism is not a DIP — no notch.
        assert silhouette_for(
            _smd_chip_package()) is None  # type: ignore[arg-type]

    def test_single_body_no_marker_no_leads(self, qtbot: QtBot) -> None:
        items = _flat(PackageBody(
            _smd_chip_package()).draw(  # type: ignore[arg-type]
                _pts((0, 0), (1, 0)), _RED, _DARK, 1.0, _Z))
        # One body path; no pin-1 marker and no lead lines (a chip has neither).
        assert len(items) == 1
        assert isinstance(items[0], QGraphicsPathItem)
        assert not any(isinstance(it, QGraphicsLineItem) for it in items)

    def test_body_centred_on_pad_midpoint(self, qtbot: QtBot) -> None:
        # Pads at (0,0)-(2,0): the chip body is centred at the midpoint (1,0),
        # not anchored at pin 1.
        items = _flat(PackageBody(
            _smd_chip_package()).draw(  # type: ignore[arg-type]
                _pts((0, 0), (2, 0)), _RED, _DARK, 1.0, _Z))
        c = items[0].sceneBoundingRect().center()
        assert (round(c.x(), 2), round(c.y(), 2)) == (1.0, 0.0)

    def test_smd_lineage_adds_metallised_caps(self, qtbot: QtBot) -> None:
        # A catalogue chip (lineage carries the ``prism_smd`` generic) draws
        # through SmdChipRenderer: the base outline plus a light metallised
        # terminal band at each end, stacked just above the body.
        import dataclasses

        from protoboard.domain.components.package.shapes import Prism
        base = _smd_chip_package()
        assert isinstance(base, Prism)
        pkg = dataclasses.replace(base, lineage=("prism", "prism_smd"))
        items, _labels = PackageBody(pkg).draw(
            _pts((0, 0), (1, 0)), _RED, _DARK, 1.0, _Z)
        assert len(items) == 3           # body + 2 caps
        body, cap_a, cap_b = items
        assert cap_a.zValue() > body.zValue()
        assert cap_a.zValue() == cap_b.zValue()
        # Caps sit at the body ends, symmetric about the pad midpoint (0.5, 0).
        ca = cap_a.sceneBoundingRect().center()
        cb = cap_b.sceneBoundingRect().center()
        assert round(ca.x() + cb.x(), 2) == 1.0
        assert ca.x() != cb.x()

    def test_plain_two_pin_prism_keeps_single_body(self, qtbot: QtBot) -> None:
        # Without the ``prism_smd`` lineage the base renderer draws exactly
        # the historical single chip rectangle — no caps (parity guard).
        items = _flat(PackageBody(
            _smd_chip_package()).draw(  # type: ignore[arg-type]
                _pts((0, 0), (1, 0)), _RED, _DARK, 1.0, _Z))
        assert len(items) == 1

    def test_dimless_two_pin_prism_degrades_to_generic(
        self, qtbot: QtBot,
    ) -> None:
        # Regression: a two-pin prism with neither dims nor a margin used to
        # crash the SMD-chip branch (lead_axis_extents → bbox() ValueError,
        # which only the bbox fallback caught).  It must degrade to the
        # generic pin-cloud rectangle instead — rendering never blocks.
        from protoboard.domain.components.package.loader import build_package
        pkg = build_package(
            {
                "shape": "prism",
                "dims": {},
                "leads": {
                    "direction": [0, 0, -1],
                    "body_offset": {"unit": 2.5, "value": [[1, 0]]},
                    "board_offset": "default",
                },
            },
            "DIMLESS_CHIP",
        )
        items = _flat(PackageBody(pkg).draw(  # type: ignore[arg-type]
            _pts((0, 0), (1, 0)), _RED, _DARK, 1.0, _Z))
        assert items  # generic bbox body, not a ValueError
        # The prism_smd caps overlay must survive the same degenerate package.
        import dataclasses

        from protoboard.domain.components.package.shapes import Prism
        assert isinstance(pkg, Prism)
        smd = dataclasses.replace(pkg, lineage=("prism", "prism_smd"))
        assert PackageBody(smd).draw(
            _pts((0, 0), (1, 0)), _RED, _DARK, 1.0, _Z)

    def test_body_shorter_than_pad_span(self, qtbot: QtBot) -> None:
        # A real chip sits *between* its pads at its declared length, shorter
        # than the hole pitch — it does not stretch to reach the pad centres.
        # The body covers the midpoint but leaves the terminals (pad centres)
        # outside, so the metallised pads stay visible at each end.
        from PySide6.QtCore import QPointF
        items = _flat(PackageBody(
            _smd_chip_package()).draw(  # type: ignore[arg-type]
                _pts((0, 0), (1, 0)), _RED, _DARK, 1.0, _Z))
        body = items[0]
        assert isinstance(body, QGraphicsPathItem)
        place = body.sceneTransform()
        midpoint, pads = QPointF(0.5, 0.0), (QPointF(0.0,
                                                     0.0), QPointF(1.0, 0.0))
        # Map scene points into the body's local frame, then test coverage.
        assert body.path().contains(place.inverted()[0].map(midpoint))
        for pad in pads:
            assert not body.path().contains(place.inverted()[0].map(pad))

    def test_diagonal_placement_aligns_to_pad_axis(self, qtbot: QtBot) -> None:
        # Diagonally-adjacent pads → the body rotates to the 45° pad axis and
        # still centres on the midpoint.
        items = _flat(PackageBody(
            _smd_chip_package()).draw(  # type: ignore[arg-type]
                _pts((0, 0), (1, 1)), _RED, _DARK, 1.0, _Z))
        c = items[0].sceneBoundingRect().center()
        assert (round(c.x(), 2), round(c.y(), 2)) == (0.5, 0.5)


class TestPackageBodyColor:
    """Body presentation (colour / edge) is read from the package."""

    def _body_item(self, items: list) -> QAbstractGraphicsShapeItem:
        # The body fill lives on the largest path/rect item — the silhouette /
        # capsule — not the small pin-1 marker that a prism also emits.
        bodies = [
            it for it in items
            if isinstance(it, (QGraphicsPathItem, QGraphicsRectItem))
        ]
        return max(
            bodies,
            key=lambda it:
            (it.sceneBoundingRect().width() * it.sceneBoundingRect().height()),
        )

    def test_cylinder_uses_package_color(self, qtbot: QtBot) -> None:
        shape = PackageBody(
            _colored_cylinder_package("#d6c08c"))  # type: ignore[arg-type]
        items = _flat(shape.draw(_pts((0, 0), (4, 0)), _RED, _DARK, 1.0, _Z))
        body = self._body_item(items)
        assert body.brush().color().name(
        ) == "#d6c08c"  # type: ignore[attr-defined]

    def test_prism_uses_package_color(self, qtbot: QtBot) -> None:
        shape = PackageBody(
            _colored_prism_package("#abcdef"))  # type: ignore[arg-type]
        pts = _pts(*([(0, i) for i in range(4)] + [(3, 3 - i)
                                                   for i in range(4)]))
        items = _flat(shape.draw(pts, _RED, _DARK, 1.0, _Z))
        body = self._body_item(items)
        assert body.brush().color().name(
        ) == "#abcdef"  # type: ignore[attr-defined]

    def test_no_color_falls_back_to_caller_fill(self, qtbot: QtBot) -> None:
        shape = PackageBody(_cylinder_package())  # type: ignore[arg-type]
        items = _flat(shape.draw(_pts((0, 0), (4, 0)), QColor("#404858"), _DARK,
                                 1.0, _Z))
        body = self._body_item(items)
        assert body.brush().color().name(
        ) == "#404858"  # type: ignore[attr-defined]

    def test_border_is_darker_than_fill(self, qtbot: QtBot) -> None:
        shape = PackageBody(
            _colored_cylinder_package("#d6c08c"))  # type: ignore[arg-type]
        items = _flat(shape.draw(_pts((0, 0), (4, 0)), _RED, _DARK, 1.0, _Z))
        body = self._body_item(items)
        fill = body.brush().color()  # type: ignore[attr-defined]
        pen = body.pen().color()  # type: ignore[attr-defined]
        assert pen.lightnessF() < fill.lightnessF()

    def test_sharp_edge_squares_prism_corners(self, qtbot: QtBot) -> None:
        # A bbox prism (no DIP/SIL silhouette) honours edge style: corner_radius
        # is declared but edge=sharp forces 0 → a square (non-rounded) body path.
        # (The body is a placed QPainterPath so it rotates with the pins; square
        # vs rounded is a path-geometry distinction, not a Rect-vs-Path type one.)
        shape = PackageBody(
            _bbox_prism_package(edge="sharp"))  # type: ignore[arg-type]
        items = _flat(
            shape.draw(_bbox_prism_positions(), _RED, _DARK, 1.0, _Z))
        body = self._body_item(items)
        assert isinstance(body, QGraphicsPathItem)
        assert not _path_is_rounded(body)

    def test_rounded_edge_rounds_prism_corners(self, qtbot: QtBot) -> None:
        shape = PackageBody(
            _bbox_prism_package(edge="rounded"))  # type: ignore[arg-type]
        items = _flat(
            shape.draw(_bbox_prism_positions(), _RED, _DARK, 1.0, _Z))
        assert isinstance(self._body_item(items), QGraphicsPathItem)


def _axial_cyl_package(edge_radius: float = 0.0) -> Package:
    return build_package(
        {
            "shape": "cylinder",
            "dims": {
                "diameter": 2.5,
                "length": 6.3,
                "edge_radius": edge_radius
            },
            "arrangement": "axial",
            "bend": 0,
            "leads": {
                "direction": [[0, 0, -1], [0, 0, 1]],
                "body_offset": {
                    "unit": 2.5,
                    "value": [[4, 0]]
                },
                "board_offset": "default",
                "lead_r": 0.27,
            },
        },
        "AX",
    )


def _radial_cyl_package() -> Package:
    return build_package(
        {
            "shape": "cylinder",
            "dims": {
                "diameter": 6.3,
                "length": 11.0
            },
            "arrangement": "radial",
            "bend": 4,
            "leads": {
                "direction": [[0, 0, -1], [0, 0, -1]],
                "body_offset": {
                    "unit": 2.5,
                    "value": [[1, 0]]
                },
                "board_offset": "default",
                "lead_r": 0.25,
            },
        },
        "RAD",
    )


def _radial_prism_package() -> Package:
    return build_package(
        {
            "shape": "prism",
            "dims": {
                "width": 4.5,
                "length": 11.0,
                "height": 3.5
            },
            "arrangement": "radial",
            "bend": 0,
            "leads": {
                "direction": [[0, 0, -1], [0, 0, -1]],
                "body_offset": {
                    "unit": 2.5,
                    "value": [[1, 0]]
                },
                "board_offset": "default",
                "lead_r": 0.25,
            },
        },
        "RADP",
    )


class TestPackageBodyParity:
    """2-pin visual parity with the legacy TwoPinParametricBody."""

    def test_axial_polar_draws_cathode_stripe(self, qtbot: QtBot) -> None:
        # Diode → capsule + stripe = two path items; plain part = one.
        diode = PackageBody(
            _axial_cyl_package(),  # type: ignore[arg-type]
            component_class=ComponentClass.DIODE,
        )
        plain = PackageBody(_axial_cyl_package())  # type: ignore[arg-type]
        d_paths = sum(
            isinstance(it, QGraphicsPathItem)
            for it in _flat(diode.draw(_pts((0, 0), (4, 0)), _RED, _DARK, 1.0,
                                       _Z)))
        p_paths = sum(
            isinstance(it, QGraphicsPathItem)
            for it in _flat(plain.draw(_pts((0, 0), (4, 0)), _RED, _DARK, 1.0,
                                       _Z)))
        assert d_paths == p_paths + 1

    @staticmethod
    def _upright_body(shape: PackageBody, p1: tuple[float, float],
                      p2: tuple[float, float]) -> QGraphicsItem:
        items = _flat(shape.draw(_pts(p1, p2), _RED, _DARK, 1.0, _Z))
        return max(
            (it for it in items if isinstance(
                it, (QGraphicsPathItem, QGraphicsEllipseItem))),
            key=lambda it: it.sceneBoundingRect().width(),
        )

    def test_axial_upright_body_sits_over_anchor_pad(
        self, qtbot: QtBot
    ) -> None:
        # An axial cylinder whose pads are CLOSER than its body length stands
        # UPRIGHT, the body sitting directly OVER ONE pad (the anchor), the bent
        # lead reaching the other — NOT centred between the pads (that is the
        # radial centroid silhouette, a different mount). User rule 2026-06-28.
        pkg = _axial_cyl_package()  # diameter 2.5, length 6.3 → body_len 2.52
        shape = PackageBody(pkg)  # type: ignore[arg-type]
        mm = DEFAULT_THEME.raster_pitch_mm
        body_len = pkg.length_mm / mm   # type: ignore[attr-defined]
        diam = pkg.diameter_mm / mm     # type: ignore[attr-defined]

        body = self._upright_body(shape, (0, 0), (1, 0))
        c = body.sceneBoundingRect().center()
        # Body is over the anchor pad (pin 1 at the origin), NOT the midpoint.
        assert (c.x(), c.y()) == pytest.approx((0.0, 0.0), abs=0.05)
        # And it spans ~the diameter, far less than the flat body length.
        assert body.sceneBoundingRect().width() == pytest.approx(diam, abs=0.2)
        assert body.sceneBoundingRect().width() < body_len - 1e-6

    def test_axial_upright_flip_moves_body_to_other_pad(
        self, qtbot: QtBot
    ) -> None:
        # Flip (vertical_flipped) chooses WHICH pad the upright body stands on:
        # unflipped → over pin 1; flipped → over pin 2.
        pkg = _axial_cyl_package()
        unflipped = PackageBody(pkg)  # type: ignore[arg-type]
        flipped = PackageBody(
            pkg, vertical_flipped=True)  # type: ignore[arg-type]
        cu = self._upright_body(unflipped, (0, 0), (1, 0)).sceneBoundingRect()
        cf = self._upright_body(flipped, (0, 0), (1, 0)).sceneBoundingRect()
        assert (cu.center().x(), cu.center().y()) == pytest.approx(
            (0.0, 0.0), abs=0.05)   # over pin 1
        assert (cf.center().x(), cf.center().y()) == pytest.approx(
            (1.0, 0.0), abs=0.05)   # over pin 2

    def test_axial_flat_body_spans_more_than_upright(
        self, qtbot: QtBot
    ) -> None:
        shape = PackageBody(_axial_cyl_package())  # type: ignore[arg-type]
        up = self._upright_body(shape, (0, 0), (1, 0)).sceneBoundingRect()
        flat = self._upright_body(shape, (0, 0), (4, 0)).sceneBoundingRect()
        assert flat.width() > up.width() + 1e-6

    def test_cathode_stripe_stays_on_flat_part_of_capsule(
        self,
        qtbot: QtBot,
    ) -> None:
        # On a fully-rounded capsule (edge_radius = D/2) the stripe must sit on
        # the straight segment, never on the curved end cap: its outer edge is
        # at most body_len/2 - r from the centre.
        from protoboard.domain.components.package.shapes import Cylinder
        pkg = _axial_cyl_package(edge_radius=1.25)  # D=2.5 → r = D/2
        assert isinstance(pkg, Cylinder)
        shape = PackageBody(pkg, component_class=ComponentClass.DIODE)
        items = _flat(shape.draw(_pts((0, 0), (8, 0)), _RED, _DARK, 1.0, _Z))
        body, stripe = [
            it for it in items if isinstance(it, QGraphicsPathItem)
        ][0], [it for it in items if isinstance(it, QGraphicsPathItem)][-1]
        mm = DEFAULT_THEME.raster_pitch_mm
        body_len = pkg.length_mm / mm
        r = min(pkg.edge_radius_mm / mm,
                body_len / 2.0)
        mid_x = 4.0  # midpoint of (0,0)-(8,0)
        stripe_right = stripe.sceneBoundingRect().right() - mid_x
        assert stripe_right <= body_len / 2.0 - r + 1e-6
        # And the stripe's near corner lies inside the body silhouette.
        from PySide6.QtCore import QPointF
        sbb = stripe.sceneBoundingRect()
        assert body.path().contains(QPointF(sbb.right() - mid_x, 0.0))

    def test_radial_emits_disc_and_lead(self, qtbot: QtBot) -> None:
        shape = PackageBody(_radial_cyl_package())  # type: ignore[arg-type]
        items = _flat(shape.draw(_pts((0, 0), (1, 0)), _RED, _DARK, 1.0, _Z))
        assert any(isinstance(it, QGraphicsEllipseItem) for it in items)
        assert any(isinstance(it, QGraphicsLineItem) for it in items)

    def test_radial_cylinder_centred_between_pads(self, qtbot: QtBot) -> None:
        # The radial disc sits at the geometric midpoint of the pads, not on
        # the anchor pad.
        shape = PackageBody(_radial_cyl_package())  # type: ignore[arg-type]
        items = _flat(shape.draw(_pts((0, 0), (1, 0)), _RED, _DARK, 1.0, _Z))
        disc = next(it for it in items if isinstance(it, QGraphicsEllipseItem))
        c = disc.rect().center()
        assert (round(c.x(), 3), round(c.y(), 3)) == (0.5, 0.0)

    def test_radial_polar_marks_minus_with_segment(self, qtbot: QtBot) -> None:
        # Polar radial part: a dark circular-segment marker on the negative
        # lead (pin 2) side, plus the body disc.  Pads (0,0)-(2,0): pin 2 at
        # (2,0), midpoint (1,0) → the segment sits on the x>1 side.
        shape = PackageBody(
            _radial_cyl_package(),  # type: ignore[arg-type]
            component_class=ComponentClass.CAPACITOR_POLAR,
        )
        items = _flat(shape.draw(_pts((0, 0), (2, 0)), _RED, _DARK, 1.0, _Z))
        seg = next(it for it in items if isinstance(it, QGraphicsPathItem))
        assert seg.sceneBoundingRect().center().x() > 1.0

    def test_radial_polar_segment_follows_physical_cathode(
        self,
        qtbot: QtBot,
    ) -> None:
        # vertical_flipped does not move the physical cathode (pin 2), so the
        # segment stays on the pin-2 side regardless.
        shape = PackageBody(
            _radial_cyl_package(),  # type: ignore[arg-type]
            component_class=ComponentClass.CAPACITOR_POLAR,
            vertical_flipped=True,
        )
        items = _flat(shape.draw(_pts((0, 0), (2, 0)), _RED, _DARK, 1.0, _Z))
        seg = next(it for it in items if isinstance(it, QGraphicsPathItem))
        assert seg.sceneBoundingRect().center().x() > 1.0

    def test_radial_nonpolar_has_no_segment(self, qtbot: QtBot) -> None:
        shape = PackageBody(
            _radial_cyl_package(),  # type: ignore[arg-type]
            component_class=ComponentClass.OTHER,
        )
        items = _flat(shape.draw(_pts((0, 0), (2, 0)), _RED, _DARK, 1.0, _Z))
        assert not any(isinstance(it, QGraphicsPathItem) for it in items)
        assert sum(isinstance(it, QGraphicsEllipseItem) for it in items) == 1

    def _disc(self, profile: str) -> Package:
        return build_package(
            {
                "shape": "disc",
                "dims": {
                    "diameter": 5.0,
                    "thickness": 2.0
                },
                "arrangement": "radial",
                "disc_profile": profile,
                "bend": 0,
                "leads": {
                    "direction": [[0, 0, -1], [0, 0, -1]],
                    "body_offset": {
                        "unit": 2.5,
                        "value": [[1, 0]]
                    },
                    "board_offset": "default",
                    "lead_r": 0.25,
                },
            },
            "D",
        )

    def test_disc_drawn_as_side_lens_not_face_circle(self,
                                                     qtbot: QtBot) -> None:
        # A disc mounts on its edge → a thin side lens (path), not the round
        # face (ellipse); thickness across, diameter along the lead axis.
        shape = PackageBody(self._disc("candy"))  # type: ignore[arg-type]
        items = _flat(shape.draw(_pts((0, 0), (1, 0)), _RED, _DARK, 1.0, _Z))
        lens = next(it for it in items if isinstance(it, QGraphicsPathItem))
        assert not any(isinstance(it, QGraphicsEllipseItem) for it in items)
        br = lens.path().boundingRect()
        # diameter 5 mm = 2 holes long, thickness 2 mm = 0.8 holes across.
        assert round(br.width(), 1) == 2.0
        assert round(br.height(), 1) == 0.8

    def test_disc_candy_has_sharp_tips_flat_is_rounded(self,
                                                       qtbot: QtBot) -> None:
        from PySide6.QtCore import QPointF
        candy = _flat(PackageBody(
            self._disc("candy")).draw(  # type: ignore[arg-type]
                _pts((0, 0), (1, 0)), _RED, _DARK, 1.0, _Z))
        flat = _flat(PackageBody(self._disc("flat")).draw(  # type: ignore[arg-type]
            _pts((0, 0), (1, 0)), _RED, _DARK, 1.0, _Z))
        candy_path = next(it for it in candy
                          if isinstance(it, QGraphicsPathItem)).path()
        flat_path = next(it for it in flat
                         if isinstance(it, QGraphicsPathItem)).path()
        # Near a tip but off-axis: the candy lens has tapered there, the flat
        # ellipse still carries material.  (path is in the local body frame,
        # length -1..+1 along x, so 0.7 is near the +x tip.)
        probe = QPointF(0.70, 0.25)
        assert not candy_path.contains(probe)
        assert flat_path.contains(probe)

    def test_radial_prism_centred_between_pads(self, qtbot: QtBot) -> None:
        shape = PackageBody(_radial_prism_package())  # type: ignore[arg-type]
        items = _flat(shape.draw(_pts((0, 0), (1, 0)), _RED, _DARK, 1.0, _Z))
        body = next(it for it in items if isinstance(it, QGraphicsPathItem))
        c = body.sceneBoundingRect().center()
        assert (round(c.x(), 3), round(c.y(), 3)) == (0.5, 0.0)

    def test_radial_body_covers_close_pins(self, qtbot: QtBot) -> None:
        # D=6.3 mm (~2.52 holes, r~1.26).  At 1-hole spacing the body is
        # centred at (0.5, 0) and covers both pads.
        shape = PackageBody(_radial_cyl_package())  # type: ignore[arg-type]
        assert shape.drawn_pin_numbers(_pts((0, 0),
                                            (1, 0))) == frozenset({1, 2})

    def test_radial_body_clears_wide_pins(self, qtbot: QtBot) -> None:
        # At 4-hole spacing the midpoint (2, 0) is farther than the radius from
        # either pad → the body covers neither.
        shape = PackageBody(_radial_cyl_package())  # type: ignore[arg-type]
        assert shape.drawn_pin_numbers(_pts((0, 0), (4, 0))) == frozenset()

    def test_axial_arrangement_no_drawn_pins(self, qtbot: QtBot) -> None:
        shape = PackageBody(_axial_cyl_package())  # type: ignore[arg-type]
        assert shape.drawn_pin_numbers(_pts((0, 0), (4, 0))) == frozenset()


# ---------------------------------------------------------------------------
# BodyShape interface contract
# ---------------------------------------------------------------------------


class TestBodyShapeContract:
    """Every shape must honour BodyShape.draw → (body_items, label_items)."""

    @pytest.mark.parametrize("shape_factory,positions", [
        (lambda: GenericBody(), _pts((0, 0), (2, 0))),
        (lambda: PackageBody(_cylinder_package()), _pts((0, 0), (4, 0))),
        (lambda: PackageBody(_radial_cyl_package()), _pts((0, 0), (1, 0))),
        (lambda: PackageBody(_dip8_prism()),
         _pts(*([(0, i) for i in range(4)] + [(3, 3 - i) for i in range(4)]))),
        (lambda: PackageBody(_sil6_prism()), _pts(*[(0, i)
                                                    for i in range(6)])),
    ])
    def test_draw_returns_list_of_qgraphicsitem(
        self,
        qtbot: QtBot,
        shape_factory,
        positions: list[Point],
    ) -> None:
        body_items, label_items = shape_factory().draw(
            positions, _RED, _DARK, 1.0, _Z)
        assert isinstance(body_items, list)
        assert isinstance(label_items, list)
        for item in body_items + label_items:
            assert isinstance(item, QGraphicsItem)

    @pytest.mark.parametrize("shape_factory,positions", [
        (lambda: GenericBody(), _pts((0, 0), (2, 0))),
        (lambda: PackageBody(_cylinder_package()), _pts((0, 0), (4, 0))),
    ])
    def test_draw_alpha_zero_still_returns_items(
        self,
        qtbot: QtBot,
        shape_factory,
        positions: list[Point],
    ) -> None:
        body_items, label_items = shape_factory().draw(
            positions, _RED, _DARK, 0.0, _Z)
        assert isinstance(body_items, list)
        assert isinstance(label_items, list)

    @pytest.mark.parametrize("shape_factory,positions", [
        (lambda: GenericBody(), _pts((0, 0), (2, 0))),
        (lambda: PackageBody(_cylinder_package()), _pts((0, 0), (4, 0))),
        (lambda: PackageBody(_radial_cyl_package()), _pts((0, 0), (1, 0))),
    ])
    def test_drawn_pin_numbers_returns_frozenset(
        self,
        qtbot: QtBot,
        shape_factory,
        positions: list[Point],
    ) -> None:
        result = shape_factory().drawn_pin_numbers(positions)
        assert isinstance(result, frozenset)
