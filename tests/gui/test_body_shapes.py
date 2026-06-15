# SPDX-License-Identifier: MIT
"""Tests for body_shapes.py — shape dispatch, geometry helpers, item counts."""

from __future__ import annotations

import uuid

import pytest
from pytestqt.qtbot import QtBot

from PySide6.QtGui import QColor
from PySide6.QtWidgets import (
    QGraphicsEllipseItem,
    QGraphicsItem,
    QGraphicsLineItem,
    QGraphicsPathItem,
    QGraphicsRectItem,
)

from protoboard.domain.components.definition import ComponentClass, ComponentDefinition
from protoboard.domain.components.footprints import (
    DualRow,
    SingleRow,
    TO220Footprint,
    TwoPin,
)
from protoboard.domain.components.instance import ComponentInstance
from protoboard.domain.layout.placement import ComponentPlacement
from protoboard.geometry import Point
from protoboard.gui.renderers.body_shapes import (
    BodyShape,
    DIPBody,
    GenericBody,
    PackageBody,
    SvgDIPBody,
    TO92Body,
    TO220Body,
    body_shape_for,
)
from protoboard.domain.components.package.loader import build_package
from protoboard.gui.theme import DEFAULT_THEME


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

_RED = QColor(200, 50, 50)
_DARK = QColor(30, 30, 30)
_Z = 5.0


def _pts(*coords: tuple[float, float]) -> list[Point]:
    return [Point(x, y) for x, y in coords]


