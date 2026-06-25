# SPDX-License-Identifier: MIT
"""Tests for physical Layout — placements, wires, strip cuts and
pin world-position resolution (including rotation).
"""

import uuid

import pytest

from protoboard.domain.components.footprints import DIL, SIL
from protoboard.domain.layout.layout import Layout
from protoboard.domain.layout.placement import ComponentPlacement, pin_world_position
from protoboard.domain.layout.wire import Wire, WireCollection
from protoboard.geometry import Point


def _approx(a: float, b: float, tol: float = 1e-9) -> bool:
    return abs(a - b) <= tol


def _approx_point(a: Point, b: Point, tol: float = 1e-9) -> bool:
    return _approx(a.x, b.x, tol) and _approx(a.y, b.y, tol)


class TestComponentPlacement:
    def test_construct(self) -> None:
        comp_id = uuid.uuid4()
        pl = ComponentPlacement(
            component_id=comp_id,
            position=Point(3.0, 5.0),
            rotation=0,
            side="top",
        )
        assert pl.component_id == comp_id
        assert pl.position == Point(3.0, 5.0)
        assert pl.rotation == 0
        assert pl.side == "top"

    def test_invalid_rotation_raises(self) -> None:
        with pytest.raises(ValueError):
            ComponentPlacement(
                component_id=uuid.uuid4(),
                position=Point(0.0, 0.0),
                rotation=45,
                side="top",
            )

    def test_invalid_side_raises(self) -> None:
        with pytest.raises(ValueError):
            ComponentPlacement(
                component_id=uuid.uuid4(),
                position=Point(0.0, 0.0),
                rotation=0,
                side="middle",  # type: ignore[arg-type]
            )

    def test_mutable(self) -> None:
        pl = ComponentPlacement(
            component_id=uuid.uuid4(),
            position=Point(0.0, 0.0),
            rotation=0,
            side="top",
        )
        pl.rotation = 90
        assert pl.rotation == 90


class TestPinWorldPosition:
    """pin_world_position(footprint, placement, pin_number) → absolute Point."""

    def test_rotation_0_sil3(self) -> None:
        from protoboard.domain.layout.placement import pin_world_position

        fp = SIL(3)
        pl = ComponentPlacement(
            component_id=uuid.uuid4(),
            position=Point(2.0, 3.0),
            rotation=0,
            side="top",
        )
        # Pin 1 at origin → world (2, 3). Pin 2 at (0,1) → world (2, 4).
        assert _approx_point(pin_world_position(fp, pl, 1), Point(2.0, 3.0))
        assert _approx_point(pin_world_position(fp, pl, 2), Point(2.0, 4.0))
        assert _approx_point(pin_world_position(fp, pl, 3), Point(2.0, 5.0))

    def test_rotation_90_sil3(self) -> None:
        from protoboard.domain.layout.placement import pin_world_position

        fp = SIL(3)
        pl = ComponentPlacement(
            component_id=uuid.uuid4(),
            position=Point(2.0, 3.0),
            rotation=90,
            side="top",
        )
        # Rotation 90° CW: (x, y) → (y, -x). Pin 2 rel=(0,1) → rot=(1,0).
        # World: pin1=(2,3), pin2=(3,3), pin3=(4,3).
        assert _approx_point(pin_world_position(fp, pl, 1), Point(2.0, 3.0))
        assert _approx_point(pin_world_position(fp, pl, 2), Point(3.0, 3.0))
        assert _approx_point(pin_world_position(fp, pl, 3), Point(4.0, 3.0))

    def test_rotation_180_sil3(self) -> None:
        from protoboard.domain.layout.placement import pin_world_position

        fp = SIL(3)
        pl = ComponentPlacement(
            component_id=uuid.uuid4(),
            position=Point(2.0, 3.0),
            rotation=180,
            side="top",
        )
        # 180°: (x,y) → (-x, -y). Pin2 rel=(0,1) → (0,-1). World=(2,2).
        assert _approx_point(pin_world_position(fp, pl, 1), Point(2.0, 3.0))
        assert _approx_point(pin_world_position(fp, pl, 2), Point(2.0, 2.0))
        assert _approx_point(pin_world_position(fp, pl, 3), Point(2.0, 1.0))

    def test_rotation_270_sil3(self) -> None:
        from protoboard.domain.layout.placement import pin_world_position

        fp = SIL(3)
        pl = ComponentPlacement(
            component_id=uuid.uuid4(),
            position=Point(2.0, 3.0),
            rotation=270,
            side="top",
        )
        # 270° CW (= 90° CCW): (x,y) → (-y, x). Pin2 rel=(0,1) → (-1,0). World=(1,3).
        assert _approx_point(pin_world_position(fp, pl, 1), Point(2.0, 3.0))
        assert _approx_point(pin_world_position(fp, pl, 2), Point(1.0, 3.0))
        assert _approx_point(pin_world_position(fp, pl, 3), Point(0.0, 3.0))

    def test_dip8_rotation_0(self) -> None:
        from protoboard.domain.layout.placement import pin_world_position

        fp = DIL(8)
        pl = ComponentPlacement(
            component_id=uuid.uuid4(),
            position=Point(5.0, 5.0),
            rotation=0,
            side="top",
        )
        # Pin 1 at (0,0) → world (5,5). Pin 8 at (3,0) → world (8,5).
        assert _approx_point(pin_world_position(fp, pl, 1), Point(5.0, 5.0))
        assert _approx_point(pin_world_position(fp, pl, 8), Point(8.0, 5.0))


