# SPDX-License-Identifier: MIT
"""Tests for RotateAroundPointCommand — rotate a selection around an
arbitrary user-picked pivot (the thin caller on rotate_components_around)."""

from __future__ import annotations

import uuid
from dataclasses import dataclass, field
from typing import cast

import pytest

from protoboard.domain.commands.base import ComponentState
from protoboard.domain.commands.group_cmds import (
    RotateAroundPointCommand,
)
from protoboard.domain.layout.layout import Layout
from protoboard.domain.layout.placement import ComponentPlacement
from protoboard.geometry import Point, rotation_about


def _rotate_point_around(p: Point, origin: Point, delta: int) -> Point:
    return rotation_about(origin, delta).apply(p)


@dataclass
class _FakeState:
    layout: Layout
    instances: dict[uuid.UUID, object] = field(default_factory=dict)
    components: dict[str, object] = field(default_factory=dict)


def _placement(
    cid: uuid.UUID, x: float, y: float, rotation: int = 0
) -> ComponentPlacement:
    return ComponentPlacement(
        component_id=cid,
        position=Point(x, y),
        rotation=rotation,
        side="top",
    )


def _state(placements: dict[uuid.UUID, ComponentPlacement]) -> _FakeState:
    layout = Layout()
    layout.ensure_root_group()
    for cid, pl in placements.items():
        layout.placements[cid] = pl
    return _FakeState(layout=layout)


class TestRotateAroundPoint:
    def test_pivots_components_around_external_point(self) -> None:
        cid1, cid2 = uuid.uuid4(), uuid.uuid4()
        pl1 = _placement(cid1, 2.0, 1.0, rotation=0)
        pl2 = _placement(cid2, 2.0, 3.0, rotation=90)
        state = _state({cid1: pl1, cid2: pl2})
        pivot = Point(1.0, 1.0)

        RotateAroundPointCommand(
            comp_ids=frozenset({cid1, cid2}),
            origin=pivot,
            delta=90,
        ).apply(cast(ComponentState, state))

        # Same geometry as a group rotation around (1,1):
        # (2,1) -> (1,0) rot 90 ; (2,3) -> (3,0) rot 180.
        new1 = state.layout.placements[cid1]
        new2 = state.layout.placements[cid2]
        assert new1.position.x == pytest.approx(1.0)
        assert new1.position.y == pytest.approx(0.0)
        assert new1.rotation == 90
        assert new2.position.x == pytest.approx(3.0)
        assert new2.position.y == pytest.approx(0.0)
        assert new2.rotation == 180

    def test_matches_rotate_point_around(self) -> None:
        cid = uuid.uuid4()
        pl = _placement(cid, 5.0, 2.0)
        state = _state({cid: pl})
        pivot = Point(3.0, 3.0)

        RotateAroundPointCommand(
            comp_ids=frozenset({cid}),
            origin=pivot,
            delta=270,
        ).apply(cast(ComponentState, state))

        expected = _rotate_point_around(Point(5.0, 2.0), pivot, 270)
        moved = state.layout.placements[cid]
        assert moved.position.x == pytest.approx(expected.x)
        assert moved.position.y == pytest.approx(expected.y)
        assert moved.rotation == 270

    def test_undo_restores_exactly(self) -> None:
        cid = uuid.uuid4()
        pl = _placement(cid, 4.0, 1.0, rotation=90)
        state = _state({cid: pl})
        pivot = Point(0.0, 0.0)

        cmd = RotateAroundPointCommand(
            comp_ids=frozenset({cid}),
            origin=pivot,
            delta=90,
        )
        cmd.apply(cast(ComponentState, state))
        assert state.layout.placements[cid].position != Point(4.0, 1.0)

        cmd.revert(cast(ComponentState, state))
        restored = state.layout.placements[cid]
        assert restored.position.x == pytest.approx(4.0)
        assert restored.position.y == pytest.approx(1.0)
        assert restored.rotation == 90

    def test_empty_selection_is_noop(self) -> None:
        cid = uuid.uuid4()
        pl = _placement(cid, 1.0, 1.0)
        state = _state({cid: pl})

        RotateAroundPointCommand(
            comp_ids=frozenset(),
            origin=Point(0.0, 0.0),
            delta=90,
        ).apply(cast(ComponentState, state))

        unchanged = state.layout.placements[cid]
        assert unchanged.position == Point(1.0, 1.0)
        assert unchanged.rotation == 0
