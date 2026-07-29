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
    WirePinItem,
    _item_contributes_electrically,
    _item_is_through_hole,
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


def _wire_pin(net: uuid.UUID | None = None) -> WirePinItem:
    return WirePinItem(id=uuid.uuid4(), net=net)


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
        assert (p.electrical_net_on('top'), p.electrical_net_on('bottom')) == (None, None)
        assert p.is_free("top") and p.is_free("bottom")

    def test_bare_top_propagates_to_both(self) -> None:
        p = self._make()
        n = _net()
        p._add_item(_bare(n), "top")
        assert (p.electrical_net_on('top'), p.electrical_net_on('bottom')) == (n, n)

    def test_bare_bottom_propagates_to_both(self) -> None:
        p = self._make()
        n = _net()
        p._add_item(_bare(n), "bottom")
        assert (p.electrical_net_on('top'), p.electrical_net_on('bottom')) == (n, n)

    def test_insulated_endpoint_top_electrical_both(self) -> None:
        # InsulatedWireItem (endpoint) is electrical contact at the solder
        # joint.  Plated-through propagates to both.
        p = self._make()
        n = _net()
        p._add_item(_ins(n), "top")
        assert (p.electrical_net_on('top'), p.electrical_net_on('bottom')) == (n, n)

    def test_insulated_crossing_no_electrical(self) -> None:
        # Mid-segment of an insulated wire is purely air contact.
        p = self._make()
        p._add_item(_ins_xing(_net()), "top")
        assert (p.electrical_net_on('top'), p.electrical_net_on('bottom')) == (None, None)
        # But physically blocks the top side.
        assert len(p.physically_blocked_on("top")) == 1
        assert len(p.physically_blocked_on("bottom")) == 0

    def test_pin_propagates_both_sides(self) -> None:
        p = self._make()
        n = _net()
        p._add_item(_pin(n), "top")
        assert (p.electrical_net_on('top'), p.electrical_net_on('bottom')) == (n, n)
        # Pin physically blocks both sides (through-hole).
        assert len(p.physically_blocked_on("top")) == 1
        assert len(p.physically_blocked_on("bottom")) == 1

    def test_component_body_no_electrical(self) -> None:
        p = self._make()
        p._add_item(_body(), "top")
        assert (p.electrical_net_on('top'), p.electrical_net_on('bottom')) == (None, None)
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
        assert (p.electrical_net_on('top'), p.electrical_net_on('bottom')) == (n, None)

    def test_bare_bottom_only_to_bottom(self) -> None:
        p = self._make()
        n = _net()
        p._add_item(_bare(n), "bottom")
        assert (p.electrical_net_on('top'), p.electrical_net_on('bottom')) == (None, n)

    def test_insulated_endpoint_top_only(self) -> None:
        p = self._make()
        n = _net()
        p._add_item(_ins(n), "top")
        assert (p.electrical_net_on('top'), p.electrical_net_on('bottom')) == (n, None)

    def test_pin_propagates_both(self) -> None:
        # Pin bridges the hole via its metal lead.
        p = self._make()
        n = _net()
        p._add_item(_pin(n), "top")
        assert (p.electrical_net_on('top'), p.electrical_net_on('bottom')) == (n, n)


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
        assert (p.electrical_net_on('top'), p.electrical_net_on('bottom')) == (n, None)

    def test_bare_on_unarmed_side_no_electrical(self) -> None:
        # Top-only board, wire placed on bottom (no copper).  Physical
        # blocker is still recorded but no net contribution.
        p = SingleSidedBoardPad(
            position=PT,
            layers=PadLayers(top=True, bottom=False),
        )
        n = _net()
        p._add_item(_bare(n), "bottom")
        assert (p.electrical_net_on('top'), p.electrical_net_on('bottom')) == (None, None)
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
        assert (p.electrical_net_on('top'), p.electrical_net_on('bottom')) == (n, n)


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
        assert (p.electrical_net_on('top'), p.electrical_net_on('bottom')) == (n, n)
        p._remove_item(w1.id)
        # Still net n because w2 remains.
        assert (p.electrical_net_on('top'), p.electrical_net_on('bottom')) == (n, n)
        p._remove_item(w2.id)
        assert (p.electrical_net_on('top'), p.electrical_net_on('bottom')) == (None, None)

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
        assert (p.electrical_net_on('top'), p.electrical_net_on('bottom')) == (None, None)
        assert p.physically_blocked_on('top') == [] and p.physically_blocked_on('bottom') == []


# ---------------------------------------------------------------------------
# PadIndex — full lifecycle on a small board
# ---------------------------------------------------------------------------