def _make_defn(
    cls: ComponentClass,
    n_pins: int,
    fp_type: str = "SIL",
) -> ComponentDefinition:
    if fp_type == "DIP":
        fp: object = DualRow(n_per_row=n_pins // 2)
    elif fp_type == "TwoPin":
        fp = TwoPin()
    else:
        # ``SIL`` test marker — use TwoPin for n=2 (matches loader redirect),
        # SingleRow for n>=3.
        fp = TwoPin() if n_pins == 2 else SingleRow(n=n_pins)
    return ComponentDefinition(
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


def _cylinder_package(edge_radius: float = 0.0) -> object:
    """An axial cylinder (resistor-like) built through the package loader.

    ``edge_radius`` defaults to 0 → a rectangular body; pass diameter/2 for a
    fully-rounded capsule (the glass-diode look)."""
    return build_package(
        {
            "shape": "cylinder",
            "dims": {"diameter": 2.5, "length": 6.3, "edge_radius": edge_radius},
            "bend": 0,
            "leads": {
                "direction": [0, 0, -1],
                "body_offset": {"unit": 2.5, "value": [[4, 0]]},
                "board_offset": "default",
            },
        },
        "TEST_CYL",
    )


def _prism_package() -> object:
    """A DIP-8 style prism built through the package loader."""
    return build_package(
        {
            "shape": "prism",
            "dims": {"width": 7.62, "length": 10.0},
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


def _colored_cylinder_package(color: str = "#d6c08c", edge: str = "rounded") -> object:
    """An axial cylinder declaring an explicit body colour and edge style."""
    return build_package(
        {
            "shape": "cylinder",
            "dims": {"diameter": 2.5, "length": 6.3},
            "bend": 0,
            "color": color,
            "edge": edge,
            "leads": {
                "direction": [0, 0, -1],
                "body_offset": {"unit": 2.5, "value": [[4, 0]]},
                "board_offset": "default",
            },
        },
        "TEST_CYL_COL",
    )


def _colored_prism_package(color: str = "#d6c08c", edge: str = "sharp") -> object:
    return build_package(
        {
            "shape": "prism",
            "dims": {"width": 7.62, "length": 10.0, "corner_radius": 1.0},
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


# ---------------------------------------------------------------------------
# GenericBody
# ---------------------------------------------------------------------------

class TestGenericBody:
    def test_empty_positions_returns_no_items(self, qtbot: QtBot) -> None:
        items = GenericBody().draw([], _RED, _DARK, 1.0, _Z)
        assert items == []

    def test_single_position_returns_rect(self, qtbot: QtBot) -> None:
        items = GenericBody().draw(_pts((0, 0)), _RED, _DARK, 1.0, _Z)
        assert len(items) == 1
        assert isinstance(items[0], QGraphicsRectItem)

    def test_two_pins_horizontal(self, qtbot: QtBot) -> None:
        items = GenericBody().draw(_pts((0, 0), (2, 0)), _RED, _DARK, 1.0, _Z)
        assert len(items) == 1
        rect = items[0]
        assert isinstance(rect, QGraphicsRectItem)
        bbox = rect.rect()
        assert bbox.width() >= 2.0

    def test_z_value_is_set(self, qtbot: QtBot) -> None:
        items = GenericBody().draw(_pts((0, 0)), _RED, _DARK, 1.0, _Z)
        assert items[0].zValue() == pytest.approx(_Z)


# ---------------------------------------------------------------------------
# TO92Body
# ---------------------------------------------------------------------------

class TestTO92Body:
    def test_three_pins_produces_items(self, qtbot: QtBot) -> None:
        items = TO92Body().draw(_pts((0, 0), (1, 0), (2, 0)), _RED, _DARK, 1.0, _Z)
        assert len(items) >= 1

    def test_fewer_than_three_falls_back(self, qtbot: QtBot) -> None:
        items = TO92Body().draw(_pts((0, 0)), _RED, _DARK, 1.0, _Z)
        assert isinstance(items[0], QGraphicsRectItem)


class TestTO220Body:
    def test_three_pins_produces_items(self, qtbot: QtBot) -> None:
        items = TO220Body().draw(_pts((0, 0), (1, 0), (2, 0)), _RED, _DARK, 1.0, _Z)
        assert len(items) >= 1


class TestSvgDIPBody:
    def test_produces_items_for_dip8(self, qtbot: QtBot) -> None:
        pos = _pts(*([(0, i) for i in range(4)] + [(3, 3 - i) for i in range(4)]))
        items = SvgDIPBody(n_per_row=4).draw(pos, _RED, _DARK, 1.0, _Z)
        assert len(items) >= 1

    def test_too_few_pins_falls_back(self, qtbot: QtBot) -> None:
        items = SvgDIPBody(n_per_row=4).draw(_pts((0, 0)), _RED, _DARK, 1.0, _Z)
        assert isinstance(items[0], QGraphicsRectItem)


class TestDIPBody:
    def test_produces_rect_for_8_pins(self, qtbot: QtBot) -> None:
        pos = _pts(*([(0, i) for i in range(4)] + [(3, 3 - i) for i in range(4)]))
        items = DIPBody(4).draw(pos, _RED, _DARK, 1.0, _Z)
        assert len(items) >= 1


# ---------------------------------------------------------------------------
# body_shape_for dispatch
# ---------------------------------------------------------------------------

class TestBodyShapeFor:
    def test_to220_footprint_returns_to220body(self) -> None:
        fp = TO220Footprint(n=3)
        defn = ComponentDefinition(
            name="T1", component_class=ComponentClass.TRANSISTOR_NPN,
            footprint=fp, pin_names={}
        )
        shape = body_shape_for(defn)
        assert isinstance(shape, TO220Body)

    def test_two_pin_without_package_falls_back_to_generic(self) -> None:
        # No package_family_ref, no instance variant, no inline package.
        defn = ComponentDefinition(
            name="X", component_class=ComponentClass.RESISTOR,
            footprint=TwoPin(), pin_names={},
        )
        shape = body_shape_for(defn)
        assert isinstance(shape, GenericBody)

    def test_sil_3_transistor_returns_to92(self) -> None:
        defn = _make_defn(ComponentClass.TRANSISTOR_NPN, n_pins=3, fp_type="SIL")
        shape = body_shape_for(defn)
        assert isinstance(shape, TO92Body)

    def test_dual_row_returns_svg_dip(self) -> None:
        fp = DualRow(n_per_row=4)
        defn = ComponentDefinition(
            name="U1", component_class=ComponentClass.IC,
            footprint=fp, pin_names={}
        )
        shape = body_shape_for(defn)
        assert isinstance(shape, SvgDIPBody)

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
        # A definition that would otherwise dispatch to SvgDIPBody, but
        # carries a package → PackageBody takes precedence.
        defn = ComponentDefinition(
            name="U1", component_class=ComponentClass.IC,
            footprint=DualRow(n_per_row=4), pin_names={},
            package=_prism_package(),  # type: ignore[arg-type]
        )
        shape = body_shape_for(defn)
        assert isinstance(shape, PackageBody)

    def test_no_package_uses_legacy_path(self) -> None:
        defn = ComponentDefinition(
            name="U1", component_class=ComponentClass.IC,
            footprint=DualRow(n_per_row=4), pin_names={},
        )
        shape = body_shape_for(defn)
        assert not isinstance(shape, PackageBody)

    def _families(self) -> dict:
        from protoboard.domain.components.package import (
            PackageFamily, PackageVariant,
        )
        return {
            "DO-35": PackageFamily(
                family_id="DO-35",
                variants=(
                    PackageVariant("typ", "typ", _cylinder_package()),  # type: ignore[arg-type]
                ),
            ),
        }

    def test_family_ref_resolves_to_package_body(self) -> None:
        from protoboard.domain.components.footprints import TwoPin
        defn = ComponentDefinition(
            name="D1", component_class=ComponentClass.DIODE,
            footprint=TwoPin(), pin_names={},
            package_family_ref="DO-35/typ",
        )
        shape = body_shape_for(defn, package_families=self._families())
        assert isinstance(shape, PackageBody)

    def test_instance_variant_resolves_to_package_body(self) -> None:
        from protoboard.domain.components.footprints import TwoPin
        defn = ComponentDefinition(
            name="D1", component_class=ComponentClass.DIODE,
            footprint=TwoPin(), pin_names={},
        )
        inst = ComponentInstance.new(
            definition_ref="D1", refdes="D1", value="",
            package_variant="DO-35/typ",
        )
        shape = body_shape_for(
            defn, instance=inst, package_families=self._families(),
        )
        assert isinstance(shape, PackageBody)

    def test_stale_ref_without_inline_falls_to_legacy(self) -> None:
        from protoboard.domain.components.footprints import TwoPin
        defn = ComponentDefinition(
            name="D1", component_class=ComponentClass.DIODE,
            footprint=TwoPin(), pin_names={},
            package_family_ref="NOPE/x",
        )
        shape = body_shape_for(defn, package_families=self._families())
        assert not isinstance(shape, PackageBody)


class TestPackageBodyDraw:
    def test_cylinder_draws_body_and_leads(self, qtbot: QtBot) -> None:
        shape = PackageBody(_cylinder_package())  # type: ignore[arg-type]
        # Pads spaced wider than the body → leads appear.
        items = shape.draw(_pts((0, 0), (4, 0)), _RED, _DARK, 1.0, _Z)
        assert any(isinstance(it, QGraphicsPathItem) for it in items)
        assert any(isinstance(it, QGraphicsLineItem) for it in items)

    def test_cylinder_edge_radius_zero_is_sharp_rectangle(
        self, qtbot: QtBot,
    ) -> None:
        # Default edge_radius=0 → the body rectangle's corner is inside the
        # path (sharp corner), not carved away.
        shape = PackageBody(_cylinder_package(edge_radius=0.0))  # type: ignore[arg-type]
        items = shape.draw(_pts((0, 0), (4, 0)), _RED, _DARK, 1.0, _Z)
        body = [it for it in items if isinstance(it, QGraphicsPathItem)][-1]
        # Local body rect is (-len/2,-w/2, len, w) = (-1.26,-0.5, 2.52, 1.0).
        # Its corner (-1.26, -0.5) belongs to a sharp rectangle.
        from PySide6.QtCore import QPointF
        assert body.path().contains(QPointF(-1.26 + 0.02, -0.5 + 0.02))

    def test_cylinder_full_edge_radius_carves_corner(self, qtbot: QtBot) -> None:
        # edge_radius = diameter/2 → capsule: the rectangle corner is carved
        # away (outside the rounded path).
        shape = PackageBody(_cylinder_package(edge_radius=1.25))  # type: ignore[arg-type]
        items = shape.draw(_pts((0, 0), (4, 0)), _RED, _DARK, 1.0, _Z)
        body = [it for it in items if isinstance(it, QGraphicsPathItem)][-1]
        from PySide6.QtCore import QPointF
        assert not body.path().contains(QPointF(-1.26 + 0.02, -0.5 + 0.02))

    def test_cylinder_short_spacing_omits_leads(self, qtbot: QtBot) -> None:
        shape = PackageBody(_cylinder_package())  # type: ignore[arg-type]
        # Pads closer than the body length → no separate lead lines.
        items = shape.draw(_pts((0, 0), (1, 0)), _RED, _DARK, 1.0, _Z)
        assert any(isinstance(it, QGraphicsPathItem) for it in items)
        assert not any(isinstance(it, QGraphicsLineItem) for it in items)

    def test_cylinder_coincident_pins_falls_back(self, qtbot: QtBot) -> None:
        shape = PackageBody(_cylinder_package())  # type: ignore[arg-type]
        items = shape.draw(_pts((0, 0), (0, 0)), _RED, _DARK, 1.0, _Z)
        assert items  # degenerate spacing → bbox rectangle, not a crash

    def test_prism_draws_rectangle(self, qtbot: QtBot) -> None:
        shape = PackageBody(_prism_package())  # type: ignore[arg-type]
        pts = _pts(*([(0, i) for i in range(4)] + [(3, 3 - i) for i in range(4)]))
        items = shape.draw(pts, _RED, _DARK, 1.0, _Z)
        assert len(items) == 1
        assert isinstance(items[0], (QGraphicsRectItem, QGraphicsPathItem))

    def test_empty_positions_returns_no_items(self, qtbot: QtBot) -> None:
        shape = PackageBody(_cylinder_package())  # type: ignore[arg-type]
        assert shape.draw([], _RED, _DARK, 1.0, _Z) == []

    def test_z_value_is_set(self, qtbot: QtBot) -> None:
        shape = PackageBody(_prism_package())  # type: ignore[arg-type]
        pts = _pts(*([(0, i) for i in range(4)] + [(3, 3 - i) for i in range(4)]))
        items = shape.draw(pts, _RED, _DARK, 1.0, _Z)
        assert items[0].zValue() == _Z


class TestPackageBodyColor:
    """Body presentation (colour / edge) is read from the package."""

    def _body_item(self, items: list) -> object:
        # The capsule / rect carrying the body fill is the last path/rect item.
        bodies = [
            it for it in items
            if isinstance(it, (QGraphicsPathItem, QGraphicsRectItem))
        ]
        return bodies[-1]

    def test_cylinder_uses_package_color(self, qtbot: QtBot) -> None:
        shape = PackageBody(_colored_cylinder_package("#d6c08c"))  # type: ignore[arg-type]
        items = shape.draw(_pts((0, 0), (4, 0)), _RED, _DARK, 1.0, _Z)
        body = self._body_item(items)
        assert body.brush().color().name() == "#d6c08c"  # type: ignore[attr-defined]

    def test_prism_uses_package_color(self, qtbot: QtBot) -> None:
        shape = PackageBody(_colored_prism_package("#abcdef"))  # type: ignore[arg-type]
        pts = _pts(*([(0, i) for i in range(4)] + [(3, 3 - i) for i in range(4)]))
        items = shape.draw(pts, _RED, _DARK, 1.0, _Z)
        body = self._body_item(items)
        assert body.brush().color().name() == "#abcdef"  # type: ignore[attr-defined]

    def test_no_color_falls_back_to_caller_fill(self, qtbot: QtBot) -> None:
        shape = PackageBody(_cylinder_package())  # type: ignore[arg-type]
        items = shape.draw(_pts((0, 0), (4, 0)), QColor("#404858"), _DARK, 1.0, _Z)
        body = self._body_item(items)
        assert body.brush().color().name() == "#404858"  # type: ignore[attr-defined]

    def test_border_is_darker_than_fill(self, qtbot: QtBot) -> None:
        shape = PackageBody(_colored_cylinder_package("#d6c08c"))  # type: ignore[arg-type]
        items = shape.draw(_pts((0, 0), (4, 0)), _RED, _DARK, 1.0, _Z)
        body = self._body_item(items)
        fill = body.brush().color()  # type: ignore[attr-defined]
        pen = body.pen().color()  # type: ignore[attr-defined]
        assert pen.lightnessF() < fill.lightnessF()

    def test_sharp_edge_squares_prism_corners(self, qtbot: QtBot) -> None:
        # corner_radius=1.0 is declared, but edge=sharp forces 0 → a plain
        # QGraphicsRectItem (rounded would be a QGraphicsPathItem).
        shape = PackageBody(_colored_prism_package(edge="sharp"))  # type: ignore[arg-type]
        pts = _pts(*([(0, i) for i in range(4)] + [(3, 3 - i) for i in range(4)]))
        items = shape.draw(pts, _RED, _DARK, 1.0, _Z)
        assert isinstance(self._body_item(items), QGraphicsRectItem)

    def test_rounded_edge_rounds_prism_corners(self, qtbot: QtBot) -> None:
        shape = PackageBody(_colored_prism_package(edge="rounded"))  # type: ignore[arg-type]
        pts = _pts(*([(0, i) for i in range(4)] + [(3, 3 - i) for i in range(4)]))
        items = shape.draw(pts, _RED, _DARK, 1.0, _Z)
        assert isinstance(self._body_item(items), QGraphicsPathItem)


def _axial_cyl_package(edge_radius: float = 0.0) -> object:
    return build_package(
        {
            "shape": "cylinder",
            "dims": {"diameter": 2.5, "length": 6.3, "edge_radius": edge_radius},
            "arrangement": "axial", "bend": 0,
            "leads": {
                "direction": [[0, 0, -1], [0, 0, 1]],
                "body_offset": {"unit": 2.5, "value": [[4, 0]]},
                "board_offset": "default", "lead_r": 0.27,
            },
        },
        "AX",
    )


def _radial_cyl_package() -> object:
    return build_package(
        {
            "shape": "cylinder",
            "dims": {"diameter": 6.3, "length": 11.0},
            "arrangement": "radial", "bend": 4,
            "leads": {
                "direction": [[0, 0, -1], [0, 0, -1]],
                "body_offset": {"unit": 2.5, "value": [[1, 0]]},
                "board_offset": "default", "lead_r": 0.25,
            },
        },
        "RAD",
    )


def _radial_prism_package() -> object:
    return build_package(
        {
            "shape": "prism",
            "dims": {"width": 4.5, "length": 11.0, "height": 3.5},
            "arrangement": "radial", "bend": 0,
            "leads": {
                "direction": [[0, 0, -1], [0, 0, -1]],
                "body_offset": {"unit": 2.5, "value": [[1, 0]]},
                "board_offset": "default", "lead_r": 0.25,
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
            for it in diode.draw(_pts((0, 0), (4, 0)), _RED, _DARK, 1.0, _Z)
        )
        p_paths = sum(
            isinstance(it, QGraphicsPathItem)
            for it in plain.draw(_pts((0, 0), (4, 0)), _RED, _DARK, 1.0, _Z)
        )
        assert d_paths == p_paths + 1

    def test_cathode_stripe_stays_on_flat_part_of_capsule(
        self, qtbot: QtBot,
    ) -> None:
        # On a fully-rounded capsule (edge_radius = D/2) the stripe must sit on
        # the straight segment, never on the curved end cap: its outer edge is
        # at most body_len/2 - r from the centre.
        pkg = _axial_cyl_package(edge_radius=1.25)  # D=2.5 → r = D/2
        shape = PackageBody(pkg, component_class=ComponentClass.DIODE)
        items = shape.draw(_pts((0, 0), (8, 0)), _RED, _DARK, 1.0, _Z)
        body, stripe = [
            it for it in items if isinstance(it, QGraphicsPathItem)
        ][0], [it for it in items if isinstance(it, QGraphicsPathItem)][-1]
        mm = DEFAULT_THEME.raster_pitch_mm
        body_len = pkg.length_mm / mm  # type: ignore[attr-defined]
        r = min(pkg.edge_radius_mm / mm, body_len / 2.0)  # type: ignore[attr-defined]
        mid_x = 4.0  # midpoint of (0,0)-(8,0)
        stripe_right = stripe.sceneBoundingRect().right() - mid_x
        assert stripe_right <= body_len / 2.0 - r + 1e-6
        # And the stripe's near corner lies inside the body silhouette.
        from PySide6.QtCore import QPointF
        sbb = stripe.sceneBoundingRect()
        assert body.path().contains(QPointF(sbb.right() - mid_x, 0.0))

    def test_radial_emits_disc_and_lead(self, qtbot: QtBot) -> None:
        shape = PackageBody(_radial_cyl_package())  # type: ignore[arg-type]
        items = shape.draw(_pts((0, 0), (1, 0)), _RED, _DARK, 1.0, _Z)
        assert any(isinstance(it, QGraphicsEllipseItem) for it in items)
        assert any(isinstance(it, QGraphicsLineItem) for it in items)

    def test_radial_cylinder_centred_between_pads(self, qtbot: QtBot) -> None:
        # The radial disc sits at the geometric midpoint of the pads, not on
        # the anchor pad.
        shape = PackageBody(_radial_cyl_package())  # type: ignore[arg-type]
        items = shape.draw(_pts((0, 0), (1, 0)), _RED, _DARK, 1.0, _Z)
        disc = next(it for it in items if isinstance(it, QGraphicsEllipseItem))
        c = disc.rect().center()
        assert (round(c.x(), 3), round(c.y(), 3)) == (0.5, 0.0)

    def test_radial_prism_centred_between_pads(self, qtbot: QtBot) -> None:
        shape = PackageBody(_radial_prism_package())  # type: ignore[arg-type]
        items = shape.draw(_pts((0, 0), (1, 0)), _RED, _DARK, 1.0, _Z)
        body = next(it for it in items if isinstance(it, QGraphicsPathItem))
        c = body.sceneBoundingRect().center()
        assert (round(c.x(), 3), round(c.y(), 3)) == (0.5, 0.0)

    def test_radial_body_covers_close_pins(self, qtbot: QtBot) -> None:
        # D=6.3 mm (~2.52 holes, r~1.26).  At 1-hole spacing the body is
        # centred at (0.5, 0) and covers both pads.
        shape = PackageBody(_radial_cyl_package())  # type: ignore[arg-type]
        assert shape.drawn_pin_numbers(_pts((0, 0), (1, 0))) == frozenset({1, 2})

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
    """Every concrete shape must honour BodyShape.draw → list[QGraphicsItem]."""

    @pytest.mark.parametrize("shape_factory,positions", [
        (lambda: GenericBody(), _pts((0, 0), (2, 0))),
        (lambda: PackageBody(_cylinder_package()), _pts((0, 0), (4, 0))),
        (lambda: PackageBody(_radial_cyl_package()), _pts((0, 0), (1, 0))),
        (lambda: TO92Body(), _pts((0, 0), (1, 0), (2, 0))),
        (lambda: DIPBody(4),
         _pts(*([(0, i) for i in range(4)] + [(3, 3 - i) for i in range(4)]))),
        (lambda: SvgDIPBody(4),
         _pts(*([(0, i) for i in range(4)] + [(3, 3 - i) for i in range(4)]))),
    ])
    def test_draw_returns_list_of_qgraphicsitem(
        self, qtbot: QtBot, shape_factory, positions: list[Point],
    ) -> None:
        items = shape_factory().draw(positions, _RED, _DARK, 1.0, _Z)
        assert isinstance(items, list)
        for item in items:
            assert isinstance(item, QGraphicsItem)

    @pytest.mark.parametrize("shape_factory,positions", [
        (lambda: GenericBody(), _pts((0, 0), (2, 0))),
        (lambda: PackageBody(_cylinder_package()), _pts((0, 0), (4, 0))),
    ])
    def test_draw_alpha_zero_still_returns_items(
        self, qtbot: QtBot, shape_factory, positions: list[Point],
    ) -> None:
        items = shape_factory().draw(positions, _RED, _DARK, 0.0, _Z)
        assert isinstance(items, list)

    @pytest.mark.parametrize("shape_factory,positions", [
        (lambda: GenericBody(), _pts((0, 0), (2, 0))),
        (lambda: PackageBody(_cylinder_package()), _pts((0, 0), (4, 0))),
        (lambda: PackageBody(_radial_cyl_package()), _pts((0, 0), (1, 0))),
    ])
    def test_drawn_pin_numbers_returns_frozenset(
        self, qtbot: QtBot, shape_factory, positions: list[Point],
    ) -> None:
        result = shape_factory().drawn_pin_numbers(positions)
        assert isinstance(result, frozenset)
