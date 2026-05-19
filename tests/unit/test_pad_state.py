# SPDX-License-Identifier: MIT
"""Tests for Pad subclass propagation and Board.make_pad factory."""

from __future__ import annotations

import uuid

import pytest

from protoboard.domain.board.board import Board
from protoboard.domain.board.boxes import BoardGrid, PadAtom, SizePolicy
from protoboard.domain.board.pad_state import (
    SHORT_CIRCUIT,
    BareWireCrossing,
    BareWireItem,
    ComponentBody,
    ConnectedDoubleSidedBoardPad,
    InsulatedWireCrossing,
    InsulatedWireItem,
    IsolatedDoubleSidedBoardPad,
    PinItem,
    SingleSidedBoardPad,
)
from protoboard.domain.board.pads import PadLayers
from protoboard.geometry import Point


@pytest.fixture
def board() -> Board:
    """Minimal 1×1 perfboard for make_pad tests."""
    return Board(
        root=BoardGrid(
            width_policy=SizePolicy.HUG, height_policy=SizePolicy.HUG,
            cell=PadAtom(
                width_policy=SizePolicy.HUG, height_policy=SizePolicy.HUG,
                layers=PadLayers(top=True, bottom=True, plated_through=True),
            ),
            rows=1, cols=1, row_gap=0, col_gap=0,
        )
    )


# ---------------------------------------------------------------------------
# Factory selection
# ---------------------------------------------------------------------------


class TestBoardMakePadFactory:
    def test_plated_through_returns_connected(self, board: Board) -> None:
        pad = board.make_pad(
            Point(0.5, 0.5),
            PadLayers(top=True, bottom=True, plated_through=True),
        )
        assert isinstance(pad, ConnectedDoubleSidedBoardPad)

    def test_both_sides_unplated_returns_isolated(self, board: Board) -> None:
        pad = board.make_pad(
            Point(0.5, 0.5),
            PadLayers(top=True, bottom=True, plated_through=False),
        )
        assert isinstance(pad, IsolatedDoubleSidedBoardPad)

    def test_top_only_returns_single_sided(self, board: Board) -> None:
        pad = board.make_pad(Point(0.5, 0.5), PadLayers(top=True, bottom=False))
        assert isinstance(pad, SingleSidedBoardPad)

    def test_bottom_only_returns_single_sided(self, board: Board) -> None:
        pad = board.make_pad(Point(0.5, 0.5), PadLayers(top=False, bottom=True))
        assert isinstance(pad, SingleSidedBoardPad)

    def test_strip_group_id_forwarded(self, board: Board) -> None:
        pad = board.make_pad(
            Point(0.5, 0.5),
            PadLayers(top=True, bottom=True, plated_through=True),
            strip_group_id=7,
        )
        assert pad.strip_group_id == 7


# ---------------------------------------------------------------------------
# Helpers for propagation tests
# ---------------------------------------------------------------------------


def _net() -> uuid.UUID:
    return uuid.uuid4()


def _pin(net: uuid.UUID | None = None) -> PinItem:
    return PinItem(id=uuid.uuid4(), pin_number=1, net=net)


def _bare(net: uuid.UUID | None = None) -> BareWireItem:
    return BareWireItem(id=uuid.uuid4(), net=net)


def _bare_xing(net: uuid.UUID | None = None) -> BareWireCrossing:
    return BareWireCrossing(id=uuid.uuid4(), net=net)


def _ins(net: uuid.UUID | None = None) -> InsulatedWireItem:
    return InsulatedWireItem(id=uuid.uuid4(), net=net)


def _ins_xing(net: uuid.UUID | None = None) -> InsulatedWireCrossing:
    return InsulatedWireCrossing(id=uuid.uuid4(), net=net)


def _body() -> ComponentBody:
    return ComponentBody(id=uuid.uuid4())


PT = Point(0.5, 0.5)


# ---------------------------------------------------------------------------
# ConnectedDoubleSidedBoardPad — plated-through
# ---------------------------------------------------------------------------


class TestConnectedDoubleSidedBoardPad:
    def _make(self) -> ConnectedDoubleSidedBoardPad:
        return ConnectedDoubleSidedBoardPad(
            position=PT,
            layers=PadLayers(top=True, bottom=True, plated_through=True),
        )

    def test_empty(self) -> None:
        p = self._make()
        assert p.electrical_net == (None, None)
        assert p.is_free("top") and p.is_free("bottom")

    def test_bare_top_propagates_to_both(self) -> None:
        p = self._make()
        n = _net()
        p._add_item(_bare(n), "top")
        assert p.electrical_net == (n, n)

    def test_bare_bottom_propagates_to_both(self) -> None:
        p = self._make()
        n = _net()
        p._add_item(_bare(n), "bottom")
        assert p.electrical_net == (n, n)

    def test_insulated_endpoint_top_electrical_both(self) -> None:
        # InsulatedWireItem (endpoint) is electrical contact at the solder
        # joint.  Plated-through propagates to both.
        p = self._make()
        n = _net()
        p._add_item(_ins(n), "top")
        assert p.electrical_net == (n, n)

    def test_insulated_crossing_no_electrical(self) -> None:
        # Mid-segment of an insulated wire is purely air contact.
        p = self._make()
        p._add_item(_ins_xing(_net()), "top")
        assert p.electrical_net == (None, None)
        # But physically blocks the top side.
        assert len(p.physically_blocked_on("top")) == 1
        assert len(p.physically_blocked_on("bottom")) == 0

    def test_pin_propagates_both_sides(self) -> None:
        p = self._make()
        n = _net()
        p._add_item(_pin(n), "top")
        assert p.electrical_net == (n, n)
        # Pin physically blocks both sides (through-hole).
        assert len(p.physically_blocked_on("top")) == 1
        assert len(p.physically_blocked_on("bottom")) == 1

    def test_component_body_no_electrical(self) -> None:
        p = self._make()
        p._add_item(_body(), "top")
        assert p.electrical_net == (None, None)
        assert len(p.physically_blocked_on("top")) == 1
        assert len(p.physically_blocked_on("bottom")) == 0


