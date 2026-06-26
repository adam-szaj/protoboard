# SPDX-License-Identifier: MIT
"""Tests for redrag_feasibility — the shared predicate (overlay == commit).

Pure domain: a bendable grab bends one lead (body fixed); a rigid grab
translates the body, the other leads absorbing the shift within their own
reach discs.  Legal iff every pin's final hole is in its own disc and every
moved rigid pin lands on a board pad.
"""

from __future__ import annotations

import uuid

from protoboard.domain.components.definition import PinBendSpec
from protoboard.domain.components.footprints import SIL
from protoboard.domain.components.package import redrag_feasibility
from protoboard.domain.layout.placement import (
    ComponentPlacement,
    pin_world_position,
)
from protoboard.geometry import Point, Vector


def _pl(pos: Point = Point(0.0, 0.0)) -> ComponentPlacement:
    return ComponentPlacement(
        component_id=uuid.uuid4(), position=pos, rotation=0, side="top",
    )


def _free(spec_max: float = 5.0) -> PinBendSpec:
    return PinBendSpec(min_reach=0.0, max_reach=spec_max,
                       default_offset=Vector(0.0, 0.0))


_RIGID = PinBendSpec(min_reach=0.0, max_reach=0.0)  # is_free() == False


# A generous board: every integer hole in a 6×6 region is a landing pad.
_PADS = frozenset(
    (float(x), float(y)) for x in range(-2, 7) for y in range(-2, 7)
)


class TestBendableGrab:
    def test_bendable_grab_pure_bend(self) -> None:
        # SIL(3): pins (0,0),(0,1),(0,2).  Grab pin 3 (bendable), move it to a
        # pad in its disc → only pin 3's offset changes, body fixed.
        fp = SIL(3)
        pl = _pl()
        specs = {1: _RIGID, 2: _RIGID, 3: _free(5.0)}
        target = Point(2.0, 2.0)  # 2 holes from pin 3 default (0,2)
        r = redrag_feasibility(
            footprint=fp, placement=pl, specs=specs,
            grabbed_pin=3, target=target, free_pads=_PADS,
        )
        assert r.ok and r.new_placement is not None
        assert r.new_placement.position == pl.position  # body did not move
        assert set(r.new_placement.pin_offsets) == {3}
        # Pin 3 now lands on the target; pins 1,2 unchanged.
        assert pin_world_position(fp, r.new_placement, 3) == target
        assert pin_world_position(fp, r.new_placement, 1) == Point(0.0, 0.0)

    def test_bendable_grab_out_of_disc_rejected(self) -> None:
        fp = SIL(3)
        specs = {1: _RIGID, 2: _RIGID, 3: _free(3.0)}  # max 3 holes
        target = Point(0.0, 6.0)  # 4 holes from pin 3 default (0,2) > 3
        r = redrag_feasibility(
            footprint=fp, placement=_pl(), specs=specs,
            grabbed_pin=3, target=target, free_pads=_PADS,
        )
        assert not r.ok and r.new_placement is None

    def test_bent_pin_can_return_to_its_default_despite_min_reach(self) -> None:
        # Regression (2026-06-26): after several re-drags a pin sits off its
        # default; the user re-drags it back ONTO its default.  min_reach (the
        # 2-pin spacing floor) must NOT block that — the bend disc is [0, max].
        fp = SIL(3)
        # min_reach 1.0 like a 2-pin spacing floor leaking into an N-pin spec.
        specs = {
            1: PinBendSpec(min_reach=1.0, max_reach=4.0),
            2: _RIGID,
            3: _RIGID,
        }
        # Pin 1 currently bent to (2,0); its default is the origin (0,0).
        pl = ComponentPlacement(
            component_id=uuid.uuid4(), position=Point(0.0, 0.0), rotation=0,
            side="top", pin_offsets={1: Vector(2.0, 0.0)},
        )
        assert pin_world_position(fp, pl, 1) == Point(2.0, 0.0)
        r = redrag_feasibility(
            footprint=fp, placement=pl, specs=specs,
            grabbed_pin=1, target=Point(0.0, 0.0), free_pads=_PADS,
        )
        assert r.ok and r.new_placement is not None
        # Pin 1 is back on its default; its offset is cleared.
        assert pin_world_position(fp, r.new_placement, 1) == Point(0.0, 0.0)
        assert 1 not in r.new_placement.pin_offsets


