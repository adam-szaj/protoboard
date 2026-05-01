# SPDX-License-Identifier: MIT
"""Tests for physical Layout — placements, wires, strip cuts and
pin world-position resolution (including rotation).
"""

import uuid

import pytest

from protoboard.domain.components.footprints import DIP, SIL
from protoboard.domain.layout.layout import Layout
from protoboard.domain.layout.placement import ComponentPlacement
from protoboard.domain.layout.wire import WireSegment
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

        fp = DIP(8)
        pl = ComponentPlacement(
            component_id=uuid.uuid4(),
            position=Point(5.0, 5.0),
            rotation=0,
            side="top",
        )
        # Pin 1 at (0,0) → world (5,5). Pin 8 at (3,0) → world (8,5).
        assert _approx_point(pin_world_position(fp, pl, 1), Point(5.0, 5.0))
        assert _approx_point(pin_world_position(fp, pl, 8), Point(8.0, 5.0))


class TestWireSegment:
    def test_construct(self) -> None:
        wire_id = uuid.uuid4()
        w = WireSegment(
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
        w = WireSegment(
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
            WireSegment(
                id=uuid.uuid4(),
                path=[Point(0.0, 0.0)],
                insulated=False,
                side="top",
            )

    def test_empty_path_raises(self) -> None:
        with pytest.raises(ValueError):
            WireSegment(
                id=uuid.uuid4(),
                path=[],
                insulated=False,
                side="top",
            )

    def test_invalid_side_raises(self) -> None:
        with pytest.raises(ValueError):
            WireSegment(
                id=uuid.uuid4(),
                path=[Point(0.0, 0.0), Point(1.0, 0.0)],
                insulated=False,
                side="both",  # type: ignore[arg-type]
            )

    def test_mutable(self) -> None:
        w = WireSegment(
            id=uuid.uuid4(),
            path=[Point(0.0, 0.0), Point(1.0, 0.0)],
            insulated=False,
            side="top",
        )
        w.insulated = True
        assert w.insulated is True


class TestLayout:
    def test_empty_layout(self) -> None:
        lay = Layout(placements={}, wires=[])
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
        lay = Layout(placements={comp_id: pl}, wires=[])
        assert comp_id in lay.placements

    def test_add_wire(self) -> None:
        w = WireSegment(
            id=uuid.uuid4(),
            path=[Point(0.0, 0.0), Point(5.0, 0.0)],
            insulated=True,
            side="top",
        )
        lay = Layout(placements={}, wires=[w])
        assert len(lay.wires) == 1