# ---------------------------------------------------------------------------
# IsolatedDoubleSidedBoardPad — sides independent unless through-hole
# ---------------------------------------------------------------------------


class TestIsolatedDoubleSidedBoardPad:
    def _make(self) -> IsolatedDoubleSidedBoardPad:
        return IsolatedDoubleSidedBoardPad(
            position=PT,
            layers=PadLayers(top=True, bottom=True, plated_through=False),
        )

    def test_bare_top_only_to_top(self) -> None:
        p = self._make()
        n = _net()
        p._add_item(_bare(n), "top")
        assert p.electrical_net == (n, None)

    def test_bare_bottom_only_to_bottom(self) -> None:
        p = self._make()
        n = _net()
        p._add_item(_bare(n), "bottom")
        assert p.electrical_net == (None, n)

    def test_insulated_endpoint_top_only(self) -> None:
        p = self._make()
        n = _net()
        p._add_item(_ins(n), "top")
        assert p.electrical_net == (n, None)

    def test_pin_propagates_both(self) -> None:
        # Pin bridges the hole via its metal lead.
        p = self._make()
        n = _net()
        p._add_item(_pin(n), "top")
        assert p.electrical_net == (n, n)


# ---------------------------------------------------------------------------
# SingleSidedBoardPad — only armed side carries net
# ---------------------------------------------------------------------------


class TestSingleSidedBoardPad:
    def test_bare_on_armed_side_only(self) -> None:
        # Top-only board.
        p = SingleSidedBoardPad(
            position=PT,
            layers=PadLayers(top=True, bottom=False),
        )
        n = _net()
        p._add_item(_bare(n), "top")
        assert p.electrical_net == (n, None)

    def test_bare_on_unarmed_side_no_electrical(self) -> None:
        # Top-only board, wire placed on bottom (no copper).  Physical
        # blocker is still recorded but no net contribution.
        p = SingleSidedBoardPad(
            position=PT,
            layers=PadLayers(top=True, bottom=False),
        )
        n = _net()
        p._add_item(_bare(n), "bottom")
        assert p.electrical_net == (None, None)
        assert len(p.physically_blocked_on("bottom")) == 1

    def test_pin_propagates_both_even_unarmed(self) -> None:
        # Footnote `*` in user's table: pin exposes conductor on both
        # faces regardless of board type.
        p = SingleSidedBoardPad(
            position=PT,
            layers=PadLayers(top=True, bottom=False),
        )
        n = _net()
        p._add_item(_pin(n), "top")
        assert p.electrical_net == (n, n)


# ---------------------------------------------------------------------------
# Multi-source net tracking & short detection
# ---------------------------------------------------------------------------


class TestMultiSourceTracking:
    def test_two_same_net_wires_remove_one_keeps_net(self) -> None:
        p = ConnectedDoubleSidedBoardPad(
            position=PT,
            layers=PadLayers(top=True, bottom=True, plated_through=True),
        )
        n = _net()
        w1 = _bare(n)
        w2 = _bare(n)
        p._add_item(w1, "top")
        p._add_item(w2, "top")
        assert p.electrical_net == (n, n)
        p._remove_item(w1.id)
        # Still net n because w2 remains.
        assert p.electrical_net == (n, n)
        p._remove_item(w2.id)
        assert p.electrical_net == (None, None)

    def test_different_nets_on_same_side_returns_short_sentinel(self) -> None:
        p = ConnectedDoubleSidedBoardPad(
            position=PT,
            layers=PadLayers(top=True, bottom=True, plated_through=True),
        )
        n1, n2 = _net(), _net()
        p._add_item(_bare(n1), "top")
        p._add_item(_bare(n2), "top")
        # Both sides see SHORT_CIRCUIT because plated-through propagates.
        assert p.electrical_net_on("top") is SHORT_CIRCUIT
        assert p.electrical_net_on("bottom") is SHORT_CIRCUIT

    def test_remove_item_round_trip_identity(self) -> None:
        p = ConnectedDoubleSidedBoardPad(
            position=PT,
            layers=PadLayers(top=True, bottom=True, plated_through=True),
        )
        n = _net()
        w = _bare(n)
        p._add_item(w, "top")
        p._remove_item(w.id)
        assert p.electrical_net == (None, None)
        assert p.physically_blocked == ([], [])
