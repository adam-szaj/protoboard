# SPDX-License-Identifier: MIT
"""Tests for collision masks: side-aware bits, AND-based collision, and the
'blocking body lights pin-side bits' mechanism (see spec)."""

from protoboard.domain.board.layer import BOTTOM, TOP
from protoboard.domain.components.package.mask import (
    PadCell,
    build_pad_mask,
    has_collision,
    masks_collide,
)
from protoboard.domain.components.package.shapes import (
    BOARD_DEFAULT,
    Leads,
    Prism,
)
from protoboard.geometry import Vector, Vector3D

from ..geometry_utils import _pt
VectorList = list[Vector]
def _two_pin_prism(steps_holes: list[Vector]) -> Prism:
    leads = Leads(
        direction=(Vector3D(0, 0, -1),) * (len(steps_holes) + 1),
        body_offset_mm=tuple(Vector(s.x * 2.5, s.y * 2.5) for s in steps_holes),
        board_offset=BOARD_DEFAULT,
    )
    return Prism(leads=leads, width_mm=2.5, length_mm=2.5, height_mm=2.5)


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
            hole for hole, b in body.items()
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
