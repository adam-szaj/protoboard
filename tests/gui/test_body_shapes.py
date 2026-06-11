# SPDX-License-Identifier: MIT
"""Tests for body_shapes.py — shape dispatch, geometry helpers, item counts."""

from __future__ import annotations

import math
import uuid
import xml.etree.ElementTree as ET
from pathlib import Path

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
from protoboard.domain.components.two_pin_body import (
    BodyShape as _TPShape,
    DiscProfile,
    EdgeStyle,
    LeadArrangement,
    TwoPinBodyFamily,
    TwoPinBodySpec,
)
from protoboard.domain.layout.placement import ComponentPlacement
from protoboard.geometry import Point
from protoboard.gui.renderers.body_shapes import (
    BodyShape,
    DIPBody,
    GenericBody,
    SvgDIPBody,
    TO92Body,
    TO220Body,
    TwoPinParametricBody,
    _find_id,
    body_shape_for,
)
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


def _axial_cyl_spec(polar: bool = False) -> TwoPinBodySpec:
    return TwoPinBodySpec(
        family="resistor_axial", variant_id="default", label="default",
        lead_arrangement=LeadArrangement.AXIAL,
        shape=_TPShape.CYLINDER,
        length_mm=6.5, diameter_mm=2.5, polar=polar,
    )


def _radial_cyl_spec(polar: bool = True) -> TwoPinBodySpec:
    return TwoPinBodySpec(
        family="led_radial", variant_id="default", label="default",
        lead_arrangement=LeadArrangement.RADIAL,
        shape=_TPShape.CYLINDER,
        length_mm=8.0, diameter_mm=5.0, polar=polar,
    )


def _radial_disc_spec(profile: DiscProfile = DiscProfile.FLAT) -> TwoPinBodySpec:
    return TwoPinBodySpec(
        family="ceramic_disc", variant_id="default", label="default",
        lead_arrangement=LeadArrangement.RADIAL,
        shape=_TPShape.DISC, disc_profile=profile,
        diameter_mm=5.0, thickness_mm=2.0,
    )


def _radial_prism_spec() -> TwoPinBodySpec:
    return TwoPinBodySpec(
        family="brick", variant_id="default", label="default",
        lead_arrangement=LeadArrangement.RADIAL,
        shape=_TPShape.PRISM, edge=EdgeStyle.SHARP,
        length_mm=22.0, width_mm=9.5, height_mm=9.0,
    )


def _families_with(*specs: TwoPinBodySpec) -> dict[str, TwoPinBodyFamily]:
    """Build a per-family map from individual specs (one family per spec)."""
    out: dict[str, TwoPinBodyFamily] = {}
    for s in specs:
        out[s.family] = TwoPinBodyFamily(family=s.family, variants=(s,))
    return out


# ---------------------------------------------------------------------------
# _find_id helper
# ---------------------------------------------------------------------------

class TestFindId:
    def test_finds_direct_child(self) -> None:
        root = ET.fromstring('<svg><rect id="body" /></svg>')
        el = _find_id(root, "body")
        assert el is not None
        assert el.tag == "rect"

    def test_finds_nested_element(self) -> None:
        root = ET.fromstring('<svg><g><circle id="target" /></g></svg>')
        el = _find_id(root, "target")
        assert el is not None
        assert el.tag == "circle"

    def test_returns_none_when_not_found(self) -> None:
        root = ET.fromstring('<svg><rect id="other" /></svg>')
        assert _find_id(root, "missing") is None

    def test_returns_first_match_among_multiple(self) -> None:
        root = ET.fromstring(
            '<svg><rect id="x" data-n="1"/><rect id="x" data-n="2"/></svg>'
        )
        el = _find_id(root, "x")
        assert el is not None
        assert el.get("data-n") == "1"


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
# TwoPinParametricBody — six combinations × polarity × vertical flip
# ---------------------------------------------------------------------------

