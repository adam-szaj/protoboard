# SPDX-License-Identifier: MIT
"""Tests for ``_candidate_no_new_pin_contact`` — the simplify pad-state gate.

The gate distinguishes two contact classes:
* **Hard contacts** — absolute reject if any new one appears (alien pins,
  hard-blocking ComponentBody, shorts, bare-foreign-net).
* **Soft contacts** — counted M ≤ N (candidate ≤ baseline) across the
  whole path.  Foreign insulated wires sharing a pad with our wire fall
  here: simplify may shift them from one pad to another but not multiply
  their count.

Reproduces reference_big_project case 2026-06-09 where wire 66a006bd was
incorrectly rejected because its canonical shorter path shared a pad with
the same foreign insulated wire — just at a different pad coordinate.
"""

from __future__ import annotations

import uuid

from protoboard.domain.board.board import Board
from protoboard.domain.board.boxes import BoardGrid, PadAtom, SizePolicy
from protoboard.domain.board.pad_state import (
    ComponentBody,
    InsulatedWireCrossing,
    InsulatedWireItem,
    PadIndex,
    PinItem,
)
from protoboard.domain.board.pads import PadLayers
from protoboard.domain.layout.layout import Layout
from protoboard.domain.layout.placement import PlacementCollection
from protoboard.domain.schematic.schematic import Schematic
from protoboard.geometry import Point
from protoboard.services.routing.postprocess import (
    _candidate_no_new_pin_contact,
)


def _perfboard(rows: int, cols: int) -> Board:
    return Board(
        root=BoardGrid(
            width_policy=SizePolicy.HUG,
            height_policy=SizePolicy.HUG,
            cell=PadAtom(
                width_policy=SizePolicy.HUG,
                height_policy=SizePolicy.HUG,
                layers=PadLayers(top=True, bottom=True, plated_through=True),
            ),
            rows=rows,
            cols=cols,
            row_gap=0.0,
            col_gap=0.0,
        )
    )


def _empty_layout() -> Layout:
    return Layout(placements=PlacementCollection({}))


def _empty_schematic() -> Schematic:
    return Schematic(nets={})


def _new_index() -> PadIndex:
    return PadIndex(
        _perfboard(5, 5),
        _empty_layout(),
        _empty_schematic(),
        {},
        {},
    )


def _net() -> uuid.UUID:
    return uuid.uuid4()


