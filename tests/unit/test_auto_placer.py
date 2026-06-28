# SPDX-License-Identifier: MIT
"""Tests for the greedy auto-placer with net-gravity heuristic."""

from __future__ import annotations

import uuid
from dataclasses import dataclass, field
from typing import Any

import pytest

from protoboard.domain.board.board import Board
from protoboard.domain.board.boxes import BoardGrid, PadAtom, SizePolicy
from protoboard.domain.board.pads import PadLayers
from protoboard.domain.components.definition import ComponentClass, ComponentDefinition
from protoboard.domain.components.footprints import SIL
from protoboard.domain.components.instance import ComponentInstance
from protoboard.domain.layout.layout import Layout
from protoboard.domain.layout.placement import ComponentPlacement
from protoboard.domain.schematic.net import Net, PinRef
from protoboard.domain.schematic.schematic import Schematic
from protoboard.geometry import Point
from protoboard.services.placement.auto_placer import (
    AutoPlaceResult,
    auto_place,
    _centroid,
    _sort_candidates,
    _has_collision,
    _extended_candidates,
)
from tests.instance_utils import bind


# ---------------------------------------------------------------------------
# Fixtures
# ---------------------------------------------------------------------------


def _pad() -> PadAtom:
    return PadAtom(
        width_policy=SizePolicy.HUG,
        height_policy=SizePolicy.HUG,
        layers=PadLayers(),
    )


def _make_perfboard(rows: int = 10, cols: int = 10) -> Board:
    grid = BoardGrid(
        width_policy=SizePolicy.HUG,
        height_policy=SizePolicy.HUG,
        cell=_pad(),
        rows=rows,
        cols=cols,
        row_gap=0.0,
        col_gap=0.0,
    )
    return Board(root=grid)


def _resistor_defn(name: str = "R") -> ComponentDefinition:
    return ComponentDefinition(
        name=name,
        footprint=SIL(n=2),
        pin_names={},
        component_class=ComponentClass.RESISTOR,
    )


def _connector_defn(n: int = 2, name: str = "J") -> ComponentDefinition:
    return ComponentDefinition(
        name=name,
        footprint=SIL(n=n),
        pin_names={},
        component_class=ComponentClass.CONNECTOR,
    )


def _inst(
    refdes: str,
    definition_ref: str,
    defn: ComponentDefinition | None = None,
) -> ComponentInstance:
    inst = ComponentInstance(
        id=uuid.uuid4(),
        definition_ref=definition_ref,
        refdes=refdes,
        value="",
    )
    return inst if defn is None else bind(inst, defn, {})


@dataclass
class _FakeSession:
    """Minimal AutoPlaceState implementation for tests."""

    board: Board
    layout: Layout = field(default_factory=Layout)
    schematic: Schematic = field(default_factory=lambda: Schematic(nets={}))
    instances: dict[uuid.UUID, ComponentInstance] = field(default_factory=dict)
    components: dict[str, ComponentDefinition] = field(default_factory=dict)


# ---------------------------------------------------------------------------
# Unit tests — helpers
# ---------------------------------------------------------------------------


class TestCentroid:
    def test_none_for_empty(self) -> None:
        assert _centroid([]) is None

    def test_single_point(self) -> None:
        c = _centroid([Point(3.0, 7.0)])
        assert c is not None
        assert c.x == pytest.approx(3.0)
        assert c.y == pytest.approx(7.0)

    def test_mean_of_two(self) -> None:
        c = _centroid([Point(0.0, 0.0), Point(4.0, 2.0)])
        assert c is not None
        assert c.x == pytest.approx(2.0)
        assert c.y == pytest.approx(1.0)


class TestSortCandidates:
    def test_no_gravity_preserves_row_major(self) -> None:
        pts = [Point(2.0, 0.0), Point(0.0, 0.0), Point(1.0, 0.0)]
        result = _sort_candidates(pts, center=None)
        # No gravity → input order preserved (row-major already sorted by caller)
        assert result == pts

    def test_sorts_by_distance_from_center(self) -> None:
        center = Point(5.0, 5.0)
        pts = [Point(0.0, 0.0), Point(5.0, 6.0), Point(10.0, 10.0)]
        result = _sort_candidates(pts, center)
        # (5,6) is closest (dist=1), then (0,0) (dist≈7.07), then (10,10) (dist≈7.07)
        assert result[0] == Point(5.0, 6.0)


