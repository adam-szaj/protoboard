# SPDX-License-Identifier: MIT
"""Cross-section parity: the rendered BODY must follow the PINS under rotation.

A component's pins are placed by ``pin_world_position`` (which carries the
placement rotation + bottom-side reflection); the body silhouette must go
through the *same* local→world transform so the two never drift apart.  A real
bug lived exactly here: the plain-bbox body path (packages with no dedicated
DIP/SIL silhouette — single-row headers, dev-boards, odd-column prisms) drew an
axis-aligned rectangle shifted only by the pin-1 anchor, so the pins rotated
while the body stayed upright.

These tests exercise one representative of every major package type end to end
through ``body_shape_for`` + the real library, and assert two invariants for a
non-square part:

* **rotation** — rotating the placement 0°→90° swaps the body bbox width/height
  (the body turns with the pin cloud), and 180° preserves them;
* **centering** — the body bbox centre tracks the pin-cloud centre at every
  orientation (the body never drifts off its pins).

They are deliberately empirical (measure the drawn QGraphicsItems' geometry),
not formula-based — the bug they guard was invisible to a pin-coordinate check.
"""

from __future__ import annotations

import uuid

import pytest
from PySide6.QtGui import QColor
from PySide6.QtWidgets import QGraphicsItemGroup
from pytestqt.qtbot import QtBot

from protoboard.domain.components.instance import ComponentInstance
from protoboard.domain.components.package import resolved_footprint
from protoboard.domain.layout.placement import (
    ComponentPlacement,
    pin_world_position,
)
from protoboard.geometry import Point
from protoboard.types import Side
from protoboard.gui.renderers.body_shapes import body_shape_for
from protoboard.controller.theme import DEFAULT_THEME
from protoboard.services.library.loader import load_library
from protoboard.services.library.package_family_loader import (
    load_full_package_catalogue,
)

_RED = QColor(200, 50, 50)
_DARK = QColor(30, 30, 30)


# One representative component per major package type.  ``variant`` selects a
# non-default package variant where the type is only reachable that way (SMD).
# Each entry is a NON-SQUARE part so a 90° rotation is observable.
#   (label, definition_name, package_variant)
_SAMPLES: list[tuple[str, str, str | None]] = [
    ("cylinder_axial", "1N4001", None),          # 2-pin axial capsule
    ("cylinder_radial_3p", "2N2222", None),      # TO-92 radial
    ("disc_radial", "C", None),                  # 2-pin disc
    ("smd_chip", "C", "SMD-0805/typ"),           # 2-pin surface-mount prism
    ("prism_sil_header", "PinHeader_1x05", None),  # single-row → bbox path
    ("prism_sil_jst", "JST_PH_3pin", None),      # single-row connector → bbox
    ("prism_sil_rnet", "R_Net_8", None),         # single-row network → bbox
    ("prism_dip", "74HC00", None),               # two-column DIP → silhouette
    ("prism_to220", "7805", None),               # 3-pin regulator prism
    ("devboard", "Arduino_Nano", None),          # dev-board → bbox + rings
]


@pytest.fixture(scope="module")
def _library() -> object:
    fams = load_full_package_catalogue()
    lib = load_library([__import__("pathlib").Path("library")], fams)
    return (lib, fams)


def _body_and_pins(
    library: object, defn_name: str, variant: str | None, rot: int, side: Side,
) -> tuple[str, float, float, Point, Point]:
    """Draw the body at *(rot, side)* and return
    ``(shape_name, body_w, body_h, body_centre, pin_cloud_centre)``.
    """
    lib, fams = library  # type: ignore[misc]
    defn = lib.get(defn_name)
    inst = ComponentInstance(
        id=uuid.uuid4(), definition_ref=defn_name, refdes="X1", value="v",
        package_variant=variant,
    )
    try:
        inst = inst.bind(defn, fams)
    except Exception:
        pass
    fp = resolved_footprint(defn, inst, fams)
    pl = ComponentPlacement(
        component_id=uuid.uuid4(), position=Point(20.0, 20.0),
        rotation=rot, side=side,
    )
    positions = [
        pin_world_position(fp, pl, n + 1) for n in range(fp.pin_count())
    ]
    shape = body_shape_for(defn, pl, inst, package_families=fams)
    group = QGraphicsItemGroup()
    for item in shape.draw(positions, _RED, _DARK, 1.0, 0.0, DEFAULT_THEME):
        group.addToGroup(item)
    r = group.boundingRect()
    body_centre = Point(r.x() + r.width() / 2.0, r.y() + r.height() / 2.0)
    xs = [p.x for p in positions]
    ys = [p.y for p in positions]
    pin_centre = Point((min(xs) + max(xs)) / 2.0, (min(ys) + max(ys)) / 2.0)
    return type(shape).__name__, r.width(), r.height(), body_centre, pin_centre