class TestFlipComponent:
    """flip_component swaps pin-1 and pin-2 pads.

    Key invariant: after flip, rotation is always around the new pin-1 pad
    (i.e. the old pin-2 pad), because placement.position is always the
    footprint origin = pin-1-without-offset anchor.
    """

    def _make_pl(
        self,
        position: Point,
        rotation: int = 0,
        pin_offsets: dict[int, Point] | None = None,
    ) -> ComponentPlacement:
        return ComponentPlacement(
            component_id=uuid.uuid4(),
            position=position,
            rotation=rotation,
            side="top",
            pin_offsets=pin_offsets or {},
        )

    # ------------------------------------------------------------------
    # Basic swap
    # ------------------------------------------------------------------

    def test_flip_swaps_pad_positions(self) -> None:
        from protoboard.domain.layout.placement import flip_component, pin_world_position

        fp = SIL(2)
        pl = self._make_pl(Point(2.0, 3.0), rotation=0)
        # rotation=0, SIL(2): pin1=(2,3), pin2=(2,4)
        p1_before = pin_world_position(fp, pl, 1)
        p2_before = pin_world_position(fp, pl, 2)

        flipped = flip_component(fp, pl)

        assert _approx_point(pin_world_position(fp, flipped, 1), p2_before)
        assert _approx_point(pin_world_position(fp, flipped, 2), p1_before)

    def test_flip_swaps_pad_positions_rotation90(self) -> None:
        from protoboard.domain.layout.placement import flip_component, pin_world_position

        fp = SIL(2)
        pl = self._make_pl(Point(5.0, 5.0), rotation=90)
        # rotation=90: pin1=(5,5), pin2=(6,5)
        p1_before = pin_world_position(fp, pl, 1)
        p2_before = pin_world_position(fp, pl, 2)

        flipped = flip_component(fp, pl)

        assert _approx_point(pin_world_position(fp, flipped, 1), p2_before)
        assert _approx_point(pin_world_position(fp, flipped, 2), p1_before)

    # ------------------------------------------------------------------
    # Rotation anchor: before flip → around pin-1; after flip → around pin-2
    # ------------------------------------------------------------------

    def test_before_flip_rotation_is_around_pin1(self) -> None:
        """Changing rotation leaves pin-1 world position fixed."""
        from protoboard.domain.layout.placement import pin_world_position

        fp = SIL(2)
        pl = self._make_pl(Point(3.0, 4.0), rotation=0)
        p1_rot0 = pin_world_position(fp, pl, 1)

        pl.rotation = 90
        p1_rot90 = pin_world_position(fp, pl, 1)

        pl.rotation = 180
        p1_rot180 = pin_world_position(fp, pl, 1)

        # pin-1 world position must not change with rotation
        assert _approx_point(p1_rot0, Point(3.0, 4.0))
        assert _approx_point(p1_rot90, Point(3.0, 4.0))
        assert _approx_point(p1_rot180, Point(3.0, 4.0))

    def test_after_flip_rotation_is_around_local_origin(self) -> None:
        """After flip, rotation is still around placement.position (local origin),
        not around either pad.  placement.position is unchanged by flip."""
        from protoboard.domain.layout.placement import flip_component

        fp = SIL(2)
        pl = self._make_pl(Point(3.0, 4.0), rotation=0)
        origin = pl.position

        flipped = flip_component(fp, pl)

        assert _approx_point(flipped.position, origin)

    # ------------------------------------------------------------------
    # Flip with pin_offsets
    # ------------------------------------------------------------------

    def test_flip_with_offset_on_pin2(self) -> None:
        """pin_offsets on pin-2 are carried over and still hit the correct pad."""
        from protoboard.domain.layout.placement import flip_component, pin_world_position

        fp = SIL(2)
        # pin2 offset shifts it 1 raster right in local frame (rotation=90 → world +y)
        pl = self._make_pl(Point(2.0, 3.0), rotation=0, pin_offsets={2: Point(1.0, 0.0)})
        p1_before = pin_world_position(fp, pl, 1)  # (2, 3)
        p2_before = pin_world_position(fp, pl, 2)  # pin2 local=(1,1) → world (2, 4) + offset → (3, 4)? let's check

        flipped = flip_component(fp, pl)

        assert _approx_point(pin_world_position(fp, flipped, 1), p2_before)
        assert _approx_point(pin_world_position(fp, flipped, 2), p1_before)

    def test_flip_twice_restores_original_pad_positions(self) -> None:
        """Double flip is identity on pad positions."""
        from protoboard.domain.layout.placement import flip_component, pin_world_position

        fp = SIL(2)
        pl = self._make_pl(Point(1.0, 1.0), rotation=90)
        p1_orig = pin_world_position(fp, pl, 1)
        p2_orig = pin_world_position(fp, pl, 2)

        double_flipped = flip_component(fp, flip_component(fp, pl))

        assert _approx_point(pin_world_position(fp, double_flipped, 1), p1_orig)
        assert _approx_point(pin_world_position(fp, double_flipped, 2), p2_orig)