from protoboard.domain.board.pad_state import PadIndex
from protoboard.domain.components.definition import ComponentDefinition
from protoboard.domain.components.footprints import SIL
from protoboard.domain.components.instance import ComponentInstance
from protoboard.domain.components.package import PackageFootprint, body_holes
from protoboard.domain.components.package.loader import build_package
from protoboard.domain.layout.layout import Layout
from protoboard.domain.layout.placement import ComponentPlacement
from protoboard.domain.layout.wire import Wire
from protoboard.domain.schematic.net import Net
from protoboard.domain.schematic.schematic import PinRef, Schematic


def _perfboard(rows: int, cols: int) -> Board:
    return Board(root=BoardGrid(
        width_policy=SizePolicy.HUG, height_policy=SizePolicy.HUG,
        cell=PadAtom(
            width_policy=SizePolicy.HUG, height_policy=SizePolicy.HUG,
            layers=PadLayers(top=True, bottom=True, plated_through=True),
        ),
        rows=rows, cols=cols, row_gap=0, col_gap=0,
    ))


def _bare_wire(*pts: tuple[float, float], side: str = "top") -> Wire:
    return Wire(
        id=uuid.uuid4(),
        path=[Point(x, y) for x, y in pts],
        insulated=False, side=side,  # type: ignore[arg-type]
    )


def _empty_layout() -> Layout:
    lay = Layout()
    lay.ensure_root_group()
    return lay


class TestPadIndexBuild:
    def test_pads_populated_from_board(self) -> None:
        board = _perfboard(3, 3)
        idx = PadIndex(board, _empty_layout(), Schematic(nets={}), {}, {})
        # 3x3 board → 9 pads
        assert len(list(idx.iter_pads())) == 9
        assert idx.pad_at(Point(0.5, 0.5)) is not None
        assert idx.pad_at(Point(2.5, 2.5)) is not None
        assert idx.pad_at(Point(3.5, 3.5)) is None  # off-board

    def test_bare_wire_populates_pads(self) -> None:
        board = _perfboard(3, 3)
        lay = _empty_layout()
        w = _bare_wire((0.5, 0.5), (2.5, 0.5))
        lay.wires.append(w)
        idx = PadIndex(board, lay, Schematic(nets={}), {}, {})
        # All three pads on y=0.5 should have a wire item on top.
        for x in (0.5, 1.5, 2.5):
            pad = idx.pad_at(Point(x, 0.5))
            assert pad is not None
            assert len(pad.physically_blocked_on("top")) == 1

    def test_component_pin_adds_pin_item_and_net(self) -> None:
        board = _perfboard(3, 3)
        defn = ComponentDefinition.from_footprint(name="R", footprint=SIL(2), pin_names={})
        inst = ComponentInstance.new(definition_ref="R", refdes="R1", value="").bind(defn, {})
        pl = ComponentPlacement(
            component_id=inst.id, position=Point(0.5, 0.5),
            rotation=90, side="top",  # SIL(2) rot=90: pin2 at (1.5, 0.5)
        )
        lay = _empty_layout()
        lay.placements[inst.id] = pl
        net_id = uuid.uuid4()
        sch = Schematic(nets={net_id: Net(
            id=net_id, name="N1",
            pins={PinRef(component_id=inst.id, pin_number=1)},
        )})
        idx = PadIndex(board, lay, sch, {"R": defn}, {inst.id: inst})
        pin_pad = idx.pad_at(Point(0.5, 0.5))
        assert pin_pad is not None
        # Pin on plated-through pad propagates to both sides.
        assert (pin_pad.electrical_net_on('top'), pin_pad.electrical_net_on('bottom')) == (net_id, net_id)
        # PinItem present on both sides (through-hole).
        assert len(pin_pad.physically_blocked_on("top")) == 1
        assert len(pin_pad.physically_blocked_on("bottom")) == 1


