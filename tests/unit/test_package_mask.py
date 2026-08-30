# SPDX-License-Identifier: MIT
"""Tests for collision masks: side-aware bits, AND-based collision, and the
'blocking body lights pin-side bits' mechanism (see spec)."""

from protoboard.domain.board.layer import BOTTOM, TOP
from protoboard.domain.components.package.body import LeadArrangement
from protoboard.domain.components.package.mask import (
    PadCell,
    build_pad_mask,
    has_collision,
    masks_collide,
)
from protoboard.domain.components.package.shapes import (
    BOARD_DEFAULT,
    Cylinder,
    Leads,
    Prism,
)
from protoboard.geometry import Size3D, Vector, Vector3D

from ..geometry_utils import _pt

VectorList = list[Vector]


def _two_pin_prism(steps_holes: list[Vector]) -> Prism:
    leads = Leads(
        direction=(Vector3D(0, 0, -1),) * (len(steps_holes) + 1),
        body_offset_mm=tuple(Vector(s.dx * 2.5, s.dy * 2.5) for s in steps_holes),
        board_offset=BOARD_DEFAULT,
    )
    return Prism(leads=leads, size_mm=Size3D(2.5, 2.5, 2.5))


class TestHasCollision:
    def test_shared_bit_collides(self) -> None:
        assert has_collision(PadCell.PIN_TOP, PadCell.PIN_TOP)

    def test_disjoint_sides_no_collision(self) -> None:
        # Pin from top vs pin from bottom in same hole — no mount collision.
        assert not has_collision(PadCell.PIN_TOP, PadCell.PIN_BOTTOM)

    def test_empty_no_collision(self) -> None:
        assert not has_collision(PadCell(0), PadCell.PIN_TOP)


class TestBuildPadMask:
    def test_pins_set_pin_and_side(self) -> None:
        m = build_pad_mask(_two_pin_prism([Vector(1, 0)]), mount_side=TOP)
        assert m[_pt(0, 0)] & PadCell.PIN
        assert m[_pt(0, 0)] & PadCell.PIN_TOP
        assert not (m[_pt(0, 0)] & PadCell.PIN_BOTTOM)

    def test_mount_side_bottom(self) -> None:
        m = build_pad_mask(_two_pin_prism([Vector(1, 0)]), mount_side=BOTTOM)
        assert m[_pt(0, 0)] & PadCell.PIN_BOTTOM
        assert not (m[_pt(0, 0)] & PadCell.PIN_TOP)

    def test_high_standoff_body_sets_only_body_bit(self) -> None:
        # body_blocks=False → body holes carry BODY_TOP but NOT pin-side bits.
        m = build_pad_mask(
            _two_pin_prism([Vector(1, 0)]), mount_side=TOP, body_blocks=False
        )
        # a hole under body that is not a pin (body bbox is 1 hole wide here)
        body_holes = [h for h, b in m.items() if b & PadCell.BODY_TOP]
        assert body_holes
        for h in body_holes:
            if not (m[h] & PadCell.PIN):  # pure body hole
                assert not (m[h] & PadCell.PIN_TOP)

    def test_blocking_body_lights_pin_side(self) -> None:
        m = build_pad_mask(
            _two_pin_prism([Vector(1, 0)]), mount_side=TOP, body_blocks=True
        )
        for h, b in m.items():
            if b & PadCell.BODY_TOP:
                assert b & PadCell.PIN_TOP  # blocking body lit the pin-side bit


def _dimless_prism():
    # Regression shape: no explicit dims and no margin, so ``bbox()`` raises —
    # placing such a part used to crash PadIndex via ``body_holes``.
    from protoboard.domain.components.package.loader import build_package

    return build_package(
        {
            "shape": "prism",
            "dims": {},
            "leads": {
                "direction": [0, 0, -1],
                "body_offset": {"unit": 2.5, "value": [[1, 0]]},
                "board_offset": "default",
            },
        },
        "DIMLESS",
    )


