# SPDX-License-Identifier: MIT
"""Cross-side shared-pad must count in the simplify M ≤ N test.

Regression: simplify accepted a path edit on a TOP insulated wire that
moved a diagonal endpoint onto a pad already held by a BOTTOM insulated
wire belonging to a foreign net.  Same metrics (length, corners) →
simplify saw "no change" → accept.  But the new path shares a pad with
the cross-side wire that the baseline did not, raising on-pad contacts
from 0 to 1.

Bug location: ``_candidate_safe_against_others`` cross-side branch
only checked collinear segment overlap (two wires sharing a track
visible from above).  Pad coincidence between a top and a bottom wire
on a plated-through hole was invisible to the rule.

Fix: cross-side branch now also runs
``_count_insulated_pad_contacts`` (sum-based for insulated, strict
per-wire for bare) just like the same-side path.
"""

from __future__ import annotations

import uuid

from protoboard.domain.layout.wire import Wire
from protoboard.geometry import Point
from protoboard.services.routing.postprocess import (
    _candidate_safe_against_others,
)


def _segs(pts: list[Point]) -> list[tuple[Point, Point]]:
    return [(pts[i], pts[i + 1]) for i in range(len(pts) - 1)]


class TestCrossSideInsulatedSharedPad:
    def test_candidate_reusing_cross_side_pad_rejected(self) -> None:
        """Candidate ends a diagonal on the pad a cross-side wire already
        rests on — baseline had no such contact.  Must reject."""
        wire_id = uuid.uuid4()
        # Cross-side foreign wire (insulated, bottom) anchored at
        # (22.5, 22.5) — the shared pad.
        foreign = Wire(
            id=uuid.uuid4(),
            path=[Point(20.5, 22.5), Point(22.5, 22.5), Point(22.5, 25.5)],
            insulated=True,
            side="bottom",
        )
        # Baseline subpath on top, diagonal away from the shared pad.
        original = [Point(23.5, 21.5), Point(22.5, 21.5), Point(21.5, 22.5)]
        # Candidate moves the diagonal endpoint onto (22.5, 22.5).
        candidate = [Point(23.5, 21.5), Point(22.5, 22.5), Point(21.5, 22.5)]

        ok = _candidate_safe_against_others(
            candidate_segs=_segs(candidate),
            other_wires=[foreign],
            wire_id=wire_id,
            wire_side="top",
            wire_net_id=uuid.uuid4(),  # our net
            other_wire_nets={foreign.id: uuid.uuid4()},  # foreign net
            original_endpoints=(original[0], original[-1]),
            original_subpath_segs=_segs(original),
        )
        assert ok is False, (
            "candidate adds a new cross-side shared-pad contact at "
            "(22.5, 22.5); simplify must reject"
        )

    def test_candidate_keeping_existing_cross_side_pad_ok(self) -> None:
        """Pad was already shared in the baseline → candidate that keeps
        the contact (or removes it) is fine."""
        wire_id = uuid.uuid4()
        foreign = Wire(
            id=uuid.uuid4(),
            path=[Point(22.5, 22.5), Point(22.5, 25.5)],
            insulated=True,
            side="bottom",
        )
        # Both baseline and candidate touch (22.5, 22.5) once.
        original = [Point(23.5, 22.5), Point(22.5, 22.5), Point(20.5, 22.5)]
        candidate = [Point(23.5, 22.5), Point(22.5, 22.5), Point(20.5, 22.5)]

        ok = _candidate_safe_against_others(
            candidate_segs=_segs(candidate),
            other_wires=[foreign],
            wire_id=wire_id,
            wire_side="top",
            wire_net_id=uuid.uuid4(),
            other_wire_nets={foreign.id: uuid.uuid4()},
            original_endpoints=(original[0], original[-1]),
            original_subpath_segs=_segs(original),
        )
        assert ok is True

    def test_same_net_cross_side_shared_pad_allowed(self) -> None:
        """Same-net cross-side shared pad is intentional (e.g. via)."""
        wire_id = uuid.uuid4()
        net_id = uuid.uuid4()
        foreign_same_net = Wire(
            id=uuid.uuid4(),
            path=[Point(22.5, 22.5), Point(22.5, 25.5)],
            insulated=True,
            side="bottom",
        )
        original = [Point(23.5, 21.5), Point(22.5, 21.5), Point(21.5, 22.5)]
        candidate = [Point(23.5, 21.5), Point(22.5, 22.5), Point(21.5, 22.5)]

        ok = _candidate_safe_against_others(
            candidate_segs=_segs(candidate),
            other_wires=[foreign_same_net],
            wire_id=wire_id,
            wire_side="top",
            wire_net_id=net_id,
            other_wire_nets={foreign_same_net.id: net_id},  # SAME net
            original_endpoints=(original[0], original[-1]),
            original_subpath_segs=_segs(original),
        )
        assert ok is True


class TestCrossSideBareSharedPad:
    def test_candidate_reusing_cross_side_bare_pad_rejected(self) -> None:
        """Bare wire on the other side sitting on the shared pad is an
        electrical short the moment our wire lands on the same pad."""
        wire_id = uuid.uuid4()
        bare_foreign = Wire(
            id=uuid.uuid4(),
            path=[Point(20.5, 22.5), Point(22.5, 22.5), Point(22.5, 25.5)],
            insulated=False,
            side="bottom",
        )
        # Our wire is insulated top — pad (22.5, 22.5) doesn't exist in
        # baseline subpath.
        original = [Point(23.5, 21.5), Point(22.5, 21.5), Point(21.5, 22.5)]
        candidate = [Point(23.5, 21.5), Point(22.5, 22.5), Point(21.5, 22.5)]

        ok = _candidate_safe_against_others(
            candidate_segs=_segs(candidate),
            other_wires=[bare_foreign],
            wire_id=wire_id,
            wire_side="top",
            wire_net_id=uuid.uuid4(),
            other_wire_nets={bare_foreign.id: uuid.uuid4()},
            original_endpoints=(original[0], original[-1]),
            original_subpath_segs=_segs(original),
        )
        assert ok is False
