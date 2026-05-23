# SPDX-License-Identifier: MIT
"""Tests for the pre-route congestion estimator (services.routing.congestion)."""

from __future__ import annotations

import uuid

from protoboard.domain.schematic.net import Net, PinRef
from protoboard.geometry import Point
from protoboard.services.routing.congestion import (
    _l_shape_cells,
    _mst_edges,
    build_congestion_map,
)


def _pin(component_id: uuid.UUID, n: int) -> PinRef:
    return PinRef(component_id=component_id, pin_number=n)


def test_mst_empty_for_lt_two_points() -> None:
    assert _mst_edges([]) == []
    assert _mst_edges([Point(0.5, 0.5)]) == []


def test_mst_two_points_one_edge() -> None:
    a = Point(0.5, 0.5)
    b = Point(5.5, 0.5)
    edges = _mst_edges([a, b])
    assert len(edges) == 1
    assert set(edges[0]) == {a, b}


def test_mst_three_points_picks_two_shortest_edges() -> None:
    # Right triangle: legs 3 and 4, hypotenuse Manhattan = 7.
    # MST should pick legs (cost 3 + 4 = 7), not include hypotenuse.
    a = Point(0.5, 0.5)
    b = Point(3.5, 0.5)  # 3 from a
    c = Point(0.5, 4.5)  # 4 from a, 7 from b
    edges = _mst_edges([a, b, c])
    assert len(edges) == 2
    edge_sets = [set(e) for e in edges]
    assert {a, b} in edge_sets
    assert {a, c} in edge_sets
    assert {b, c} not in edge_sets


def test_l_shape_horizontal_first_excludes_endpoints() -> None:
    a = Point(0.5, 0.5)
    b = Point(3.5, 2.5)
    cells = _l_shape_cells(a, b, horizontal_first=True)
    # Path: (0.5,0.5) → (1.5,0.5) → (2.5,0.5) → (3.5,0.5) [corner] → (3.5,1.5) → (3.5,2.5)
    # Excluding endpoints a and b → 4 transit cells (corner included).
    assert a not in cells
    assert b not in cells
    assert Point(3.5, 0.5) in cells  # corner
    assert Point(1.5, 0.5) in cells
    assert Point(3.5, 1.5) in cells
    assert len(cells) == 4


def test_l_shape_vertical_first_uses_other_corner() -> None:
    a = Point(0.5, 0.5)
    b = Point(3.5, 2.5)
    cells = _l_shape_cells(a, b, horizontal_first=False)
    # Corner is (0.5, 2.5), not (3.5, 0.5).
    assert Point(0.5, 2.5) in cells
    assert Point(3.5, 0.5) not in cells
    assert a not in cells
    assert b not in cells


def test_l_shape_same_point_returns_empty() -> None:
    p = Point(2.5, 2.5)
    assert _l_shape_cells(p, p, horizontal_first=True) == []
    assert _l_shape_cells(p, p, horizontal_first=False) == []


def test_l_shape_collinear_horizontal_only() -> None:
    a = Point(0.5, 1.5)
    b = Point(3.5, 1.5)
    cells_hv = _l_shape_cells(a, b, horizontal_first=True)
    cells_vh = _l_shape_cells(a, b, horizontal_first=False)
    # Both variants degenerate to same straight line.
    assert cells_hv == cells_vh
    assert cells_hv == [Point(1.5, 1.5), Point(2.5, 1.5)]


def test_build_congestion_map_empty_when_no_pins() -> None:
    cid = uuid.uuid4()
    net = Net(id=uuid.uuid4(), name="N1", pins={_pin(cid, 1), _pin(cid, 2)})
    # No placements → no pin_positions → empty map.
    assert build_congestion_map([net], {}) == {}


def test_build_congestion_map_endpoints_not_voted() -> None:
    cid = uuid.uuid4()
    pin_a = _pin(cid, 1)
    pin_b = _pin(cid, 2)
    a = Point(0.5, 0.5)
    b = Point(2.5, 0.5)
    net = Net(id=uuid.uuid4(), name="N1", pins={pin_a, pin_b})
    pin_positions = {pin_a: (a, "top"), pin_b: (b, "top")}
    m = build_congestion_map([net], pin_positions)
    assert a not in m
    assert b not in m