class TestRedragOffsetInverse:
    """Verify that the world→local offset inversion used by _commit_redrag is correct.

    The forward pipeline is:
        local = fp_pos + offset  →  X-mirror (bottom)  →  rotate  →  + position

    The inverse (computing offset from a target world position) must be:
        world_delta = target - baseline_world  →  unrotate  →  unmirror (bottom)

    This test checks that applying the computed offset to pin_world_position
    reproduces the original target world position.
    """

    def _make_pl(
        self,
        position: Point,
        rotation: int = 0,
        side: str = "top",
        pin_offsets: dict[int, Point] | None = None,
    ) -> ComponentPlacement:
        return ComponentPlacement(
            component_id=uuid.uuid4(),
            position=position,
            rotation=rotation,
            side=side,  # type: ignore[arg-type]
            pin_offsets=pin_offsets or {},
        )

    def _compute_offset(
        self, target_world: Point, baseline_world: Point, rotation: int, side: str
    ) -> Point:
        from protoboard.domain.layout.placement import unrotate
        delta = Point(target_world.x - baseline_world.x, target_world.y - baseline_world.y)
        off = unrotate(delta, rotation)
        if side == "bottom":
            off = Point(-off.x, off.y)
        return off

    def test_top_rotation0(self) -> None:
        fp = SIL(2)
        pl = self._make_pl(Point(5.0, 5.0), rotation=0, side="top")
        baseline = pin_world_position(fp, pl, 2)  # (5, 6) — no offset

        target = Point(5.0, 8.0)
        off = self._compute_offset(target, baseline, pl.rotation, pl.side)
        pl2 = self._make_pl(Point(5.0, 5.0), rotation=0, side="top", pin_offsets={2: off})
        assert _approx_point(pin_world_position(fp, pl2, 2), target)

    def test_top_rotation90(self) -> None:
        fp = SIL(2)
        pl = self._make_pl(Point(5.0, 5.0), rotation=90, side="top")
        baseline = pin_world_position(fp, pl, 2)  # rotation90: (6, 5)

        target = Point(8.0, 5.0)
        off = self._compute_offset(target, baseline, pl.rotation, pl.side)
        pl2 = self._make_pl(Point(5.0, 5.0), rotation=90, side="top", pin_offsets={2: off})
        assert _approx_point(pin_world_position(fp, pl2, 2), target)

    def test_bottom_rotation0(self) -> None:
        fp = SIL(2)
        # bottom, rotation=0: pin2 fp_pos=(0,1) → X-mirror→(0,1) → rotate→(0,1) → +(5,5) = (5,6)
        pl = self._make_pl(Point(5.0, 5.0), rotation=0, side="bottom")
        baseline = pin_world_position(fp, pl, 2)

        target = Point(5.0, 8.0)
        off = self._compute_offset(target, baseline, pl.rotation, pl.side)
        pl2 = self._make_pl(Point(5.0, 5.0), rotation=0, side="bottom", pin_offsets={2: off})
        assert _approx_point(pin_world_position(fp, pl2, 2), target)

    def test_bottom_rotation90(self) -> None:
        fp = SIL(2)
        pl = self._make_pl(Point(5.0, 5.0), rotation=90, side="bottom")
        baseline = pin_world_position(fp, pl, 2)

        target = Point(8.0, 5.0)
        off = self._compute_offset(target, baseline, pl.rotation, pl.side)
        pl2 = self._make_pl(Point(5.0, 5.0), rotation=90, side="bottom", pin_offsets={2: off})
        assert _approx_point(pin_world_position(fp, pl2, 2), target)

    def test_both_pins_stay_at_origin_after_offset(self) -> None:
        """Verify oba piny mają tę samą world position gdy offset sprowadza je do (0,0)."""
        fp = SIL(2)
        pl = self._make_pl(Point(3.0, 3.0), rotation=0, side="top")
        # Without offset pin1=(3,3), pin2=(3,4).
        # Force pin2 to world (3,3) — same as pin1.
        baseline = pin_world_position(fp, pl, 2)
        target = Point(3.0, 3.0)
        off = self._compute_offset(target, baseline, pl.rotation, pl.side)
        pl2 = self._make_pl(Point(3.0, 3.0), rotation=0, side="top", pin_offsets={2: off})
        assert _approx_point(pin_world_position(fp, pl2, 2), target)
        # off should be (0, -1) to cancel the (0,1) footprint position of pin2
        assert _approx(off.x, 0.0) and _approx(off.y, -1.0)