class TestRigidGrab:
    def test_rigid_grab_translates_body_axial_2pin(self) -> None:
        # SIL(2): pin1 (0,0) rigid, pin2 (0,1) bendable.  Grab the rigid pin 1
        # and move it one hole over → the body slides and pin 2 absorbs it,
        # staying on its original pad (0,1).
        fp = SIL(2)
        pl = _pl()
        specs = {1: _RIGID, 2: _free(5.0)}
        target = Point(1.0, 0.0)  # pin 1 from (0,0) → (1,0)
        r = redrag_feasibility(
            footprint=fp, placement=pl, specs=specs,
            grabbed_pin=1, target=target, free_pads=_PADS,
        )
        assert r.ok and r.new_placement is not None
        assert r.new_placement.position == Point(1.0, 0.0)  # body shifted
        # Pin 1 reaches the target; pin 2 stays on its old pad (absorbed shift).
        assert pin_world_position(fp, r.new_placement, 1) == target
        assert pin_world_position(fp, r.new_placement, 2) == Point(0.0, 1.0)

    def test_rigid_grab_rejected_when_other_lead_out_of_disc(self) -> None:
        # Pin 2 can only bend 1 hole; shifting the body 3 holes would force pin 2
        # 3 holes off its pad → out of its disc → rejected.
        fp = SIL(2)
        specs = {1: _RIGID, 2: _free(1.0)}
        target = Point(3.0, 0.0)
        r = redrag_feasibility(
            footprint=fp, placement=_pl(), specs=specs,
            grabbed_pin=1, target=target, free_pads=_PADS,
        )
        assert not r.ok

    def test_rigid_grab_other_rigid_pin_off_pad_rejected(self) -> None:
        # Both pins rigid; the body shift lands pin 2 on a non-pad coordinate.
        fp = SIL(2)
        specs = {1: _RIGID, 2: _RIGID}
        # Shift by (0.5, 0): pin 2 → (0.5, 1.0), not in the integer-hole pad set.
        target = Point(0.5, 0.0)
        r = redrag_feasibility(
            footprint=fp, placement=_pl(), specs=specs,
            grabbed_pin=1, target=target, free_pads=_PADS,
        )
        assert not r.ok

    def test_rigid_grab_other_rigid_pin_on_pad_ok(self) -> None:
        # Both rigid, but the shift lands pin 2 squarely on a board pad → ok.
        fp = SIL(2)
        specs = {1: _RIGID, 2: _RIGID}
        target = Point(2.0, 0.0)  # pin 2 → (2,1), a valid pad
        r = redrag_feasibility(
            footprint=fp, placement=_pl(), specs=specs,
            grabbed_pin=1, target=target, free_pads=_PADS,
        )
        assert r.ok and r.new_placement is not None
        assert pin_world_position(fp, r.new_placement, 2) == Point(2.0, 1.0)


class TestOverlayCommitAgree:
    def test_overlay_and_commit_agree(self) -> None:
        # For a set of candidate pads, redrag_feasibility(...).ok must match
        # whether the pad is a legal landing — the anti-divergence guarantee.
        fp = SIL(3)
        pl = _pl()
        specs = {1: _RIGID, 2: _RIGID, 3: _free(3.0)}  # pin 3 disc radius 3
        default3 = pin_world_position(fp, pl, 3)  # (0, 2)
        for x in range(-2, 7):
            for y in range(-2, 7):
                pad = Point(float(x), float(y))
                dist = ((pad.x - default3.x) ** 2 + (pad.y - default3.y) ** 2) ** 0.5
                expected = dist <= 3.0  # within pin 3's disc
                r = redrag_feasibility(
                    footprint=fp, placement=pl, specs=specs,
                    grabbed_pin=3, target=pad, free_pads=_PADS,
                )
                assert r.ok == expected, (pad, dist)