def test_build_congestion_map_straight_line_full_votes() -> None:
    # Collinear pins: both L-shape variants degenerate to the same straight path,
    # so each transit cell gets +0.5 twice → 1.0.
    cid = uuid.uuid4()
    pin_a = _pin(cid, 1)
    pin_b = _pin(cid, 2)
    a = Point(0.5, 0.5)
    b = Point(3.5, 0.5)
    net = Net(id=uuid.uuid4(), name="N1", pins={pin_a, pin_b})
    pin_positions = {pin_a: (a, "top"), pin_b: (b, "top")}
    m = build_congestion_map([net], pin_positions)
    # Transit cells: (1.5, 0.5), (2.5, 0.5) → each gets 0.5 from HV + 0.5 from VH.
    assert m.get(Point(1.5, 0.5)) == 1.0
    assert m.get(Point(2.5, 0.5)) == 1.0


def test_build_congestion_map_corner_cells_get_one() -> None:
    # Non-collinear two pins: HV and VH have different corners.  Each corner
    # belongs to exactly one variant → vote 0.5 each.  Interior cells likewise.
    cid = uuid.uuid4()
    pin_a = _pin(cid, 1)
    pin_b = _pin(cid, 2)
    a = Point(0.5, 0.5)
    b = Point(2.5, 2.5)
    net = Net(id=uuid.uuid4(), name="N1", pins={pin_a, pin_b})
    pin_positions = {pin_a: (a, "top"), pin_b: (b, "top")}
    m = build_congestion_map([net], pin_positions)
    # Corner of HV: (2.5, 0.5).  Corner of VH: (0.5, 2.5).
    assert m.get(Point(2.5, 0.5)) == 0.5
    assert m.get(Point(0.5, 2.5)) == 0.5
    # Mid-edge cells.
    assert m.get(Point(1.5, 0.5)) == 0.5  # HV only
    assert m.get(Point(0.5, 1.5)) == 0.5  # VH only


def test_build_congestion_map_multiple_nets_accumulate() -> None:
    # Two overlapping nets sharing a corridor cell → votes add up.
    cid1 = uuid.uuid4()
    cid2 = uuid.uuid4()
    pin_a1 = _pin(cid1, 1)
    pin_b1 = _pin(cid1, 2)
    pin_a2 = _pin(cid2, 1)
    pin_b2 = _pin(cid2, 2)
    # Both nets are collinear and share cell (2.5, 0.5).
    net1 = Net(id=uuid.uuid4(), name="N1", pins={pin_a1, pin_b1})
    net2 = Net(id=uuid.uuid4(), name="N2", pins={pin_a2, pin_b2})
    pp = {
        pin_a1: (Point(0.5, 0.5), "top"),
        pin_b1: (Point(4.5, 0.5), "top"),
        pin_a2: (Point(1.5, 0.5), "top"),
        pin_b2: (Point(3.5, 0.5), "top"),
    }
    m = build_congestion_map([net1, net2], pp)
    # (2.5, 0.5) is transit for both nets → 1.0 + 1.0 = 2.0
    assert m.get(Point(2.5, 0.5)) == 2.0


def test_build_congestion_map_single_pin_no_votes() -> None:
    cid = uuid.uuid4()
    pin_a = _pin(cid, 1)
    net = Net(id=uuid.uuid4(), name="N1", pins={pin_a})
    pp = {pin_a: (Point(2.5, 2.5), "top")}
    assert build_congestion_map([net], pp) == {}


def test_build_congestion_map_global_net_votes_normally() -> None:
    # Per design decision 2026-05-23: global nets vote like any other.
    cid = uuid.uuid4()
    pin_a = _pin(cid, 1)
    pin_b = _pin(cid, 2)
    net = Net(
        id=uuid.uuid4(),
        name="GND",
        pins={pin_a, pin_b},
        global_net=True,
    )
    pp = {pin_a: (Point(0.5, 0.5), "top"), pin_b: (Point(2.5, 0.5), "top")}
    m = build_congestion_map([net], pp)
    assert m.get(Point(1.5, 0.5)) == 1.0


def test_build_congestion_map_duplicate_pin_positions_deduped() -> None:
    # Two pins of the same net may sit on the same pad (rare but legal during
    # placement); they should collapse to one MST point, not produce a
    # zero-length edge.
    cid = uuid.uuid4()
    pin_a = _pin(cid, 1)
    pin_b = _pin(cid, 2)
    same = Point(2.5, 2.5)
    net = Net(id=uuid.uuid4(), name="N1", pins={pin_a, pin_b})
    pp = {pin_a: (same, "top"), pin_b: (same, "top")}
    assert build_congestion_map([net], pp) == {}