class TestSoftInsulatedCrossingShift:
    """Soft contacts: alien insulated wires sharing a pad are counted,
    not absolute-rejected.  Simplify can shift them across pads as long
    as the total count does not grow."""

    def test_shift_alien_insulated_from_one_pad_to_another_accepted(self) -> None:
        """Original path passes through pad A which has a foreign insulated
        wire.  Candidate shortcut passes through pad B which has the SAME
        foreign insulated wire at a different position.  Total soft count
        is 1 before AND after → accept."""
        idx = _new_index()
        our_net = _net()
        their_net = _net()
        # Foreign insulated wire occupies pads (2.5, 2.5) AND (1.5, 2.5)
        # as mid-segment crossings on top side.
        pad_a = idx.pad_at(Point(2.5, 2.5))
        pad_b = idx.pad_at(Point(1.5, 2.5))
        assert pad_a is not None and pad_b is not None
        pad_a._add_item(InsulatedWireCrossing(id=uuid.uuid4(), net=their_net), "top")
        pad_b._add_item(InsulatedWireCrossing(id=uuid.uuid4(), net=their_net), "top")
        # Original wire path passes through pad A but not B.
        original = [Point(0.5, 2.5), Point(2.5, 2.5), Point(4.5, 2.5)]
        # Candidate path passes through pad B but not A.
        candidate = [Point(0.5, 2.5), Point(1.5, 2.5), Point(4.5, 2.5)]
        # Both have exactly 1 soft contact with foreign insulated — accept.
        assert _candidate_no_new_pin_contact(
            candidate,
            original,
            idx,
            wire_side="top",
            wire_net_id=our_net,
            wire_is_insulated=True,
        )

    def test_growing_soft_count_rejected(self) -> None:
        """Original passes through 1 alien-insulated pad, candidate would
        pass through 3.  Total grows → reject."""
        idx = _new_index()
        our_net = _net()
        their_net = _net()
        # Foreign insulated occupies 3 pads in a row at y=2.5.
        for pos in (Point(1.5, 2.5), Point(2.5, 2.5), Point(3.5, 2.5)):
            pad = idx.pad_at(pos)
            assert pad is not None
            pad._add_item(
                InsulatedWireCrossing(id=uuid.uuid4(), net=their_net),
                "top",
            )
        # Original ortho path: only (2.5, 2.5) touches a foreign pad.
        # Use pure-ortho moves to avoid mid-segment ambiguity.
        original = [Point(2.5, 0.5), Point(2.5, 2.5), Point(2.5, 4.5)]
        # Candidate path runs along y=2.5: hits all three foreign pads.
        candidate = [
            Point(0.5, 2.5),
            Point(1.5, 2.5),
            Point(2.5, 2.5),
            Point(3.5, 2.5),
            Point(4.5, 2.5),
        ]
        assert not _candidate_no_new_pin_contact(
            candidate,
            original,
            idx,
            wire_side="top",
            wire_net_id=our_net,
            wire_is_insulated=True,
        )

    def test_zero_baseline_one_candidate_rejected(self) -> None:
        """Original passes through 0 alien-insulated pads, candidate would
        pass through 1.  No shift to do — strict reject."""
        idx = _new_index()
        our_net = _net()
        their_net = _net()
        pad_b = idx.pad_at(Point(2.5, 2.5))
        assert pad_b is not None
        pad_b._add_item(
            InsulatedWireCrossing(id=uuid.uuid4(), net=their_net),
            "top",
        )
        # Original does NOT touch (2.5, 2.5).
        original = [Point(0.5, 0.5), Point(4.5, 0.5)]
        # Candidate WOULD touch (2.5, 2.5) — first new contact → reject.
        candidate = [Point(0.5, 0.5), Point(2.5, 2.5), Point(4.5, 0.5)]
        assert not _candidate_no_new_pin_contact(
            candidate,
            original,
            idx,
            wire_side="top",
            wire_net_id=our_net,
            wire_is_insulated=True,
        )

    def test_same_net_insulated_pad_is_not_counted(self) -> None:
        """A pad with OUR net's insulated wire vertex isn't a 'foreign'
        contact.  Candidate may touch any number of own-net pads."""
        idx = _new_index()
        our_net = _net()
        # Same-net insulated wire on (2.5, 2.5).
        pad_b = idx.pad_at(Point(2.5, 2.5))
        assert pad_b is not None
        pad_b._add_item(
            InsulatedWireCrossing(id=uuid.uuid4(), net=our_net),
            "top",
        )
        original = [Point(0.5, 0.5), Point(4.5, 0.5)]
        candidate = [Point(0.5, 0.5), Point(2.5, 2.5), Point(4.5, 0.5)]
        # Same-net — not a foreign contact → accept.
        assert _candidate_no_new_pin_contact(
            candidate,
            original,
            idx,
            wire_side="top",
            wire_net_id=our_net,
            wire_is_insulated=True,
        )

    def test_opposite_side_alien_insulated_does_not_block(self) -> None:
        """Foreign insulated wire on BOTTOM side doesn't block TOP-side
        candidate — they live on different layers."""
        idx = _new_index()
        our_net = _net()
        their_net = _net()
        pad_b = idx.pad_at(Point(2.5, 2.5))
        assert pad_b is not None
        pad_b._add_item(
            InsulatedWireCrossing(id=uuid.uuid4(), net=their_net),
            "bottom",
        )
        original = [Point(0.5, 0.5), Point(4.5, 0.5)]
        candidate = [Point(0.5, 0.5), Point(2.5, 2.5), Point(4.5, 0.5)]
        # Top wire over bottom-side foreign insulated — irrelevant.
        assert _candidate_no_new_pin_contact(
            candidate,
            original,
            idx,
            wire_side="top",
            wire_net_id=our_net,
            wire_is_insulated=True,
        )


