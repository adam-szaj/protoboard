"""Tests for the pure snap candidate selection operation."""

from __future__ import annotations

from protoboard.domain.layout.snapping import SnapRequest, snap_point
from protoboard.geometry import Point


def test_nearest_candidate_is_selected() -> None:
    request = SnapRequest(
        scene_point=Point(1.8, 1.6),
        pad_candidates=(Point(0.5, 0.5), Point(2.5, 1.5)),
        pin_candidates=(Point(4.5, 4.5),),
    )

    assert snap_point(request) == Point(2.5, 1.5)


def test_pad_wins_equal_distance_tie_with_pin() -> None:
    request = SnapRequest(
        scene_point=Point(1.0, 1.0),
        pad_candidates=(Point(0.5, 1.0),),
        pin_candidates=(Point(1.5, 1.0),),
    )

    assert snap_point(request) == Point(0.5, 1.0)


def test_empty_candidates_fall_back_to_half_grid_for_negative_coordinates() -> None:
    assert snap_point(SnapRequest(scene_point=Point(-1.2, -2.8))) == Point(-1.5, -2.5)


def test_candidate_order_does_not_change_nearest_result() -> None:
    first = SnapRequest(
        scene_point=Point(2.2, 2.2),
        pad_candidates=(Point(1.5, 1.5), Point(2.5, 2.5)),
    )
    second = SnapRequest(
        scene_point=first.scene_point,
        pad_candidates=tuple(reversed(first.pad_candidates)),
    )

    assert snap_point(first) == snap_point(second) == Point(2.5, 2.5)
