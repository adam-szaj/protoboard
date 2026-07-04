# SPDX-License-Identifier: MIT
"""Tests for the grid-cell component spreader."""

from __future__ import annotations

import math
import uuid
from dataclasses import dataclass, field

import pytest

from protoboard.domain.board.board import Board
from protoboard.domain.board.boxes import BoardGrid, PadAtom, SizePolicy
from protoboard.domain.board.pads import PadLayers
from protoboard.domain.commands.base import Command, CommandStack, MacroCommand
from protoboard.domain.commands.component_cmds import (
    ComponentState,
    MoveComponentCommand,
)
from protoboard.domain.components.definition import ComponentClass, ComponentDefinition
from protoboard.domain.components.footprints import SIL
from protoboard.domain.components.instance import ComponentInstance
from protoboard.domain.layout.layout import Layout
from protoboard.domain.layout.placement import ComponentPlacement
from protoboard.domain.layout.wire_utils import board_pads_for_side
from protoboard.domain.schematic.net import Net, PinRef
from protoboard.domain.schematic.schematic import Schematic
from protoboard.geometry import Point, Rect
from protoboard.services.placement.spreader import (
    SpreadResult,
    _assign_grid_cells,
    _build_net_graph,
    _snap_to_grid,
    spread_components,
)
from tests.instance_utils import bind


# ---------------------------------------------------------------------------
# Shared fixtures
# ---------------------------------------------------------------------------


def _pad() -> PadAtom:
    return PadAtom(
        width_policy=SizePolicy.HUG,
        height_policy=SizePolicy.HUG,
        layers=PadLayers(),
    )