class TestPadIndexIncremental:
    def test_add_remove_wire_roundtrip(self) -> None:
        board = _perfboard(3, 3)
        idx = PadIndex(board, _empty_layout(), Schematic(nets={}), {}, {})
        w = _bare_wire((0.5, 0.5), (2.5, 0.5))
        idx.add_wire(w)
        # Pads carry the wire.
        for x in (0.5, 1.5, 2.5):
            pad = idx.pad_at(Point(x, 0.5))
            assert pad is not None
            assert len(pad.physically_blocked_on("top")) == 1
        # Remove → state identical to pre-add.
        idx.remove_wire(w)
        for x in (0.5, 1.5, 2.5):
            pad = idx.pad_at(Point(x, 0.5))
            assert pad is not None
            assert len(pad.physically_blocked_on("top")) == 0

    def test_multi_same_net_wire_removal_keeps_net(self) -> None:
        board = _perfboard(3, 3)
        idx = PadIndex(board, _empty_layout(), Schematic(nets={}), {}, {})
        n = _net()
        w1 = _bare_wire((0.5, 0.5), (1.5, 0.5))
        w2 = _bare_wire((0.5, 0.5), (0.5, 1.5))
        idx.add_wire(w1, net=n)
        idx.add_wire(w2, net=n)
        pad = idx.pad_at(Point(0.5, 0.5))
        assert pad is not None
        assert pad.electrical_net_on("top") == n
        idx.remove_wire(w1)
        # w2 still contributes the net.
        assert pad.electrical_net_on("top") == n
        idx.remove_wire(w2)
        assert pad.electrical_net_on("top") is None

    def test_bare_mid_segment_crossing(self) -> None:
        # Diagonal wire (0.5,0.5)→(2.5,2.5) passes through pad (1.5, 1.5).
        board = _perfboard(3, 3)
        idx = PadIndex(board, _empty_layout(), Schematic(nets={}), {}, {})
        n = _net()
        w = _bare_wire((0.5, 0.5), (2.5, 2.5))
        idx.add_wire(w, net=n)
        mid = idx.pad_at(Point(1.5, 1.5))
        assert mid is not None
        # Bare wire crossing → BareWireCrossing on the pad, electrical net set.
        items = mid.physically_blocked_on("top")
        assert any(it.kind == "bare_crossing" for it in items)
        assert mid.electrical_net_on("top") == n


class TestPadIndexCanRouteThrough:
    def test_bare_same_net_pad_allowed(self) -> None:
        board = _perfboard(3, 3)
        idx = PadIndex(board, _empty_layout(), Schematic(nets={}), {}, {})
        n = _net()
        idx.add_wire(_bare_wire((0.5, 0.5), (1.5, 0.5)), net=n)
        assert idx.can_route_through(
            Point(1.5, 0.5), "top", our_net=n, wire_type="bare",
        )

    def test_bare_alien_net_pad_blocked(self) -> None:
        board = _perfboard(3, 3)
        idx = PadIndex(board, _empty_layout(), Schematic(nets={}), {}, {})
        their_net = _net()
        our_net = _net()
        idx.add_wire(_bare_wire((0.5, 0.5), (1.5, 0.5)), net=their_net)
        assert not idx.can_route_through(
            Point(1.5, 0.5), "top", our_net=our_net, wire_type="bare",
        )

    def test_bare_free_pad_allowed(self) -> None:
        board = _perfboard(3, 3)
        idx = PadIndex(board, _empty_layout(), Schematic(nets={}), {}, {})
        assert idx.can_route_through(
            Point(0.5, 0.5), "top", our_net=None, wire_type="bare",
        )

    def test_plated_through_alien_net_other_side_blocked(self) -> None:
        # Real-world user_tangled_04 case: bare wire on bottom of net A
        # makes top of plated-through pad also alien-net A.  Top-side
        # candidate of net B must be blocked.
        board = _perfboard(3, 3)
        idx = PadIndex(board, _empty_layout(), Schematic(nets={}), {}, {})
        net_a = _net()
        net_b = _net()
        idx.add_wire(_bare_wire((0.5, 0.5), (1.5, 0.5), side="bottom"), net=net_a)
        # Top side of (1.5, 0.5) now also carries net A.
        assert not idx.can_route_through(
            Point(1.5, 0.5), "top", our_net=net_b, wire_type="bare",
        )

    def test_component_body_non_blocking_allows_routing_under(self) -> None:
        """Default ComponentBody (blocks_routing=False) does NOT block
        wires routing under the component body.  Through-hole parts sit
        on standoffs / leads so the gap under the body is usable; some
        users deliberately route under them with the component acting as
        a jumper.  Autoroute still soft-penalises via under_component_top.
        """
        board = _perfboard(3, 3)
        # Directly construct a Pad and inject a non-blocking ComponentBody
        # so the test is independent of full PadIndex add_component flow.
        idx = PadIndex(board, _empty_layout(), Schematic(nets={}), {}, {})
        target = idx.pad_at(Point(1.5, 1.5))
        assert target is not None
        target._add_item(ComponentBody(id=uuid.uuid4(), blocks_routing=False), "top")
        # Both insulated and bare wires of any net may pass through.
        assert idx.can_route_through(
            Point(1.5, 1.5), "top", our_net=None, wire_type="insulated",
        )
        assert idx.can_route_through(
            Point(1.5, 1.5), "top", our_net=None, wire_type="bare",
        )

    def test_component_body_blocking_blocks_routing(self) -> None:
        """ComponentBody with blocks_routing=True is a hard block on its
        own side — for SMD and tight-fit packages that physically touch
        the PCB and leave no gap for wires.
        """
        board = _perfboard(3, 3)
        idx = PadIndex(board, _empty_layout(), Schematic(nets={}), {}, {})
        target = idx.pad_at(Point(1.5, 1.5))
        assert target is not None
        target._add_item(ComponentBody(id=uuid.uuid4(), blocks_routing=True), "top")
        # Same side — blocked for both wire types.
        assert not idx.can_route_through(
            Point(1.5, 1.5), "top", our_net=None, wire_type="insulated",
        )
        assert not idx.can_route_through(
            Point(1.5, 1.5), "top", our_net=None, wire_type="bare",
        )
        # Opposite side — body on top doesn't constrain bottom-side wires.
        assert idx.can_route_through(
            Point(1.5, 1.5), "bottom", our_net=None, wire_type="insulated",
        )


