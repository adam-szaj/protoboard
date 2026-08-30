# SPDX-License-Identifier: MIT
"""Value-decoding renderers — DIN colour bands and the SMD chip digit code.

The DIN band plugin (``library/renderers/resistor_bands.py``) paints the IEC
colour code from ``instance.value`` on resistors and small inductors in the
shared DIN axial bodies; ``SmdChipRenderer`` prints the digit code
(``22k`` → ``223``) on two-pin chips.  Both degrade to the plain body when
there is no instance or the value does not decode.
"""

from __future__ import annotations

import uuid

import pytest
from PySide6.QtGui import QColor
from PySide6.QtWidgets import (
    QAbstractGraphicsShapeItem,
    QGraphicsSimpleTextItem,
)

from protoboard.controller.theme import DEFAULT_THEME
from protoboard.domain.components.instance import ComponentInstance
from protoboard.geometry import Point
from protoboard.gui.renderers.body_shapes import body_shape_for
from protoboard.gui.renderers.packages import load_renderer_plugins
from protoboard.paths import BUILTIN_LIBRARY_DIR

_RED = QColor("#cc4444")
_DARK = QColor("#222222")
_Z = 5.0

#: The band palette entries the assertions read (mirrors the plugin tables).
_BROWN = "#8b5a2b"
_BLACK = "#000000"
_ORANGE = "#ff8c1a"
_GOLD = "#d4af37"
_INDUCTOR_GREEN = "#7fa06a"
_DIN_BEIGE = "#d6c08c"


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


def _draw(
    library,
    families,
    defname: str,
    value: str,
    *,
    variant: str | None = None,
    tolerance: str | None = None,
    pads: tuple[tuple[float, float], ...] = ((0.0, 0.0), (4.0, 0.0)),
):
    defn = library.get(defname)
    assert defn is not None
    inst = ComponentInstance(
        id=uuid.uuid4(),
        definition_ref=defname,
        refdes="X1",
        value=value,
        package_variant=variant,
        tolerance=tolerance,
    ).bind(defn, families)
    shape = body_shape_for(defn, instance=inst, package_families=families)
    # draw() now returns (body_items, label_items); these tests inspect all
    # drawn items together, so flatten the two Z bands back into one list.
    body_items, label_items = shape.draw(
        [Point(*p) for p in pads],
        _RED,
        _DARK,
        1.0,
        _Z,
        DEFAULT_THEME,
    )
    return body_items + label_items


def _fills(items) -> list[str]:
    return [
        it.brush().color().name()
        for it in items
        if isinstance(it, QAbstractGraphicsShapeItem)
    ]


def _texts(items) -> list[str]:
    return [it.text() for it in items if isinstance(it, QGraphicsSimpleTextItem)]


class TestDinColorBands:

    def test_resistor_10k_bands_in_order(self, qtbot, library, families):
        # Body first, then the code group from the pin-1 end (brown black
        # orange = 1 0 ×10³) and the gold tolerance band at the far end.
        fills = _fills(_draw(library, families, "R", "10k"))
        assert fills == [_DIN_BEIGE, _BROWN, _BLACK, _ORANGE, _GOLD]

    def test_undecodable_value_draws_plain_body(self, qtbot, library, families):
        assert _fills(_draw(library, families, "R", "?")) == [_DIN_BEIGE]

    def test_upright_mount_shows_no_bands(self, qtbot, library, families):
        # Bands are visible only on the horizontal body: on adjacent pads the
        # axial part stands upright (top-view disc), so no code is drawn.
        fills = _fills(
            _draw(library, families, "R", "10k", pads=((0.0, 0.0), (1.0, 0.0)))
        )
        assert fills == [_DIN_BEIGE]

    def test_inductor_gets_its_own_body_color_and_uh_code(
        self,
        qtbot,
        library,
        families,
    ):
        # The shared DIN family declares the resistor beige; an inductor body
        # renders in the moulded green, with the code in µH (10 µH = brown
        # black ×10⁰-black + gold).
        fills = _fills(_draw(library, families, "L", "10uH"))
        assert fills == [_INDUCTOR_GREEN, _BROWN, _BLACK, _BLACK, _GOLD]

    def test_declared_tolerance_drives_the_last_band(
        self,
        qtbot,
        library,
        families,
    ):
        # instance.tolerance = 1% → the tolerance band is brown, not the
        # default gold.
        fills = _fills(_draw(library, families, "R", "10k", tolerance="1%"))
        assert fills == [_DIN_BEIGE, _BROWN, _BLACK, _ORANGE, _BROWN]

    def test_twenty_percent_encodes_as_no_band(self, qtbot, library, families):
        # ±20 % has no colour in the IEC code — its encoding IS the missing
        # band, so only the three code-group bands are drawn.
        fills = _fills(_draw(library, families, "R", "10k", tolerance="20%"))
        assert fills == [_DIN_BEIGE, _BROWN, _BLACK, _ORANGE]

    def test_unspecified_tolerance_keeps_default_gold(
        self,
        qtbot,
        library,
        families,
    ):
        fills = _fills(_draw(library, families, "R", "10k"))
        assert fills[-1] == _GOLD

    def test_bands_lie_inside_the_body(self, qtbot, library, families):
        items = _draw(library, families, "R", "10k")
        shapes = [it for it in items if isinstance(it, QAbstractGraphicsShapeItem)]
        body, *bands = shapes
        assert len(bands) == 4
        body_rect = body.sceneBoundingRect()
        for band in bands:
            assert body_rect.contains(band.sceneBoundingRect())


class TestSmdDigitCode:

    def test_resistor_22k_prints_223(self, qtbot, library, families):
        items = _draw(
            library,
            families,
            "R",
            "22k",
            variant="chip-smd/imperial/0805",
            pads=((0.0, 0.0), (1.0, 0.0)),
        )
        assert _texts(items) == ["223"]

    def test_sub_ten_ohm_uses_r_notation(self, qtbot, library, families):
        items = _draw(
            library,
            families,
            "R",
            "4R7",
            variant="chip-smd/imperial/0805",
            pads=((0.0, 0.0), (1.0, 0.0)),
        )
        assert _texts(items) == ["4R7"]

    def test_undecodable_value_prints_nothing(self, qtbot, library, families):
        items = _draw(
            library,
            families,
            "R",
            "?",
            variant="chip-smd/imperial/0805",
            pads=((0.0, 0.0), (1.0, 0.0)),
        )
        assert _texts(items) == []

    def test_code_fits_inside_the_chip_body(self, qtbot, library, families):
        items = _draw(
            library,
            families,
            "R",
            "22k",
            variant="chip-smd/imperial/0805",
            pads=((0.0, 0.0), (1.0, 0.0)),
        )
        shapes = [it for it in items if isinstance(it, QAbstractGraphicsShapeItem)]
        labels = [it for it in items if isinstance(it, QGraphicsSimpleTextItem)]
        body = max(shapes, key=lambda it: it.sceneBoundingRect().width())
        assert body.sceneBoundingRect().contains(labels[0].sceneBoundingRect())