class TestTwoPinParametricBodyAxial:
    def test_axial_cylinder_produces_leads_and_body(self, qtbot: QtBot) -> None:
        body = TwoPinParametricBody(_axial_cyl_spec())
        items = body.draw(_pts((0, 0), (5, 0)), _RED, _DARK, 1.0, _Z)
        # Two leads + body path = at least 3 items when pads are farther
        # apart than the body length.
        assert any(isinstance(it, QGraphicsLineItem) for it in items)
        assert any(isinstance(it, QGraphicsPathItem) for it in items)

    def test_axial_polar_adds_cathode_stripe(self, qtbot: QtBot) -> None:
        plain = TwoPinParametricBody(_axial_cyl_spec(polar=False))
        polar = TwoPinParametricBody(_axial_cyl_spec(polar=True))
        items_plain = plain.draw(_pts((0, 0), (5, 0)), _RED, _DARK, 1.0, _Z)
        items_polar = polar.draw(_pts((0, 0), (5, 0)), _RED, _DARK, 1.0, _Z)
        # Polar version emits an extra QGraphicsPathItem for the stripe.
        paths_plain = sum(isinstance(it, QGraphicsPathItem) for it in items_plain)
        paths_polar = sum(isinstance(it, QGraphicsPathItem) for it in items_polar)
        assert paths_polar > paths_plain

    def test_axial_short_distance_omits_leads(self, qtbot: QtBot) -> None:
        body = TwoPinParametricBody(_axial_cyl_spec())
        # Pads at 1 raster apart, body length 6.5 mm ≈ 2.6 raster — no leads.
        items = body.draw(_pts((0, 0), (1, 0)), _RED, _DARK, 1.0, _Z)
        assert not any(isinstance(it, QGraphicsLineItem) for it in items)

    def test_axial_coincident_pins_falls_back_to_generic(
        self, qtbot: QtBot,
    ) -> None:
        body = TwoPinParametricBody(_axial_cyl_spec())
        items = body.draw(_pts((1, 1), (1, 1)), _RED, _DARK, 1.0, _Z)
        assert any(isinstance(it, QGraphicsRectItem) for it in items)


class TestTwoPinParametricBodyRadial:
    def test_radial_cylinder_emits_circle_and_lead(self, qtbot: QtBot) -> None:
        body = TwoPinParametricBody(_radial_cyl_spec())
        items = body.draw(_pts((0, 0), (1, 0)), _RED, _DARK, 1.0, _Z)
        assert any(isinstance(it, QGraphicsEllipseItem) for it in items)
        assert any(isinstance(it, QGraphicsLineItem) for it in items)

    def test_radial_disc_emits_circle(self, qtbot: QtBot) -> None:
        body = TwoPinParametricBody(_radial_disc_spec())
        items = body.draw(_pts((0, 0), (1, 0)), _RED, _DARK, 1.0, _Z)
        assert any(isinstance(it, QGraphicsEllipseItem) for it in items)

    def test_radial_prism_emits_rectangle_path(self, qtbot: QtBot) -> None:
        body = TwoPinParametricBody(_radial_prism_spec())
        items = body.draw(_pts((0, 0), (1, 0)), _RED, _DARK, 1.0, _Z)
        assert any(isinstance(it, QGraphicsPathItem) for it in items)
        # Lead remains too.
        assert any(isinstance(it, QGraphicsLineItem) for it in items)

    def test_radial_polar_anchor_pin_is_drawn_by_body(
        self, qtbot: QtBot,
    ) -> None:
        body = TwoPinParametricBody(_radial_cyl_spec(polar=True))
        # The anchor pin (pin 1 by default) is occluded by the body, so
        # component_renderer skips its marker.
        drawn = body.drawn_pin_numbers(_pts((0, 0), (1, 0)))
        assert 1 in drawn

    def test_radial_vertical_flipped_swaps_anchor(self, qtbot: QtBot) -> None:
        body = TwoPinParametricBody(_radial_cyl_spec(), vertical_flipped=True)
        drawn = body.drawn_pin_numbers(_pts((0, 0), (1, 0)))
        assert 2 in drawn