def _prism_package():  # noqa: ANN202  (Package protocol)
    """A 4-pin prism (2×1 box) whose body covers interior holes.

    Pins at local (0,0),(0,1),(2,1),(2,0); body_holes include the interior
    column x=1 — exactly the pads a body should mark with ComponentBody.
    """
    return build_package(
        {
            "shape": "prism",
            "dims": {"ly": 5.0, "lx": 5.0},
            "bend": 0,
            "leads": {
                "direction": [0, 0, -1],
                "body_offset": {"unit": 2.5, "value": [[0, 1], [2, 0], [0, -1]]},
                "board_offset": "default",
            },
        },
        "TEST_DIP4",
    )


def _packaged_defn(*, blocks_routing: bool = False) -> ComponentDefinition:
    pkg = _prism_package()
    return ComponentDefinition(
        name="U",
        # type: ignore[arg-type]
        pin_names={},
        package=pkg,  # type: ignore[arg-type]
        body_blocks_routing=blocks_routing,
    )


def _placed_packaged_index(
    *, blocks_routing: bool = False,
) -> "PadIndex":
    board = _perfboard(4, 4)
    defn = _packaged_defn(blocks_routing=blocks_routing)
    inst = ComponentInstance.new(definition_ref="U", refdes="U1", value="").bind(defn, {})
    pl = ComponentPlacement(
        component_id=inst.id, position=Point(0.5, 0.5),
        rotation=0, side="top",
    )
    lay = _empty_layout()
    lay.placements[inst.id] = pl
    return PadIndex(
        board, lay, Schematic(nets={}), {"U": defn}, {inst.id: inst},
    )


class TestPadIndexPackageBody:
    """add_component derives the body footprint from the package mask."""

    def test_interior_hole_gets_component_body(self) -> None:
        idx = _placed_packaged_index()
        # Local body hole (1, 0) → world (1.5, 0.5) (pin1 anchored at 0.5).
        pad = idx.pad_at(Point(1.5, 0.5))
        assert pad is not None
        items = pad.physically_blocked_on("top")
        assert any(isinstance(it, ComponentBody) for it in items)

    def test_pin_hole_keeps_pin_item_not_body(self) -> None:
        idx = _placed_packaged_index()
        # Pin 1 local (0,0) → world (0.5, 0.5).
        pad = idx.pad_at(Point(0.5, 0.5))
        assert pad is not None
        items = pad.physically_blocked_on("top")
        assert any(isinstance(it, PinItem) for it in items)
        assert not any(isinstance(it, ComponentBody) for it in items)

    def test_non_blocking_body_allows_routing_under(self) -> None:
        idx = _placed_packaged_index(blocks_routing=False)
        assert idx.can_route_through(
            Point(1.5, 0.5), "top", our_net=None, wire_type="insulated",
        )

    def test_blocking_body_blocks_routing_under(self) -> None:
        idx = _placed_packaged_index(blocks_routing=True)
        assert not idx.can_route_through(
            Point(1.5, 0.5), "top", our_net=None, wire_type="insulated",
        )
        # Opposite side stays free — body sits on top only.
        assert idx.can_route_through(
            Point(1.5, 0.5), "bottom", our_net=None, wire_type="insulated",
        )

    def test_body_footprint_matches_mask_not_flooded_bbox(self) -> None:
        # Only the package's body_holes (minus pins) carry a ComponentBody;
        # holes outside the mask stay free even if a rectangular bbox would
        # have covered them.
        idx = _placed_packaged_index()
        pkg = _prism_package()
        body_world = {
            Point(0.5 + h.x, 0.5 + h.y) for h in body_holes(pkg)
        }
        for pad in idx.iter_pads():
            has_body = any(
                isinstance(it, ComponentBody)
                for it in pad.physically_blocked_on("top")
            )
            if has_body:
                assert pad.position in body_world


