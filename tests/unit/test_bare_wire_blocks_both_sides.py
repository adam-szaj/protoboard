# SPDX-License-Identifier: MIT
"""Regression test for the plated-through short bug.

A bare wire on layer X sits inside a plated-through hole, so its pad is
electrically continuous with layer Y at that position.  When the autorouter
later runs A* on layer Y looking for an insulated path, the pad held by the
bare wire must be off-limits — otherwise the insulated jumper's endpoint
lands on the bare wire's net and silently shorts.

The fix forces _build_wire_occupied to block both sides for every bare wire
regardless of the *both_sides* hint the caller passes.
"""

from __future__ import annotations

import uuid

from protoboard.domain.layout.wire import Wire
from protoboard.geometry import Point
from protoboard.services.routing.autorouter import _build_wire_occupied


def _bare(p1: tuple[float, float], p2: tuple[float, float], side: str) -> Wire:
    return Wire(
        id=uuid.uuid4(),
        path=[Point(*p1), Point(*p2)],
        insulated=False,
        side=side,  # type: ignore[arg-type]
    )


def _insulated(p1: tuple[float, float], p2: tuple[float, float], side: str) -> Wire:
    return Wire(
        id=uuid.uuid4(),
        path=[Point(*p1), Point(*p2)],
        insulated=True,
        side=side,  # type: ignore[arg-type]
    )


class TestBareWireBlocksBothSides:
    def test_bare_bottom_blocks_top_even_when_caller_asks_for_single_side(
        self,
    ) -> None:
        """A bare wire on bottom must also block the top layer (plated-through hole)."""
        wires = [_bare((0.5, 0.5), (1.5, 0.5), side="bottom")]
        occ = _build_wire_occupied(wires, exclude_wire_id=None, both_sides=False)
        # Both layers at every covered point must appear.
        assert (Point(0.5, 0.5), "bottom") in occ
        assert (Point(0.5, 0.5), "top") in occ
        assert (Point(1.5, 0.5), "bottom") in occ
        assert (Point(1.5, 0.5), "top") in occ

    def test_insulated_wire_keeps_single_side_default(self) -> None:
        """An insulated wire only blocks its own side when both_sides=False."""
        wires = [_insulated((0.5, 0.5), (1.5, 0.5), side="top")]
        occ = _build_wire_occupied(wires, exclude_wire_id=None, both_sides=False)
        assert (Point(0.5, 0.5), "top") in occ
        assert (Point(0.5, 0.5), "bottom") not in occ

    def test_insulated_wire_respects_both_sides_when_requested(self) -> None:
        wires = [_insulated((0.5, 0.5), (1.5, 0.5), side="top")]
        occ = _build_wire_occupied(wires, exclude_wire_id=None, both_sides=True)
        assert (Point(0.5, 0.5), "top") in occ
        assert (Point(0.5, 0.5), "bottom") in occ