class TestHasCollision:
    def _make_placement(
        self, comp_id: uuid.UUID, x: float, y: float
    ) -> ComponentPlacement:
        return ComponentPlacement(
            component_id=comp_id,
            position=Point(x, y),
            rotation=0,
            side="top",
        )

    def test_no_collision_when_empty_existing(self) -> None:
        defn = _resistor_defn()
        inst = _inst("R1", "R", defn)
        pl = self._make_placement(uuid.uuid4(), 0.5, 0.5)
        assert not _has_collision(pl, defn, inst, {})

    def test_collision_when_bbox_overlaps(self) -> None:
        defn = _resistor_defn()
        inst1 = _inst("R1", "R", defn)
        inst2 = _inst("R2", "R", defn)
        comp1 = uuid.uuid4()
        comp2 = uuid.uuid4()
        pl1 = self._make_placement(comp1, 0.5, 0.5)
        pl2 = self._make_placement(comp2, 0.5, 0.5)  # same position → bbox overlap
        existing = {comp1: (pl1, defn, inst1)}
        assert _has_collision(pl2, defn, inst2, existing)

    def test_no_collision_when_far_apart(self) -> None:
        defn = _resistor_defn()
        inst1 = _inst("R1", "R", defn)
        inst2 = _inst("R2", "R", defn)
        comp1 = uuid.uuid4()
        comp2 = uuid.uuid4()
        pl1 = self._make_placement(comp1, 0.5, 0.5)
        pl2 = self._make_placement(comp2, 8.5, 8.5)
        existing = {comp1: (pl1, defn, inst1)}
        assert not _has_collision(pl2, defn, inst2, existing)


class TestExtendedCandidates:
    def test_returns_points_outside_board(self) -> None:
        board = _make_perfboard(5, 5)
        from protoboard.domain.layout.wire_utils import board_pads_for_side
        pads = board_pads_for_side(board, "top")
        extra = _extended_candidates(pads, board.bounds)
        # All returned points must NOT be in board pads
        for pt in extra:
            assert pt not in pads

    def test_nonempty_for_small_board(self) -> None:
        board = _make_perfboard(3, 3)
        from protoboard.domain.layout.wire_utils import board_pads_for_side
        pads = board_pads_for_side(board, "top")
        extra = _extended_candidates(pads, board.bounds)
        assert len(extra) > 0


# ---------------------------------------------------------------------------
# Integration tests — auto_place
# ---------------------------------------------------------------------------


class TestAutoPlaceBasic:
    def test_returns_auto_place_result(self) -> None:
        board = _make_perfboard(5, 5)
        defn = _resistor_defn()
        inst = _inst("R1", "R", defn)
        session = _FakeSession(board=board, components={"R": defn})
        result = auto_place([inst], session)
        assert isinstance(result, AutoPlaceResult)

    def test_places_single_component(self) -> None:
        board = _make_perfboard(5, 5)
        defn = _resistor_defn()
        inst = _inst("R1", "R", defn)
        session = _FakeSession(board=board, components={"R": defn})
        result = auto_place([inst], session)
        assert len(result.placements) == 1
        assert result.placements[0].component_id == inst.id

    def test_skips_component_with_no_defn(self) -> None:
        board = _make_perfboard(5, 5)
        inst = _inst("X1", "unknown_part")
        session = _FakeSession(board=board)
        result = auto_place([inst], session)
        assert len(result.placements) == 0
        assert inst.id in result.skipped

    def test_places_multiple_without_collision(self) -> None:
        board = _make_perfboard(10, 10)
        defn = _resistor_defn()
        insts = [_inst(f"R{i}", "R", defn) for i in range(1, 6)]
        session = _FakeSession(board=board, components={"R": defn})
        result = auto_place(insts, session)
        assert len(result.placements) == 5
        # No two placements at the same position
        positions = [pl.position for pl in result.placements]
        assert len(set((p.x, p.y) for p in positions)) == 5

    def test_respects_already_placed_components(self) -> None:
        board = _make_perfboard(5, 5)
        defn = _resistor_defn()
        existing_id = uuid.uuid4()
        existing_inst = bind(
            ComponentInstance(
                id=existing_id, definition_ref="R", refdes="R0", value=""
            ),
            defn,
            {},
        )
        layout = Layout()
        layout.placements[existing_id] = ComponentPlacement(
            component_id=existing_id,
            position=Point(0.5, 0.5),
            rotation=0,
            side="top",
        )
        new_inst = _inst("R1", "R", defn)
        session = _FakeSession(
            board=board,
            layout=layout,
            components={"R": defn},
            instances={existing_id: existing_inst},
        )
        result = auto_place([new_inst], session)
        assert len(result.placements) == 1
        # Must not collide with already-placed
        placed = result.placements[0]
        assert placed.position != Point(0.5, 0.5)

    def test_placement_position_is_on_board_pad(self) -> None:
        board = _make_perfboard(5, 5)
        from protoboard.domain.layout.wire_utils import board_pads_for_side
        board_pads = board_pads_for_side(board, "top")
        defn = _resistor_defn()
        inst = _inst("R1", "R", defn)
        session = _FakeSession(board=board, components={"R": defn})
        result = auto_place([inst], session)
        assert result.placements[0].position in board_pads


