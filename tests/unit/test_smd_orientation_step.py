# SPDX-License-Identifier: MIT
"""``smd_orientation_step`` — the 45° orientation step of a 2-pin SMD chip.

A rigid surface-mount 2-pin part has exactly one degree of freedom: the
orientation of pin 2 around the pin-1 anchor, over the 8 neighbour pads.
The step is a cyclic walk over those 8 directions (45° is NOT a grid
isometry — the span alternates 1 ↔ √2), stored canonically as the floor
cardinal in ``rotation`` plus, for diagonals, a unit residual in
``pin_offsets[2]``.  Pin 1 never moves.
"""

from __future__ import annotations

import math
import uuid

from protoboard.domain.components.footprints import GenericFootprint
from protoboard.domain.components.package.reach import smd_orientation_step
from protoboard.domain.layout.placement import (
    ComponentPlacement,
    pin_world_position,
)
from protoboard.geometry import Point, Vector


def _placement(x: float = 5.0, y: float = 5.0, **kw: object) -> ComponentPlacement:
    return ComponentPlacement(
        component_id=uuid.uuid4(), position=Point(x, y), rotation=0,
        side="top", **kw,  # type: ignore[arg-type]
    )


def _fp() -> GenericFootprint:
    # pin 1 at local (0,0) — the anchor; pin 2 default at (1,0).
    return GenericFootprint([Vector(1.0, 0.0)])


def _span(fp: GenericFootprint, pl: ComponentPlacement) -> float:
    p1 = pin_world_position(fp, pl, 1)
    p2 = pin_world_position(fp, pl, 2)
    return math.hypot(p2.x - p1.x, p2.y - p1.y)


def _dir(fp: GenericFootprint, pl: ComponentPlacement) -> tuple[int, int]:
    p1 = pin_world_position(fp, pl, 1)
    p2 = pin_world_position(fp, pl, 2)
    return (round(p2.x - p1.x), round(p2.y - p1.y))


def test_step_cw_from_default_reaches_diagonal() -> None:
    fp, pl = _fp(), _placement()
    stepped = smd_orientation_step(fp, pl, 1)
    assert stepped is not None
    # Pin 1 anchored; pin 2 on the diagonal between E (rot 0) and the
    # rotation-90 cardinal (0, -1) — i.e. (1, -1).
    assert pin_world_position(fp, stepped, 1) == Point(5.0, 5.0)
    assert _dir(fp, stepped) == (1, -1)
    # Canonical: floor cardinal in rotation, unit residual on pin 2 only.
    assert stepped.rotation == 0
    assert set(stepped.pin_offsets) == {2}


def test_two_steps_equal_pure_90_rotation() -> None:
    fp, pl = _fp(), _placement()
    s1 = smd_orientation_step(fp, pl, 1)
    assert s1 is not None
    s2 = smd_orientation_step(fp, s1, 1)
    assert s2 is not None
    # A cardinal state is a pure rotation: no offsets at all.
    assert s2.rotation == 90
    assert s2.pin_offsets == {}
    assert pin_world_position(fp, s2, 1) == Point(5.0, 5.0)
    assert _dir(fp, s2) == (0, -1)


def test_eight_steps_close_the_cycle() -> None:
    fp, pl = _fp(), _placement()
    cur = pl
    seen: list[tuple[int, int]] = []
    for _ in range(8):
        nxt = smd_orientation_step(fp, cur, 1)
        assert nxt is not None
        assert pin_world_position(fp, nxt, 1) == Point(5.0, 5.0)
        assert math.isclose(_span(fp, nxt), 1.0) or math.isclose(
            _span(fp, nxt), math.sqrt(2)
        )
        seen.append(_dir(fp, nxt))
        cur = nxt
    # All 8 neighbour directions visited exactly once, back to the start.
    assert len(set(seen)) == 8
    assert cur.rotation == pl.rotation
    assert cur.pin_offsets == {}
    assert _dir(fp, cur) == _dir(fp, pl)


def test_ccw_step_reaches_the_mirror_diagonal() -> None:
    # The pads on the other side of pin 1 — unreachable by the old re-drag
    # without a prior 180° rotation — are one CCW step away.
    fp, pl = _fp(), _placement()
    stepped = smd_orientation_step(fp, pl, -1)
    assert stepped is not None
    assert pin_world_position(fp, stepped, 1) == Point(5.0, 5.0)
    assert _dir(fp, stepped) == (1, 1)
    assert stepped.rotation == 270  # floor cardinal of the (1,1) diagonal
    assert set(stepped.pin_offsets) == {2}


def test_stretched_legacy_state_is_not_steppable() -> None:
    # A pre-fix re-drag could stretch the chip to a 2-hole span; such a state
    # has no orientation index — the caller falls back to the 90° rotation.
    fp, pl = _fp(), _placement()
    pl.pin_offsets = {2: Vector(1.0, 0.0)}  # pin 2 at (7,5): span 2
    assert smd_orientation_step(fp, pl, 1) is None


def test_same_pad_legacy_state_is_not_steppable() -> None:
    fp, pl = _fp(), _placement()
    pl.pin_offsets = {2: Vector(-1.0, 0.0)}  # pin 2 on pin 1's pad: span 0
    assert smd_orientation_step(fp, pl, 1) is None


def test_non_unit_pitch_is_not_steppable() -> None:
    fp = GenericFootprint([Vector(2.0, 0.0)])  # 2-hole pitch
    assert smd_orientation_step(fp, _placement(), 1) is None


def test_bottom_side_cycles_and_anchors_pin1() -> None:
    fp = _fp()
    pl = _placement()
    pl.side = "bottom"
    anchor = pin_world_position(fp, pl, 1)
    cur = pl
    for _ in range(8):
        nxt = smd_orientation_step(fp, cur, 1)
        assert nxt is not None
        assert pin_world_position(fp, nxt, 1) == anchor
        assert math.isclose(_span(fp, nxt), 1.0) or math.isclose(
            _span(fp, nxt), math.sqrt(2)
        )
        cur = nxt
    assert _dir(fp, cur) == _dir(fp, pl)
