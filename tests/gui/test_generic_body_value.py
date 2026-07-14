# SPDX-License-Identifier: MIT
"""A generic (non-SMD) body prints its raw value/part number."""

from __future__ import annotations

import math
import uuid

import pytest
from PySide6.QtGui import QColor
from PySide6.QtWidgets import QGraphicsSimpleTextItem

from protoboard.domain.components.instance import ComponentInstance
from protoboard.domain.components.package import resolved_footprint
from protoboard.domain.layout.placement import (
    ComponentPlacement,
    pin_world_position,
)
from protoboard.geometry import Point
from protoboard.gui.renderers.body_shapes import body_shape_for
from protoboard.gui.renderers.packages import load_renderer_plugins
from protoboard.controller.theme import DEFAULT_THEME
from protoboard.paths import BUILTIN_LIBRARY_DIR

_RED = QColor("#cc4444")
_DARK = QColor("#222222")
_Z = 5.0
_TO92_PADS = ((0.0, 0.0), (1.0, 0.0), (2.0, 0.0))


@pytest.fixture(scope="module")
def library():
    from protoboard.services.library.loader import load_library
    load_renderer_plugins([BUILTIN_LIBRARY_DIR])
    return load_library([BUILTIN_LIBRARY_DIR])


@pytest.fixture(scope="module")
def families():
    from protoboard.services.library.package_family_loader import (
        load_full_package_catalogue,
    )
    return load_full_package_catalogue()


def _draw(library, families, value: str):
    defn = library.get("BC547")
    assert defn is not None
    inst = ComponentInstance(
        id=uuid.uuid4(), definition_ref="BC547", refdes="Q1", value=value,
    ).bind(defn, families)
    shape = body_shape_for(defn, instance=inst, package_families=families)
    return shape.draw([Point(*p) for p in _TO92_PADS], _RED, _DARK, 1.0, _Z,
                      DEFAULT_THEME)


def _texts(items) -> list[str]:
    return [it.text() for it in items if isinstance(it, QGraphicsSimpleTextItem)]


def test_to92_prints_part_number(qtbot, library, families) -> None:
    items = _draw(library, families, "BC547")
    assert "BC547" in _texts(items)


def test_empty_value_prints_nothing(qtbot, library, families) -> None:
    items = _draw(library, families, "?")
    assert _texts(items) == []


def test_generic_label_uses_fixed_font(qtbot, library, families) -> None:
    items = _draw(library, families, "2N2222")
    labels = [it for it in items if isinstance(it, QGraphicsSimpleTextItem)]
    assert labels
    assert labels[0].font().pointSizeF() == pytest.approx(
        DEFAULT_THEME.body_print_font_size
    )


def _draw_rotated(library, families, rotation: int):
    """Draw BC547 (TO-92) at a placed, rotated position — mirrors the
    ``pin_world_position`` idiom from ``test_body_transform_parity.py``."""
    defn = library.get("BC547")
    assert defn is not None
    inst = ComponentInstance(
        id=uuid.uuid4(), definition_ref="BC547", refdes="Q1", value="2N2222",
    ).bind(defn, families)
    fp = resolved_footprint(defn, inst, families)
    placement = ComponentPlacement(
        component_id=uuid.uuid4(), position=Point(20.0, 20.0),
        rotation=rotation, side="top",
    )
    positions = [
        pin_world_position(fp, placement, n + 1) for n in range(fp.pin_count())
    ]
    shape = body_shape_for(defn, placement, inst, package_families=families)
    return shape.draw(positions, _RED, _DARK, 1.0, _Z, DEFAULT_THEME)


def test_generic_label_rotates_with_body(qtbot, library, families) -> None:
    items0 = _draw_rotated(library, families, 0)
    items90 = _draw_rotated(library, families, 90)
    labels0 = [it for it in items0 if isinstance(it, QGraphicsSimpleTextItem)]
    labels90 = [it for it in items90 if isinstance(it, QGraphicsSimpleTextItem)]
    assert labels0 and labels90
    m0 = labels0[0].transform()
    m90 = labels90[0].transform()
    angle0 = math.degrees(math.atan2(m0.m12(), m0.m11()))
    angle90 = math.degrees(math.atan2(m90.m12(), m90.m11()))
    # The label's world orientation rotates with the body's placement — a
    # ~90 degree shift (mod 180, since the upright-flip in make_body_label
    # may add a half turn to keep glyphs readable).
    delta = (angle90 - angle0) % 180.0
    assert delta == pytest.approx(90.0, abs=1.0)