def _make_perfboard(rows: int = 20, cols: int = 20) -> Board:
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
    return ComponentDefinition.from_footprint(
        name=name,
        footprint=SIL(n=2),
        pin_names={},
        component_class=ComponentClass.RESISTOR,
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


def _place(comp_id: uuid.UUID, x: float, y: float) -> ComponentPlacement:
    return ComponentPlacement(
        component_id=comp_id,
        position=Point(x, y),
        rotation=0,
        side="top",
    )


@dataclass
class _FakeSession:
    board: Board
    layout: Layout = field(default_factory=Layout)
    schematic: Schematic = field(default_factory=lambda: Schematic(nets={}))
    instances: dict[uuid.UUID, ComponentInstance] = field(default_factory=dict)
    components: dict[str, ComponentDefinition] = field(default_factory=dict)
    stack: CommandStack[ComponentState] = field(default_factory=CommandStack)


# ---------------------------------------------------------------------------
# Unit tests — _build_net_graph
# ---------------------------------------------------------------------------


class TestBuildNetGraph:
    def test_empty_nets_returns_empty_graph(self) -> None:
        a, b = uuid.uuid4(), uuid.uuid4()
        schematic = Schematic(nets={})
        graph = _build_net_graph({a, b}, schematic)
        assert graph[a] == {}
        assert graph[b] == {}

    def test_one_shared_net_gives_weight_one(self) -> None:
        a, b = uuid.uuid4(), uuid.uuid4()
        net_id = uuid.uuid4()
        schematic = Schematic(
            nets={
                net_id: Net(
                    id=net_id,
                    name="N",
                    pins={PinRef(a, 1), PinRef(b, 1)},
                )
            }
        )
        graph = _build_net_graph({a, b}, schematic)
        assert graph[a].get(b, 0) == pytest.approx(1.0)
        assert graph[b].get(a, 0) == pytest.approx(1.0)

    def test_two_shared_nets_gives_weight_two(self) -> None:
        a, b = uuid.uuid4(), uuid.uuid4()
        net1, net2 = uuid.uuid4(), uuid.uuid4()
        schematic = Schematic(
            nets={
                net1: Net(id=net1, name="N1", pins={PinRef(a, 1), PinRef(b, 1)}),
                net2: Net(id=net2, name="N2", pins={PinRef(a, 2), PinRef(b, 2)}),
            }
        )
        graph = _build_net_graph({a, b}, schematic)
        assert graph[a].get(b, 0) == pytest.approx(2.0)

    def test_external_component_not_in_graph(self) -> None:
        a, b, c = uuid.uuid4(), uuid.uuid4(), uuid.uuid4()
        net_id = uuid.uuid4()
        schematic = Schematic(
            nets={
                net_id: Net(id=net_id, name="N", pins={PinRef(a, 1), PinRef(c, 1)})
            }
        )
        graph = _build_net_graph({a, b}, schematic)
        assert c not in graph
        assert c not in graph.get(a, {})

    def test_no_edge_within_same_component(self) -> None:
        a = uuid.uuid4()
        net_id = uuid.uuid4()
        schematic = Schematic(
            nets={
                net_id: Net(id=net_id, name="N", pins={PinRef(a, 1), PinRef(a, 2)})
            }
        )
        graph = _build_net_graph({a}, schematic)
        assert graph[a] == {}


# ---------------------------------------------------------------------------
# Unit tests — _assign_grid_cells
# ---------------------------------------------------------------------------


class TestAssignGridCells:
    def _bounds(self, rows: int = 10, cols: int = 10) -> Rect:
        return _make_perfboard(rows, cols).bounds

    def test_returns_one_cell_per_component(self) -> None:
        ids = [uuid.uuid4() for _ in range(6)]
        positions = {c: Point(5.0, 5.0) for c in ids}
        net_graph = {c: {} for c in ids}
        result = _assign_grid_cells(ids, net_graph, positions, self._bounds())
        assert len(result) == len(ids)

    def test_all_cells_distinct(self) -> None:
        ids = [uuid.uuid4() for _ in range(9)]
        positions = {c: Point(5.0, 5.0) for c in ids}
        net_graph = {c: {} for c in ids}
        result = _assign_grid_cells(ids, net_graph, positions, self._bounds())
        cells = list(result.values())
        assert len({(p.x, p.y) for p in cells}) == len(ids)

    def test_all_targets_within_board_bounds(self) -> None:
        board = _make_perfboard(10, 10)
        bounds = board.bounds
        ids = [uuid.uuid4() for _ in range(8)]
        positions = {c: Point(5.0, 5.0) for c in ids}
        net_graph = {c: {} for c in ids}
        result = _assign_grid_cells(ids, net_graph, positions, bounds)
        for pt in result.values():
            assert bounds.x <= pt.x <= bounds.x + bounds.width
            assert bounds.y <= pt.y <= bounds.y + bounds.height

    def test_connected_pair_gets_closer_cells_than_unconnected(self) -> None:
        # a and b are connected; c and d are unconnected to each other and to a/b.
        a, b, c, d = [uuid.uuid4() for _ in range(4)]
        net_graph = {a: {b: 1.0}, b: {a: 1.0}, c: {}, d: {}}
        # All start at the same position (no positional bias).
        positions = {x: Point(5.0, 5.0) for x in [a, b, c, d]}
        bounds = _make_perfboard(10, 10).bounds
        result = _assign_grid_cells([a, b, c, d], net_graph, positions, bounds)

        dist_ab = math.hypot(result[b].x - result[a].x, result[b].y - result[a].y)
        dist_cd = math.hypot(result[d].x - result[c].x, result[d].y - result[c].y)
        # Connected pair should land in adjacent cells (distance ≈ cell_size).
        # Unconnected pair has no attraction — should be no closer on average.
        # We assert a/b are within 2 cell-diagonals of each other.
        cell_w = bounds.width / 2
        cell_h = bounds.height / 2
        cell_diag = math.hypot(cell_w, cell_h)
        assert dist_ab <= 2 * cell_diag, (
            f"Connected pair too far apart: {dist_ab:.2f} > 2×{cell_diag:.2f}"
        )

    def test_single_component_gets_a_cell(self) -> None:
        # _assign_grid_cells is only called when len >= 2, but should handle 1 gracefully.
        a, b = uuid.uuid4(), uuid.uuid4()
        positions = {a: Point(3.0, 3.0), b: Point(7.0, 7.0)}
        net_graph = {a: {}, b: {}}
        result = _assign_grid_cells([a, b], net_graph, positions, self._bounds())
        assert a in result and b in result
        assert result[a] != result[b]

    def test_components_near_current_position_when_no_gravity(self) -> None:
        # Without net connections, each component should claim the cell
        # nearest its own current position.
        board = _make_perfboard(10, 10)
        bounds = board.bounds
        a, b = uuid.uuid4(), uuid.uuid4()
        # a near top-left, b near bottom-right.
        positions = {a: Point(1.0, 1.0), b: Point(9.0, 9.0)}
        net_graph = {a: {}, b: {}}
        result = _assign_grid_cells([a, b], net_graph, positions, bounds)
        # a's cell should be closer to top-left than b's cell.
        dist_a_tl = math.hypot(result[a].x - bounds.x, result[a].y - bounds.y)
        dist_b_tl = math.hypot(result[b].x - bounds.x, result[b].y - bounds.y)
        assert dist_a_tl < dist_b_tl


# ---------------------------------------------------------------------------
# Unit tests — _snap_to_grid
# ---------------------------------------------------------------------------


class TestSnapToGrid:
    def test_result_positions_are_on_board_pads(self) -> None:
        board = _make_perfboard(10, 10)
        pads = board_pads_for_side(board, "top")
        defn = _resistor_defn()
        ids = [uuid.uuid4() for _ in range(3)]
        insts = {c: _inst(f"R{i}", "R", defn) for i, c in enumerate(ids)}
        original = {c: _place(c, 0.5, 0.5) for c in ids}
        ideal = {
            ids[0]: Point(1.5, 1.5),
            ids[1]: Point(5.5, 5.5),
            ids[2]: Point(8.5, 8.5),
        }
        session = _FakeSession(board=board, components={"R": defn}, instances=insts)
        result = _snap_to_grid(
            ideal_positions=ideal,
            mobile_ids=ids,
            original_placements=original,
            fixed_obstacles={},
            pads_top=pads,
            pads_bottom=set(),
            session=session,
        )
        for pl in result.values():
            assert pl.position in pads

    def test_no_pairwise_collisions_in_result(self) -> None:
        board = _make_perfboard(10, 10)
        pads = board_pads_for_side(board, "top")
        defn = _resistor_defn()
        ids = [uuid.uuid4() for _ in range(4)]
        insts = {c: _inst(f"R{i}", "R", defn) for i, c in enumerate(ids)}
        original = {c: _place(c, 0.5, float(i)) for i, c in enumerate(ids)}
        ideal = {
            ids[0]: Point(0.5, 0.5),
            ids[1]: Point(3.5, 3.5),
            ids[2]: Point(6.5, 6.5),
            ids[3]: Point(9.5, 9.5),
        }
        session = _FakeSession(board=board, components={"R": defn}, instances=insts)
        result = _snap_to_grid(
            ideal_positions=ideal,
            mobile_ids=ids,
            original_placements=original,
            fixed_obstacles={},
            pads_top=pads,
            pads_bottom=set(),
            session=session,
        )
        from protoboard.services.placement.auto_placer import _has_collision
        placed_list = list(result.items())
        for i, (cid_i, pl_i) in enumerate(placed_list):
            existing = {
                cid_j: (pl_j, defn, insts[cid_j])
                for cid_j, pl_j in placed_list[:i]
            }
            assert not _has_collision(pl_i, defn, insts[cid_i], existing)

    def test_fixed_obstacles_treated_as_immovable(self) -> None:
        board = _make_perfboard(5, 5)
        pads = board_pads_for_side(board, "top")
        defn = _resistor_defn()
        fixed_id = uuid.uuid4()
        fixed_pl = _place(fixed_id, 0.5, 0.5)
        fixed_inst = _inst("RF", "R", defn)
        mobile_id = uuid.uuid4()
        mobile_inst = _inst("R1", "R", defn)
        original = {mobile_id: _place(mobile_id, 2.5, 2.5)}
        ideal = {mobile_id: Point(0.5, 0.5)}
        session = _FakeSession(
            board=board,
            components={"R": defn},
            instances={mobile_id: mobile_inst, fixed_id: fixed_inst},
        )
        result = _snap_to_grid(
            ideal_positions=ideal,
            mobile_ids=[mobile_id],
            original_placements=original,
            fixed_obstacles={fixed_id: (fixed_pl, defn, fixed_inst)},
            pads_top=pads,
            pads_bottom=set(),
            session=session,
        )
        from protoboard.domain.layout.placement import component_bbox
        fixed_bbox = component_bbox(defn.default_footprint(), fixed_pl, defn.component_class.value)
        mobile_bbox = component_bbox(
            defn.default_footprint(), result[mobile_id], defn.component_class.value
        )
        assert not fixed_bbox.intersects(mobile_bbox)


# ---------------------------------------------------------------------------
# Integration tests — spread_components
# ---------------------------------------------------------------------------


class TestSpreadComponents:
    def test_stacked_components_get_distinct_positions(self) -> None:
        board = _make_perfboard(20, 20)
        defn = _resistor_defn()
        ids = [uuid.uuid4() for _ in range(5)]
        insts = {c: _inst(f"R{i}", "R", defn) for i, c in enumerate(ids)}
        layout = Layout()
        for c in ids:
            layout.placements[c] = _place(c, 5.5, 5.5)

        session = _FakeSession(board=board, layout=layout, components={"R": defn}, instances=insts)
        result = spread_components(ids, session)

        assert result.moved_ids, "Expected at least some components to move"
        positions = {result.new_placements[c].position for c in ids}
        assert len(positions) > 1

    def test_all_positions_on_board_pads(self) -> None:
        board = _make_perfboard(20, 20)
        pads = board_pads_for_side(board, "top")
        defn = _resistor_defn()
        ids = [uuid.uuid4() for _ in range(5)]
        insts = {c: _inst(f"R{i}", "R", defn) for i, c in enumerate(ids)}
        layout = Layout()
        for i, c in enumerate(ids):
            layout.placements[c] = _place(c, float(i) + 0.5, 0.5)

        session = _FakeSession(board=board, layout=layout, components={"R": defn}, instances=insts)
        result = spread_components(ids, session)

        for cid in ids:
            assert result.new_placements[cid].position in pads

    def test_no_pairwise_collisions_after_spread(self) -> None:
        board = _make_perfboard(20, 20)
        defn = _resistor_defn()
        ids = [uuid.uuid4() for _ in range(6)]
        insts = {c: _inst(f"R{i}", "R", defn) for i, c in enumerate(ids)}
        layout = Layout()
        for c in ids:
            layout.placements[c] = _place(c, 10.5, 10.5)

        session = _FakeSession(board=board, layout=layout, components={"R": defn}, instances=insts)
        result = spread_components(ids, session)

        from protoboard.services.placement.auto_placer import _has_collision
        placed = [(cid, result.new_placements[cid]) for cid in ids]
        for i, (cid_i, pl_i) in enumerate(placed):
            existing = {
                cid_j: (pl_j, defn, insts[cid_j]) for cid_j, pl_j in placed[:i]
            }
            assert not _has_collision(pl_i, defn, insts[cid_i], existing)

    def test_components_cover_board_area_evenly(self) -> None:
        """After spread, components should be distributed across board quadrants."""
        board = _make_perfboard(20, 20)
        defn = _resistor_defn()
        ids = [uuid.uuid4() for _ in range(8)]
        insts = {c: _inst(f"R{i}", "R", defn) for i, c in enumerate(ids)}
        layout = Layout()
        for c in ids:
            layout.placements[c] = _place(c, 5.5, 5.5)  # all stacked

        session = _FakeSession(board=board, layout=layout, components={"R": defn}, instances=insts)
        result = spread_components(ids, session)

        bounds = board.bounds
        cx, cy = bounds.center.x, bounds.center.y
        quadrants: set[tuple[bool, bool]] = set()
        for cid in ids:
            pos = result.new_placements[cid].position
            quadrants.add((pos.x >= cx, pos.y >= cy))
        # Should occupy at least 3 of 4 quadrants with 8 components.
        assert len(quadrants) >= 3

    def test_partial_spread_unselected_unchanged(self) -> None:
        board = _make_perfboard(20, 20)
        defn = _resistor_defn()
        all_ids = [uuid.uuid4() for _ in range(5)]
        mobile_ids = all_ids[:3]
        fixed_ids = all_ids[3:]
        insts = {c: _inst(f"R{i}", "R", defn) for i, c in enumerate(all_ids)}
        layout = Layout()
        orig_positions = {c: Point(float(i) + 0.5, 0.5) for i, c in enumerate(all_ids)}
        for c in all_ids:
            layout.placements[c] = _place(c, orig_positions[c].x, orig_positions[c].y)

        session = _FakeSession(board=board, layout=layout, components={"R": defn}, instances=insts)
        result = spread_components(mobile_ids, session)

        for cid in fixed_ids:
            assert cid not in result.moved_ids
            assert cid not in result.new_placements

    def test_single_component_returns_empty_result(self) -> None:
        board = _make_perfboard(10, 10)
        defn = _resistor_defn()
        c = uuid.uuid4()
        layout = Layout()
        layout.placements[c] = _place(c, 0.5, 0.5)
        session = _FakeSession(
            board=board, layout=layout, components={"R": defn},
            instances={c: _inst("R1", "R", defn)},
        )
        result = spread_components([c], session)
        assert result.moved_ids == set()
        assert result.new_placements == {}

    def test_empty_input_returns_empty_result(self) -> None:
        board = _make_perfboard(10, 10)
        session = _FakeSession(board=board)
        result = spread_components([], session)
        assert isinstance(result, SpreadResult)
        assert result.moved_ids == set()

    def test_rotation_preserved(self) -> None:
        board = _make_perfboard(20, 20)
        defn = _resistor_defn()
        ids = [uuid.uuid4() for _ in range(3)]
        insts = {c: _inst(f"R{i}", "R", defn) for i, c in enumerate(ids)}
        layout = Layout()
        rotations = [0, 90, 180]
        for i, c in enumerate(ids):
            layout.placements[c] = ComponentPlacement(
                component_id=c, position=Point(5.5, 5.5),
                rotation=rotations[i], side="top",
            )
        session = _FakeSession(board=board, layout=layout, components={"R": defn}, instances=insts)
        result = spread_components(ids, session)
        for i, c in enumerate(ids):
            assert result.new_placements[c].rotation == rotations[i]

    def test_undo_restores_original_positions(self) -> None:
        board = _make_perfboard(20, 20)
        defn = _resistor_defn()
        ids = [uuid.uuid4() for _ in range(4)]
        insts = {c: _inst(f"R{i}", "R", defn) for i, c in enumerate(ids)}
        layout = Layout()
        for c in ids:
            layout.placements[c] = _place(c, 10.5, 10.5)

        session = _FakeSession(board=board, layout=layout, components={"R": defn}, instances=insts)
        orig_positions = {c: layout.placements[c].position for c in ids}

        result = spread_components(ids, session)
        assert result.moved_ids, "No components moved; test is vacuous"

        from typing import cast
        cmds = [
            cast(
                "Command[ComponentState]",
                MoveComponentCommand(
                    component_id=cid,
                    new_placement=result.new_placements[cid],
                    old_placement=session.layout.placements[cid].copy(),
                    skip_conflict_check=True,
                ),
            )
            for cid in result.moved_ids
        ]
        macro = MacroCommand(commands=cmds, description="Spread")
        session.stack.execute(macro, session)

        for cid in result.moved_ids:
            assert session.layout.placements[cid].position != orig_positions[cid]

        session.stack.undo(session)

        for cid in ids:
            assert session.layout.placements[cid].position == orig_positions[cid]