def _pin_span(
    library: object, defn_name: str, variant: str | None, rot: int,
) -> tuple[float, float]:
    lib, fams = library  # type: ignore[misc]
    defn = lib.get(defn_name)
    inst = ComponentInstance(
        id=uuid.uuid4(), definition_ref=defn_name, refdes="X1", value="v",
        package_variant=variant,
    )
    try:
        inst = inst.bind(defn, fams)
    except Exception:
        pass
    fp = resolved_footprint(defn, inst, fams)
    pl = ComponentPlacement(
        component_id=uuid.uuid4(), position=Point(20.0, 20.0),
        rotation=rot, side="top",
    )
    pos = [pin_world_position(fp, pl, n + 1) for n in range(fp.pin_count())]
    xs = [p.x for p in pos]
    ys = [p.y for p in pos]
    return max(xs) - min(xs), max(ys) - min(ys)


class TestBodyFollowsPinsUnderRotation:
    """Every major package type: the body rotates with the pins and stays
    centred on them.  The single-row / dev-board entries are the ones the
    plain-bbox fix repaired; the DIP / cylinder entries guard against a
    regression in the silhouette / capsule paths."""

    @pytest.mark.parametrize(
        "label,name,variant", _SAMPLES, ids=[s[0] for s in _SAMPLES],
    )
    def test_body_bbox_swaps_wh_on_90(
        self, qtbot: QtBot, _library: object,
        label: str, name: str, variant: str | None,
    ) -> None:
        # Only meaningful when the pin cloud itself is non-square.
        pin_w0, pin_h0 = _pin_span(_library, name, variant, 0)
        if abs(pin_w0 - pin_h0) < 0.5:
            pytest.skip(f"{label}: pin cloud is square — rotation not observable")

        sh0, w0, h0, _bc0, _pc0 = _body_and_pins(_library, name, variant, 0, "top")
        sh90, w90, h90, _bc90, _pc90 = _body_and_pins(
            _library, name, variant, 90, "top")

        assert sh0 == sh90
        # The body turns with the pins: width/height swap within a tolerance
        # that absorbs asymmetric leads / markers.
        assert w0 == pytest.approx(h90, abs=0.35), (
            f"{label}: body did not rotate — rot0 w={w0:.2f} but rot90 h={h90:.2f}"
        )
        assert h0 == pytest.approx(w90, abs=0.35), (
            f"{label}: body did not rotate — rot0 h={h0:.2f} but rot90 w={w90:.2f}"
        )

    @pytest.mark.parametrize(
        "label,name,variant", _SAMPLES, ids=[s[0] for s in _SAMPLES],
    )
    def test_body_bbox_preserved_on_180(
        self, qtbot: QtBot, _library: object,
        label: str, name: str, variant: str | None,
    ) -> None:
        _sh0, w0, h0, _bc0, _pc0 = _body_and_pins(
            _library, name, variant, 0, "top")
        _sh180, w180, h180, _bc180, _pc180 = _body_and_pins(
            _library, name, variant, 180, "top")
        # A 180° turn maps the bbox onto itself.
        assert w0 == pytest.approx(w180, abs=0.35)
        assert h0 == pytest.approx(h180, abs=0.35)

    @pytest.mark.parametrize(
        "label,name,variant", _SAMPLES, ids=[s[0] for s in _SAMPLES],
    )
    @pytest.mark.parametrize("rot", [0, 90, 180, 270])
    def test_body_centre_tracks_pin_centre(
        self, qtbot: QtBot, _library: object,
        label: str, name: str, variant: str | None, rot: int,
    ) -> None:
        # The body stays centred on its pins at every orientation (no drift).
        _sh, _w, _h, body_c, pin_c = _body_and_pins(
            _library, name, variant, rot, "top")
        assert body_c.x == pytest.approx(pin_c.x, abs=0.6), (
            f"{label} rot={rot}: body cx {body_c.x:.2f} != pin cx {pin_c.x:.2f}"
        )
        assert body_c.y == pytest.approx(pin_c.y, abs=0.6), (
            f"{label} rot={rot}: body cy {body_c.y:.2f} != pin cy {pin_c.y:.2f}"
        )


class TestBodyFollowsPinsOnFlip:
    """Flipping to the bottom side must keep the body centred on the pins
    (the bottom-side reflection is part of the shared transform)."""

    @pytest.mark.parametrize(
        "label,name,variant", _SAMPLES, ids=[s[0] for s in _SAMPLES],
    )
    def test_body_centre_tracks_pins_on_bottom(
        self, qtbot: QtBot, _library: object,
        label: str, name: str, variant: str | None,
    ) -> None:
        _sh, _w, _h, body_c, pin_c = _body_and_pins(
            _library, name, variant, 0, "bottom")
        assert body_c.x == pytest.approx(pin_c.x, abs=0.6)
        assert body_c.y == pytest.approx(pin_c.y, abs=0.6)
