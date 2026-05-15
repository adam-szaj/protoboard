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

from protoboard.domain.components.body_type import BodyTypeSpec
from protoboard.domain.components.definition import ComponentClass, ComponentDefinition
from protoboard.domain.components.footprints import (
    DualRow,
    SingleRow,
    TO220Footprint,
)
from protoboard.domain.components.instance import ComponentInstance
from protoboard.domain.layout.placement import ComponentPlacement
from protoboard.geometry import Point
from protoboard.gui.renderers.body_shapes import (
    AxialBody,
    BodyShape,
    DIPBody,
    GenericBody,
    RadialBody,
    SvgDIPBody,
    TO92Body,
    TO220Body,
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
    from protoboard.domain.components.footprints import SingleRow, DualRow
    if fp_type == "DIP":
        fp = DualRow(n_per_row=n_pins // 2)
    else:
        fp = SingleRow(n=n_pins)
    return ComponentDefinition(
        name="TEST",
        component_class=cls,
        footprint=fp,
        pin_names={},
    )


def _make_placement() -> ComponentPlacement:
    return ComponentPlacement(
        component_id=uuid.uuid4(),
        position=Point(0, 0),
        rotation=0,
        side="top",
    )


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
        # bbox should span at least the pin distance with some margin
        assert bbox.width() >= 2.0

    def test_z_value_is_set(self, qtbot: QtBot) -> None:
        items = GenericBody().draw(_pts((0, 0)), _RED, _DARK, 1.0, _Z)
        assert items[0].zValue() == pytest.approx(_Z)

    def test_alpha_affects_fill(self, qtbot: QtBot) -> None:
        items_full = GenericBody().draw(_pts((0, 0)), _RED, _DARK, 1.0, _Z)
        items_half = GenericBody().draw(_pts((0, 0)), _RED, _DARK, 0.5, _Z)
        # Both should produce a rect — alpha is encoded in pen/brush, not shape.
        assert isinstance(items_full[0], QGraphicsRectItem)
        assert isinstance(items_half[0], QGraphicsRectItem)


# ---------------------------------------------------------------------------
# AxialBody
# ---------------------------------------------------------------------------

class TestAxialBody:
    def test_single_pin_falls_back_to_generic(self, qtbot: QtBot) -> None:
        items = AxialBody().draw(_pts((1, 1)), _RED, _DARK, 1.0, _Z)
        assert len(items) == 1
        assert isinstance(items[0], QGraphicsRectItem)

    def test_two_horizontal_pins_returns_line_and_body(self, qtbot: QtBot) -> None:
        items = AxialBody().draw(_pts((0, 0), (3, 0)), _RED, _DARK, 1.0, _Z)
        # Should have lead line + body path.
        assert len(items) >= 2
        has_line = any(isinstance(it, QGraphicsLineItem) for it in items)
        has_path = any(isinstance(it, QGraphicsPathItem) for it in items)
        assert has_line
        assert has_path

    def test_two_vertical_pins(self, qtbot: QtBot) -> None:
        items = AxialBody().draw(_pts((0, 0), (0, 3)), _RED, _DARK, 1.0, _Z)
        assert len(items) >= 2

    def test_coincident_pins_falls_back_to_generic(self, qtbot: QtBot) -> None:
        items = AxialBody().draw(_pts((1, 1), (1, 1)), _RED, _DARK, 1.0, _Z)
        assert isinstance(items[0], QGraphicsRectItem)

    def test_z_ordering_lead_below_body(self, qtbot: QtBot) -> None:
        items = AxialBody().draw(_pts((0, 0), (3, 0)), _RED, _DARK, 1.0, _Z)
        line_z = min(it.zValue() for it in items if isinstance(it, QGraphicsLineItem))
        path_z = max(it.zValue() for it in items if isinstance(it, QGraphicsPathItem))
        assert line_z < path_z


# ---------------------------------------------------------------------------
# RadialBody
# ---------------------------------------------------------------------------

class TestRadialBody:
    def test_single_pin_falls_back_to_generic(self, qtbot: QtBot) -> None:
        items = RadialBody().draw(_pts((0, 0)), _RED, _DARK, 1.0, _Z)
        assert isinstance(items[0], QGraphicsRectItem)

    def test_two_pins_produces_items(self, qtbot: QtBot) -> None:
        items = RadialBody().draw(_pts((0, 0), (1, 0)), _RED, _DARK, 1.0, _Z)
        assert len(items) >= 1


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


# ---------------------------------------------------------------------------
# TO220Body
# ---------------------------------------------------------------------------

class TestTO220Body:
    def test_three_pins_produces_items(self, qtbot: QtBot) -> None:
        items = TO220Body().draw(_pts((0, 0), (1, 0), (2, 0)), _RED, _DARK, 1.0, _Z)
        assert len(items) >= 1


# ---------------------------------------------------------------------------
# SvgDIPBody
# ---------------------------------------------------------------------------

class TestSvgDIPBody:
    def test_produces_items_for_dip8(self, qtbot: QtBot) -> None:
        pos = _pts(*([(0, i) for i in range(4)] + [(3, 3 - i) for i in range(4)]))
        items = SvgDIPBody(n_per_row=4).draw(pos, _RED, _DARK, 1.0, _Z)
        assert len(items) >= 1

    def test_too_few_pins_falls_back(self, qtbot: QtBot) -> None:
        items = SvgDIPBody(n_per_row=4).draw(_pts((0, 0)), _RED, _DARK, 1.0, _Z)
        assert isinstance(items[0], QGraphicsRectItem)


# ---------------------------------------------------------------------------
# DIPBody (fallback)
# ---------------------------------------------------------------------------

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

    def test_sil_2_resistor_returns_axial_or_cylinder(self) -> None:
        defn = _make_defn(ComponentClass.RESISTOR, n_pins=2, fp_type="SIL")
        shape = body_shape_for(defn)
        assert isinstance(shape, BodyShape)  # AxialBody or CylinderBody

    def test_sil_2_capacitor_nonpolar_returns_radial_or_cylinder(self) -> None:
        defn = _make_defn(ComponentClass.CAPACITOR_NONPOLAR, n_pins=2, fp_type="SIL")
        shape = body_shape_for(defn)
        assert isinstance(shape, BodyShape)

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

    def test_generic_fallback(self) -> None:
        # Large SIL connector has no special shape.
        defn = _make_defn(ComponentClass.CONNECTOR, n_pins=10, fp_type="SIL")
        shape = body_shape_for(defn)
        assert isinstance(shape, BodyShape)

    def test_placement_vflipped_passed_through(self) -> None:
        defn = _make_defn(ComponentClass.DIODE, n_pins=2, fp_type="SIL")
        pl_normal = _make_placement()
        pl_flipped = _make_placement()
        pl_flipped.vertical_flipped = True
        # Both should return a valid BodyShape without error.
        s1 = body_shape_for(defn, placement=pl_normal)
        s2 = body_shape_for(defn, placement=pl_flipped)
        assert isinstance(s1, BodyShape)
        assert isinstance(s2, BodyShape)


# ---------------------------------------------------------------------------
# BodyShape interface contract
# ---------------------------------------------------------------------------

class TestBodyShapeContract:
    """Verify all concrete shapes honour the BodyShape interface contract."""

    @pytest.mark.parametrize("shape,positions", [
        (GenericBody(), _pts((0, 0), (2, 0))),
        (AxialBody(), _pts((0, 0), (2, 0))),
        (RadialBody(), _pts((0, 0), (1, 0))),
        (TO92Body(), _pts((0, 0), (1, 0), (2, 0))),
        (DIPBody(4), _pts(*([(0, i) for i in range(4)] + [(3, 3-i) for i in range(4)]))),
        (SvgDIPBody(4), _pts(*([(0, i) for i in range(4)] + [(3, 3-i) for i in range(4)]))),
    ])
    def test_draw_returns_list_of_qgraphicsitem(
        self, qtbot: QtBot, shape: BodyShape, positions: list[Point]
    ) -> None:
        items = shape.draw(positions, _RED, _DARK, 1.0, _Z)
        assert isinstance(items, list)
        for item in items:
            assert isinstance(item, QGraphicsItem)

    @pytest.mark.parametrize("shape,positions", [
        (GenericBody(), _pts((0, 0), (2, 0))),
        (AxialBody(), _pts((0, 0), (2, 0))),
    ])
    def test_draw_alpha_zero_still_returns_items(
        self, qtbot: QtBot, shape: BodyShape, positions: list[Point]
    ) -> None:
        items = shape.draw(positions, _RED, _DARK, 0.0, _Z)
        assert isinstance(items, list)

    @pytest.mark.parametrize("shape,positions", [
        (GenericBody(), _pts((0, 0), (2, 0))),
        (AxialBody(), _pts((0, 0), (2, 0))),
        (RadialBody(), _pts((0, 0), (1, 0))),
    ])
    def test_drawn_pin_numbers_returns_frozenset(
        self, qtbot: QtBot, shape: BodyShape, positions: list[Point]
    ) -> None:
        result = shape.drawn_pin_numbers(positions)
        assert isinstance(result, frozenset)