class TestDimlessBodyMask:
    """A package with no derivable body extent occupies only its pins."""

    def test_body_holes_is_empty(self) -> None:
        from protoboard.domain.components.package.mask import body_holes

        assert body_holes(_dimless_prism()) == []

    def test_pad_mask_carries_pins_only(self) -> None:
        m = build_pad_mask(_dimless_prism(), mount_side=TOP)
        assert len(m) == 2
        for bits in m.values():
            assert bits & PadCell.PIN
            assert not (bits & PadCell.BODY_TOP)


class TestMasksCollide:
    def test_low_part_under_blocking_body_collides(self) -> None:
        # Blocking body on top at holes 0..N, and a part with a pin in one of
        # those holes mounted from top → collision via AND.
        body = build_pad_mask(
            _two_pin_prism([Vector(2, 0)]), mount_side=TOP, body_blocks=True
        )
        part = {_pt(1, 0): PadCell.PIN | PadCell.PIN_TOP}
        clash = masks_collide(body, part)
        assert clash  # hole (1,0) under the blocking body

    def test_part_under_high_standoff_body_ok(self) -> None:
        body = build_pad_mask(
            _two_pin_prism([Vector(2, 0)]), mount_side=TOP, body_blocks=False
        )
        # part at a pure-body hole (not a pin hole) from top — high standoff → ok
        body_only = [
            hole
            for hole, b in body.items()
            if (b & PadCell.BODY_TOP) and not (b & PadCell.PIN)
        ]
        assert body_only
        part = {body_only[0]: PadCell.PIN | PadCell.PIN_TOP}
        assert not masks_collide(body, part)

    def test_opposite_side_under_blocking_body_ok(self) -> None:
        # Even a blocking body on top doesn't block mounting from bottom.
        body = build_pad_mask(
            _two_pin_prism([Vector(2, 0)]), mount_side=TOP, body_blocks=True
        )
        part = {_pt(1, 0): PadCell.PIN | PadCell.PIN_BOTTOM}
        assert not masks_collide(body, part)


class TestRadialCylinderMask:
    """Regression: radial-cylinder bbox is centred on the pad centroid.

    The old pin-1-anchored ``Rect(0, 0, d, d)`` claimed a phantom row of
    holes below every radial part, so two 2.5 mm LEDs placed side by side
    (adjacent rows or columns) tripped DRC BODY_OVERLAP even though the
    bodies at most touch (tangent discs at the raster pitch are legal).
    """

    @staticmethod
    def _led25() -> Cylinder:
        # The LED/led25 shape: 2.5 mm disc over two pads one hole apart.
        leads = Leads(
            direction=(Vector3D(0, 0, -1), Vector3D(0, 0, -1)),
            body_offset_mm=(Vector(1.25, 0.0),),
            board_offset=(Vector(1.0, 0.0),),
        )
        return Cylinder(
            diameter_mm=2.5,
            length_mm=4.5,
            leads=leads,
            arrangement=LeadArrangement.RADIAL,
        )

    def test_bbox_centred_between_pads(self) -> None:
        bb = self._led25().bbox()
        assert (bb.x, bb.y, bb.lx, bb.ly) == (0.0, -0.5, 1.0, 1.0)

    def test_mask_claims_only_pin_holes(self) -> None:
        mask = build_pad_mask(self._led25())
        assert set(mask.keys()) == {_pt(0, 0), _pt(1, 0)}

    def test_side_by_side_leds_do_not_collide(self) -> None:
        mask = build_pad_mask(self._led25())
        next_row = {_pt(p.x, p.y + 1): b for p, b in mask.items()}
        next_col = {_pt(p.x + 2, p.y): b for p, b in mask.items()}
        assert not masks_collide(mask, next_row)
        assert not masks_collide(mask, next_col)
        # Directly on top of each other still collides, of course.
        assert masks_collide(mask, dict(mask))