class TestWireSegment:
    def test_construct(self) -> None:
        wire_id = uuid.uuid4()
        w = Wire(
            id=wire_id,
            path=[Point(0.0, 0.0), Point(1.0, 0.0), Point(2.0, 0.0)],
            insulated=False,
            side="top",
        )
        assert w.id == wire_id
        assert len(w.path) == 3
        assert w.insulated is False
        assert w.side == "top"
        assert w.color is None

    def test_insulated_wire_with_color(self) -> None:
        w = Wire(
            id=uuid.uuid4(),
            path=[Point(0.0, 0.0), Point(1.0, 0.0)],
            insulated=True,
            side="bottom",
            color="#FF0000",
        )
        assert w.insulated is True
        assert w.color == "#FF0000"

    def test_single_point_path_raises(self) -> None:
        with pytest.raises(ValueError):
            Wire(
                id=uuid.uuid4(),
                path=[Point(0.0, 0.0)],
                insulated=False,
                side="top",
            )

    def test_empty_path_raises(self) -> None:
        with pytest.raises(ValueError):
            Wire(
                id=uuid.uuid4(),
                path=[],
                insulated=False,
                side="top",
            )

    def test_invalid_side_raises(self) -> None:
        with pytest.raises(ValueError):
            Wire(
                id=uuid.uuid4(),
                path=[Point(0.0, 0.0), Point(1.0, 0.0)],
                insulated=False,
                side="both",  # type: ignore[arg-type]
            )

    def test_mutable(self) -> None:
        w = Wire(
            id=uuid.uuid4(),
            path=[Point(0.0, 0.0), Point(1.0, 0.0)],
            insulated=False,
            side="top",
        )
        w.insulated = True
        assert w.insulated is True


class TestLayout:
    def test_empty_layout(self) -> None:
        lay = Layout(placements={}, wires=WireCollection([]))
        assert len(lay.placements) == 0
        assert len(lay.wires) == 0

    def test_add_placement(self) -> None:
        comp_id = uuid.uuid4()
        pl = ComponentPlacement(
            component_id=comp_id,
            position=Point(1.0, 2.0),
            rotation=0,
            side="top",
        )
        lay = Layout(placements={comp_id: pl}, wires=WireCollection([]))
        assert comp_id in lay.placements

    def test_add_wire(self) -> None:
        w = Wire(
            id=uuid.uuid4(),
            path=[Point(0.0, 0.0), Point(5.0, 0.0)],
            insulated=True,
            side="top",
        )
        lay = Layout(placements={}, wires=WireCollection([w]))
        assert len(lay.wires) == 1
