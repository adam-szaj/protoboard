# SPDX-License-Identifier: MIT
"""``component_bbox`` margin must follow package *rigidity*, not electrical class.

Bug: an SMD resistor (class ``resistor`` ∈ ``_FLEXIBLE_LEAD_CLASSES``) was given
the flexible-lead ``_PAD_MARGIN`` as if its body floated with bendable leads,
even though its body is a rigid chip lying flat on the pads.  The fix reads
``is_rigid_package(fp.package)`` when the footprint is a ``PackageFootprint``,
falling back to the class test only for bare footprints with no package.
"""

from __future__ import annotations

import pytest

from protoboard.domain.components.footprints import GenericFootprint
from protoboard.domain.components.package.adapter import PackageFootprint
from protoboard.domain.components.package.family import resolve_variant
from protoboard.domain.layout.placement import (
    _BODY_MARGIN,
    _PAD_MARGIN,
    ComponentPlacement,
    component_bbox,
)
from protoboard.geometry import Point, Rect, Vector


def _catalogue():  # type: ignore[no-untyped-def]
    from protoboard.paths import BUILTIN_LIBRARY_DIR
    from protoboard.services.library.loader import load_library
    from protoboard.services.library.package_family_loader import (
        load_full_package_catalogue,
    )

    families = load_full_package_catalogue()
    lib = load_library([BUILTIN_LIBRARY_DIR], families)
    return lib, families


def _pl() -> ComponentPlacement:
    import uuid

    return ComponentPlacement(
        component_id=uuid.uuid4(), position=Point(5.0, 5.0), rotation=0, side="top",
    )


def _margin_of(bbox: Rect, pin_span_x: float) -> float:
    """Recover the symmetric margin baked into a bbox from a known pin span."""
    return (bbox.lx - pin_span_x) / 2.0


class TestComponentBboxRigidity:
    def test_smd_resistor_gets_body_margin(self) -> None:
        """SMD-0805 R is a rigid chip → _BODY_MARGIN, despite class 'resistor'."""
        lib, families = _catalogue()
        defn = lib.get("R")
        variant = resolve_variant(families, "SMD-0805/typ")
        assert variant is not None
        fp = PackageFootprint(variant.package)
        pl = _pl()

        bbox = component_bbox(fp, pl, defn.component_class)

        pins = [fp.pin_positions()[i] for i in range(fp.pin_count())]
        span_x = max(p.x for p in pins) - min(p.x for p in pins)
        assert _margin_of(bbox, span_x) == pytest.approx(_BODY_MARGIN)

    def test_axial_resistor_keeps_pad_margin(self) -> None:
        """The default axial R uses the BOARD_SOLVE solver → non-rigid → _PAD_MARGIN."""
        lib, _families = _catalogue()
        defn = lib.get("R")
        fp = defn.default_footprint()  # PackageFootprint over the axial DIN package
        pl = _pl()

        bbox = component_bbox(fp, pl, defn.component_class)

        pins = [fp.pin_positions()[i] for i in range(fp.pin_count())]
        span_x = max(p.x for p in pins) - min(p.x for p in pins)
        # axial pins run along one axis; the symmetric body margin is recoverable
        # from the x extent regardless of which axis the leads span.
        m = (bbox.lx - span_x) / 2.0
        assert m == pytest.approx(_PAD_MARGIN)

    def test_dip_ic_gets_body_margin(self) -> None:
        """A DIP IC is rigid → _BODY_MARGIN (class 'ic' was already non-flexible)."""
        lib, _families = _catalogue()
        defn = lib.get("74HC00")
        fp = defn.default_footprint()
        pl = _pl()

        bbox = component_bbox(fp, pl, defn.component_class)

        pins = [fp.pin_positions()[i] for i in range(fp.pin_count())]
        span_x = max(p.x for p in pins) - min(p.x for p in pins)
        assert _margin_of(bbox, span_x) == pytest.approx(_BODY_MARGIN)

    def test_bare_footprint_falls_back_to_class(self) -> None:
        """A footprint with no package keeps the class-based margin (back-compat).

        ``drag_detector`` / ``auto_placer`` pass bare footprints with no resolved
        variant; those must behave exactly as before.
        """
        fp = GenericFootprint([Vector(0.0, 2.0)])  # 2 pins along y; x span = 0
        pl = _pl()

        flexible = component_bbox(fp, pl, "resistor")
        rigid = component_bbox(fp, pl, "ic")

        assert flexible.lx / 2.0 == pytest.approx(_PAD_MARGIN)
        assert rigid.lx / 2.0 == pytest.approx(_BODY_MARGIN)