# ---------------------------------------------------------------------------
# TO92Body / TO220Body / DIPBody / SvgDIPBody
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

    def test_two_pin_resistor_uses_parametric_body(self) -> None:
        defn = _make_defn(ComponentClass.RESISTOR, n_pins=2, fp_type="TwoPin")
        families = _families_with(_axial_cyl_spec())
        shape = body_shape_for(defn, two_pin_families=families)
        # Loader feeds resistor_axial; default variant is axial cylinder.
        assert isinstance(shape, TwoPinParametricBody)

    def test_two_pin_with_explicit_variant(self) -> None:
        from protoboard.domain.components.instance import ComponentInstance
        defn = ComponentDefinition(
            name="R", component_class=ComponentClass.RESISTOR,
            footprint=TwoPin(), pin_names={},
        )
        inst = ComponentInstance(
            id=uuid.uuid4(), definition_ref="R", refdes="R1", value="",
            body_variant="led_radial/default",
        )
        families = _families_with(_radial_cyl_spec())
        shape = body_shape_for(defn, instance=inst, two_pin_families=families)
        assert isinstance(shape, TwoPinParametricBody)
        # Resolved spec is the radial one we injected.
        assert shape._spec.lead_arrangement is LeadArrangement.RADIAL  # type: ignore[attr-defined]

    def test_two_pin_no_matching_family_falls_back_to_generic(self) -> None:
        # ComponentClass with no entry in _CLASS_TO_FAMILY.
        defn = ComponentDefinition(
            name="X", component_class=ComponentClass.SWITCH,
            footprint=TwoPin(), pin_names={},
        )
        shape = body_shape_for(defn, two_pin_families={})
        # No legacy ladder — generic bbox.
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
        families = _families_with(_axial_cyl_spec(polar=True))
        # Both should return a valid BodyShape without error.
        s1 = body_shape_for(defn, placement=pl_normal,
                            two_pin_families=families)
        s2 = body_shape_for(defn, placement=pl_flipped,
                            two_pin_families=families)
        assert isinstance(s1, BodyShape)
        assert isinstance(s2, BodyShape)


# ---------------------------------------------------------------------------
# BodyShape interface contract
# ---------------------------------------------------------------------------

class TestBodyShapeContract:
    """Every concrete shape must honour BodyShape.draw → list[QGraphicsItem]."""

    @pytest.mark.parametrize("shape_factory,positions", [
        (lambda: GenericBody(), _pts((0, 0), (2, 0))),
        (lambda: TwoPinParametricBody(_axial_cyl_spec()), _pts((0, 0), (2, 0))),
        (lambda: TwoPinParametricBody(_radial_cyl_spec()), _pts((0, 0), (1, 0))),
        (lambda: TwoPinParametricBody(_radial_disc_spec()), _pts((0, 0), (1, 0))),
        (lambda: TwoPinParametricBody(_radial_prism_spec()), _pts((0, 0), (1, 0))),
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
        (lambda: TwoPinParametricBody(_axial_cyl_spec()), _pts((0, 0), (2, 0))),
    ])
    def test_draw_alpha_zero_still_returns_items(
        self, qtbot: QtBot, shape_factory, positions: list[Point],
    ) -> None:
        items = shape_factory().draw(positions, _RED, _DARK, 0.0, _Z)
        assert isinstance(items, list)

    @pytest.mark.parametrize("shape_factory,positions", [
        (lambda: GenericBody(), _pts((0, 0), (2, 0))),
        (lambda: TwoPinParametricBody(_axial_cyl_spec()), _pts((0, 0), (2, 0))),
        (lambda: TwoPinParametricBody(_radial_cyl_spec()), _pts((0, 0), (1, 0))),
    ])
    def test_drawn_pin_numbers_returns_frozenset(
        self, qtbot: QtBot, shape_factory, positions: list[Point],
    ) -> None:
        result = shape_factory().drawn_pin_numbers(positions)
        assert isinstance(result, frozenset)
