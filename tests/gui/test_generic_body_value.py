# SPDX-License-Identifier: MIT
"""A generic (non-SMD) body prints its raw value/part number."""

from __future__ import annotations

import uuid

import pytest
from PySide6.QtGui import QColor
from PySide6.QtWidgets import QGraphicsSimpleTextItem

from protoboard.domain.components.instance import ComponentInstance
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