class TestAutoPlaceGravity:
    def test_gravity_pulls_toward_connected_component(self) -> None:
        """A newly placed component should be closer to its net partner than to
        the opposite corner of the board."""
        board = _make_perfboard(20, 20)
        defn = _resistor_defn()

        # R1 already placed at top-left corner area
        r1_id = uuid.uuid4()
        r1_inst = bind(
            ComponentInstance(
                id=r1_id, definition_ref="R", refdes="R1", value=""
            ),
            defn,
            {},
        )
        layout = Layout()
        layout.placements[r1_id] = ComponentPlacement(
            component_id=r1_id,
            position=Point(0.5, 0.5),
            rotation=0,
            side="top",
        )

        # R2 shares a net with R1 (pin 1 ↔ pin 1)
        r2_id = uuid.uuid4()
        r2_inst = bind(
            ComponentInstance(
                id=r2_id, definition_ref="R", refdes="R2", value=""
            ),
            defn,
            {},
        )

        net_id = uuid.uuid4()
        schematic = Schematic(
            nets={
                net_id: Net(
                    id=net_id,
                    name="VCC",
                    pins={
                        PinRef(component_id=r1_id, pin_number=1),
                        PinRef(component_id=r2_id, pin_number=1),
                    },
                )
            }
        )

        session = _FakeSession(
            board=board,
            layout=layout,
            schematic=schematic,
            components={"R": defn},
            instances={r1_id: r1_inst},
        )

        result = auto_place([r2_inst], session)
        assert len(result.placements) == 1

        placed_pos = result.placements[0].position
        # Gravity center = (0.5, 0.5) (pin 1 of R1).
        # R2 should be placed near top-left, not near bottom-right (19.5, 19.5).
        dist_to_r1 = (placed_pos.x - 0.5) ** 2 + (placed_pos.y - 0.5) ** 2
        dist_to_br = (placed_pos.x - 19.5) ** 2 + (placed_pos.y - 19.5) ** 2
        assert dist_to_r1 < dist_to_br, (
            f"Placed at {placed_pos} which is closer to bottom-right than to R1"
        )


class TestAutoPlaceOutsideBoard:
    def test_places_outside_when_board_full(self) -> None:
        """When the board is tiny (1×1) a second resistor has no room and must
        be placed outside board bounds."""
        board = _make_perfboard(rows=1, cols=1)
        defn = _resistor_defn()

        # First component already occupies the only pad on the 1×1 board.
        r1_id = uuid.uuid4()
        r1_inst = bind(
            ComponentInstance(
                id=r1_id, definition_ref="R", refdes="R1", value=""
            ),
            defn,
            {},
        )
        layout = Layout()
        layout.placements[r1_id] = ComponentPlacement(
            component_id=r1_id,
            position=Point(0.5, 0.5),
            rotation=0,
            side="top",
        )

        r2_inst = _inst("R2", "R", defn)
        session = _FakeSession(
            board=board,
            layout=layout,
            components={"R": defn},
            instances={r1_id: r1_inst},
        )

        result = auto_place([r2_inst], session)
        # Must place somewhere (outside board), not skip
        assert len(result.placements) == 1
        assert r2_inst.id in result.outside_board
        assert len(result.skipped) == 0