class TestHardContactsAbsoluteReject:
    """Hard contacts always reject when introduced as NEW (not present on
    original path).  Shift not allowed."""

    def test_alien_pin_on_candidate_rejects(self) -> None:
        idx = _new_index()
        our_net = _net()
        their_net = _net()
        pad = idx.pad_at(Point(2.5, 2.5))
        assert pad is not None
        pad._add_item(
            PinItem(id=uuid.uuid4(), pin_number=1, net=their_net),
            "top",
        )
        original = [Point(0.5, 0.5), Point(4.5, 0.5)]
        candidate = [Point(0.5, 0.5), Point(2.5, 2.5), Point(4.5, 0.5)]
        assert not _candidate_no_new_pin_contact(
            candidate,
            original,
            idx,
            wire_side="top",
            wire_net_id=our_net,
            wire_is_insulated=True,
        )

    def test_hard_component_body_on_candidate_rejects(self) -> None:
        idx = _new_index()
        our_net = _net()
        pad = idx.pad_at(Point(2.5, 2.5))
        assert pad is not None
        # SMD-like body that physically blocks routing.
        pad._add_item(
            ComponentBody(id=uuid.uuid4(), blocks_routing=True),
            "top",
        )
        original = [Point(0.5, 0.5), Point(4.5, 0.5)]
        candidate = [Point(0.5, 0.5), Point(2.5, 2.5), Point(4.5, 0.5)]
        assert not _candidate_no_new_pin_contact(
            candidate,
            original,
            idx,
            wire_side="top",
            wire_net_id=our_net,
            wire_is_insulated=True,
        )

    def test_non_blocking_component_body_does_not_reject(self) -> None:
        """Default ComponentBody (blocks_routing=False) is informational
        only — routing under it is allowed."""
        idx = _new_index()
        our_net = _net()
        pad = idx.pad_at(Point(2.5, 2.5))
        assert pad is not None
        pad._add_item(
            ComponentBody(id=uuid.uuid4(), blocks_routing=False),
            "top",
        )
        original = [Point(0.5, 0.5), Point(4.5, 0.5)]
        candidate = [Point(0.5, 0.5), Point(2.5, 2.5), Point(4.5, 0.5)]
        assert _candidate_no_new_pin_contact(
            candidate,
            original,
            idx,
            wire_side="top",
            wire_net_id=our_net,
            wire_is_insulated=True,
        )

    def test_alien_insulated_endpoint_counted_as_soft(self) -> None:
        """An ``InsulatedWireItem`` (endpoint of an alien insulated wire)
        is treated softly — same as ``InsulatedWireCrossing`` (mid-vertex).
        Both represent the alien wire physically resting on the pad."""
        idx = _new_index()
        our_net = _net()
        their_net = _net()
        # Foreign endpoint on (2.5, 0.5).
        pad_a = idx.pad_at(Point(2.5, 0.5))
        assert pad_a is not None
        pad_a._add_item(
            InsulatedWireItem(id=uuid.uuid4(), net=their_net),
            "top",
        )
        # Foreign mid-vertex on (2.5, 4.5).
        pad_b = idx.pad_at(Point(2.5, 4.5))
        assert pad_b is not None
        pad_b._add_item(
            InsulatedWireCrossing(id=uuid.uuid4(), net=their_net),
            "top",
        )
        # Original passes through the endpoint pad — 1 soft contact.
        # Layout: pin1(0.5,2.5) → (2.5, 0.5) → pin2(4.5,2.5).  Diagonal
        # legs so no extra pads are touched mid-segment.
        original = [Point(0.5, 2.5), Point(2.5, 0.5), Point(4.5, 2.5)]
        # Candidate shifts to the mid-vertex pad — also 1 soft contact.
        candidate = [Point(0.5, 2.5), Point(2.5, 4.5), Point(4.5, 2.5)]
        assert _candidate_no_new_pin_contact(
            candidate,
            original,
            idx,
            wire_side="top",
            wire_net_id=our_net,
            wire_is_insulated=True,
        )