class TestPadIndexClone:
    def test_clone_is_independent(self) -> None:
        board = _perfboard(3, 3)
        idx = PadIndex(board, _empty_layout(), Schematic(nets={}), {}, {})
        n = _net()
        idx.add_wire(_bare_wire((0.5, 0.5), (1.5, 0.5)), net=n)
        twin = idx.clone()
        # Remove from clone, original unchanged.
        for wid, _ in list(twin._wire_records.items()):
            wire_obj = Wire(
                id=wid, path=[Point(0.5, 0.5), Point(1.5, 0.5)],
                insulated=False, side="top",
            )
            twin.remove_wire(wire_obj)
        # Original still has the wire on pads.
        pad_orig = idx.pad_at(Point(1.5, 0.5))
        pad_twin = twin.pad_at(Point(1.5, 0.5))
        assert pad_orig is not None and pad_twin is not None
        assert pad_orig.electrical_net_on("top") == n
        assert pad_twin.electrical_net_on("top") is None


# ---------------------------------------------------------------------------
# WirePinItem — virtual goldpin (rule 5 + 6 equivalence with PinItem)
# ---------------------------------------------------------------------------


class TestWirePinItem:
    """A wire-pin is electrically and physically equivalent to a real
    PinItem: through-hole propagation to all physical layers, blocks
    foreign wire interiors.  See docs/archive/wire-pin-implementation.md §2.2.
    """

    def _make_plated_through(self) -> ConnectedDoubleSidedBoardPad:
        return ConnectedDoubleSidedBoardPad(
            position=PT,
            layers=PadLayers(top=True, bottom=True, plated_through=True),
        )

    def test_through_hole_predicate(self) -> None:
        # Rule 5: wire-pin pierces the hole, propagates to every side.
        assert _item_is_through_hole(_wire_pin()) is True

    def test_contributes_electrically(self) -> None:
        assert _item_contributes_electrically(_wire_pin()) is True

    def test_pin_distinguishable(self) -> None:
        # Distinct .kind makes it possible to filter renderers/labels —
        # the wire-pin has no refdes, no PinRef.
        wp = _wire_pin()
        p = _pin()
        assert wp.kind == "wire_pin"
        assert p.kind == "pin"

    def test_propagates_both_sides_on_plated_through(self) -> None:
        p = self._make_plated_through()
        n = _net()
        p._add_item(_wire_pin(n), "top")
        # Rule 5: through-hole, same as a real pin.
        assert (
            p.electrical_net_on("top"),
            p.electrical_net_on("bottom"),
        ) == (n, n)

    def test_physically_blocks_both_sides(self) -> None:
        p = self._make_plated_through()
        p._add_item(_wire_pin(_net()), "top")
        # Through-hole physical presence — pin pierces both sides.
        assert len(p.physically_blocked_on("top")) == 1
        assert len(p.physically_blocked_on("bottom")) == 1

    def test_equivalent_to_pin_on_isolated_pad(self) -> None:
        # Isolated double-sided pad (no plated_through): pin / wire-pin
        # still propagate to both sides because rule 5 says through-hole
        # items pierce the stack regardless of board type (the lead
        # exists in both layers' copper rings simultaneously).
        pad = IsolatedDoubleSidedBoardPad(
            position=PT,
            layers=PadLayers(top=True, bottom=True, plated_through=False),
        )
        n = _net()
        pad._add_item(_wire_pin(n), "top")
        assert pad.electrical_net_on("top") == n
        assert pad.electrical_net_on("bottom") == n

    def test_net_inherited_on_short_when_mixed_with_foreign_pin(self) -> None:
        # Wire-pin + foreign component pin on same pad → SHORT_CIRCUIT
        # behavior matches PinItem + PinItem.  Rule 6 catches this at the
        # DRC layer (no wire over wire-pin pad), but raw multi-source
        # state must register the conflict.
        pad = self._make_plated_through()
        n_a = _net()
        n_b = _net()
        pad._add_item(_wire_pin(n_a), "top")
        pad._add_item(_pin(n_b), "top")
        assert pad.electrical_net_on("top") is SHORT_CIRCUIT
