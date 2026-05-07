# SPDX-License-Identifier: MIT
"""Tests for the force-directed component spreader."""

from __future__ import annotations

import math
import uuid
from dataclasses import dataclass, field

import pytest

from protoboard.domain.board.board import Board
from protoboard.domain.board.boxes import BoardGrid, PadAtom, SizePolicy
from protoboard.domain.board.pads import PadLayers
from protoboard.domain.commands.base import CommandStack, MacroCommand
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
from protoboard.geometry import Point
from protoboard.services.placement.spreader import (
    SpreadResult,
    _build_net_graph,
    _run_force_simulation,
    _snap_to_grid,
    spread_components,
)


# ---------------------------------------------------------------------------
# Shared fixtures (mirror test_auto_placer.py)
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
    return ComponentDefinition(
        name=name,
        footprint=SIL(n=2),
        pin_names={},
        component_class=ComponentClass.RESISTOR,
    )


def _inst(refdes: str, definition_ref: str) -> ComponentInstance:
    return ComponentInstance(
        id=uuid.uuid4(),
        definition_ref=definition_ref,
        refdes=refdes,
        value="",
    )


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
    stack: CommandStack[ComponentState] = field(
        default_factory=CommandStack
    )


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
        # c is on the net but NOT in the mobile set
        schematic = Schematic(
            nets={
                net_id: Net(id=net_id, name="N", pins={PinRef(a, 1), PinRef(c, 1)})
            }
        )
        graph = _build_net_graph({a, b}, schematic)
        # c should not appear as a key or as a neighbor
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
# Unit tests — _run_force_simulation
# ---------------------------------------------------------------------------


class TestRunForceSimulation:
    def _positions(self, pts: list[tuple[float, float]]) -> dict[uuid.UUID, Point]:
        ids = [uuid.uuid4() for _ in pts]
        return {ids[i]: Point(pts[i][0], pts[i][1]) for i in range(len(pts))}

    def test_repulsion_increases_mean_pairwise_distance(self) -> None:
        # All components stacked at the same point.
        ids = [uuid.uuid4() for _ in range(5)]
        positions = {c: Point(10.0, 10.0) for c in ids}
        net_graph = {c: {} for c in ids}
        anchor_pulls: dict[uuid.UUID, list[Point]] = {c: [] for c in ids}
        bbox_radii = {c: 0.5 for c in ids}
        board_bounds = _make_perfboard(20, 20).bounds

        before_dists = _mean_pairwise_dist(list(positions.values()))

        result = _run_force_simulation(
            positions=positions,
            net_graph=net_graph,
            anchor_pulls=anchor_pulls,
            bbox_radii=bbox_radii,
            board_bounds=board_bounds,
            max_iterations=100,
            temperature=1.0,
            max_net_distance=16.0,
        )

        after_dists = _mean_pairwise_dist(list(result.values()))
        assert after_dists > before_dists

    def test_spring_pulls_connected_pair_closer(self) -> None:
        a, b, c = uuid.uuid4(), uuid.uuid4(), uuid.uuid4()
        # a and b connected; c unconnected; all start at different positions.
        # Keep them on a small board so max_net_distance is not exceeded.
        positions = {a: Point(1.0, 1.0), b: Point(8.0, 8.0), c: Point(1.0, 8.0)}
        net_graph = {a: {b: 1.0}, b: {a: 1.0}, c: {}}
        anchor_pulls = {a: [], b: [], c: []}
        bbox_radii = {a: 0.5, b: 0.5, c: 0.5}
        board_bounds = _make_perfboard(10, 10).bounds

        dist_ab_before = math.hypot(8.0 - 1.0, 8.0 - 1.0)  # ~9.9 RU
        dist_ac_before = math.hypot(1.0 - 1.0, 8.0 - 1.0)  # 7.0 RU

        result = _run_force_simulation(
            positions=positions,
            net_graph=net_graph,
            anchor_pulls=anchor_pulls,
            bbox_radii=bbox_radii,
            board_bounds=board_bounds,
            max_iterations=200,
            temperature=1.0,
            max_net_distance=30.0,  # well above starting distances
        )

        dist_ab_after = math.hypot(result[b].x - result[a].x, result[b].y - result[a].y)
        dist_ac_after = math.hypot(result[c].x - result[a].x, result[c].y - result[a].y)

        # Connected pair a–b should be closer after simulation.
        assert dist_ab_after < dist_ab_before
        # Unconnected pair a–c should be further (repulsion dominates when no spring).
        assert dist_ac_after > dist_ac_before

    def test_positions_stay_within_board_bounds(self) -> None:
        ids = [uuid.uuid4() for _ in range(4)]
        board = _make_perfboard(10, 10)
        bounds = board.bounds
        # Start at center; strong repulsion may push toward edges but not outside.
        positions = {c: Point(bounds.center.x, bounds.center.y) for c in ids}
        net_graph = {c: {} for c in ids}
        anchor_pulls = {c: [] for c in ids}
        bbox_radii = {c: 0.5 for c in ids}

        result = _run_force_simulation(
            positions=positions,
            net_graph=net_graph,
            anchor_pulls=anchor_pulls,
            bbox_radii=bbox_radii,
            board_bounds=bounds,
            max_iterations=200,
            temperature=2.0,
            max_net_distance=16.0,
        )

        for pt in result.values():
            assert bounds.x <= pt.x <= bounds.x + bounds.width
            assert bounds.y <= pt.y <= bounds.y + bounds.height

    def test_zero_iterations_returns_original(self) -> None:
        ids = [uuid.uuid4(), uuid.uuid4()]
        positions = {ids[0]: Point(1.0, 2.0), ids[1]: Point(5.0, 6.0)}
        board_bounds = _make_perfboard(10, 10).bounds
        result = _run_force_simulation(
            positions=dict(positions),
            net_graph={c: {} for c in ids},
            anchor_pulls={c: [] for c in ids},
            bbox_radii={c: 0.5 for c in ids},
            board_bounds=board_bounds,
            max_iterations=0,
            temperature=1.0,
            max_net_distance=16.0,
        )
        for cid, pt in positions.items():
            assert result[cid].x == pytest.approx(pt.x)
            assert result[cid].y == pytest.approx(pt.y)


# ---------------------------------------------------------------------------
# Unit tests — _snap_to_grid
# ---------------------------------------------------------------------------


class TestSnapToGrid:
    def _make_session(self, board: Board) -> _FakeSession:
        return _FakeSession(board=board, components={"R": _resistor_defn()})

    def test_result_positions_are_on_board_pads(self) -> None:
        board = _make_perfboard(10, 10)
        pads = board_pads_for_side(board, "top")
        ids = [uuid.uuid4() for _ in range(3)]
        defn = _resistor_defn()
        insts = {c: _inst(f"R{i}", "R") for i, c in enumerate(ids)}
        original = {c: _place(c, 0.5, 0.5) for c in ids}  # all stacked
        # Spread targets: different positions
        ideal = {
            ids[0]: Point(1.5, 1.5),
            ids[1]: Point(5.5, 5.5),
            ids[2]: Point(8.5, 8.5),
        }
        session = _FakeSession(
            board=board,
            components={"R": defn},
            instances=insts,
        )
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
        insts = {c: _inst(f"R{i}", "R") for i, c in enumerate(ids)}
        original = {c: _place(c, 0.5, float(i)) for i, c in enumerate(ids)}
        ideal = {
            ids[0]: Point(0.5, 0.5),
            ids[1]: Point(3.5, 3.5),
            ids[2]: Point(6.5, 6.5),
            ids[3]: Point(9.5, 9.5),
        }
        session = _FakeSession(
            board=board,
            components={"R": defn},
            instances=insts,
        )
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
            existing = {cid_j: (pl_j, defn) for cid_j, pl_j in placed_list[:i]}
            assert not _has_collision(pl_i, defn, existing), (
                f"Collision for component {cid_i} at {pl_i.position}"
            )

    def test_fixed_obstacles_treated_as_immovable(self) -> None:
        board = _make_perfboard(5, 5)
        pads = board_pads_for_side(board, "top")
        defn = _resistor_defn()

        fixed_id = uuid.uuid4()
        fixed_pl = _place(fixed_id, 0.5, 0.5)
        fixed_inst = _inst("RF", "R")

        mobile_id = uuid.uuid4()
        mobile_inst = _inst("R1", "R")
        original = {mobile_id: _place(mobile_id, 2.5, 2.5)}
        ideal = {mobile_id: Point(0.5, 0.5)}  # exactly where fixed component is

        session = _FakeSession(
            board=board,
            components={"R": defn},
            instances={mobile_id: mobile_inst, fixed_id: fixed_inst},
        )

        result = _snap_to_grid(
            ideal_positions=ideal,
            mobile_ids=[mobile_id],
            original_placements=original,
            fixed_obstacles={fixed_id: (fixed_pl, defn)},
            pads_top=pads,
            pads_bottom=set(),
            session=session,
        )
        # Must not land exactly on the fixed component's bbox
        from protoboard.domain.layout.placement import component_bbox
        fixed_bbox = component_bbox(defn.footprint, fixed_pl, defn.component_class.value)
        mobile_bbox = component_bbox(defn.footprint, result[mobile_id], defn.component_class.value)
        assert not fixed_bbox.intersects(mobile_bbox)


# ---------------------------------------------------------------------------
# Integration tests — spread_components
# ---------------------------------------------------------------------------


class TestSpreadComponents:
    def test_stacked_components_get_distinct_positions(self) -> None:
        board = _make_perfboard(20, 20)
        defn = _resistor_defn()
        ids = [uuid.uuid4() for _ in range(5)]
        insts = {c: _inst(f"R{i}", "R") for i, c in enumerate(ids)}
        layout = Layout()
        for c in ids:
            layout.placements[c] = _place(c, 5.5, 5.5)  # all at same position

        session = _FakeSession(
            board=board,
            layout=layout,
            components={"R": defn},
            instances=insts,
        )

        result = spread_components(ids, session)

        assert result.moved_ids, "Expected at least some components to move"
        positions = {
            result.new_placements[c].position for c in ids
        }
        assert len(positions) > 1

    def test_all_positions_on_board_pads(self) -> None:
        board = _make_perfboard(20, 20)
        pads = board_pads_for_side(board, "top")
        defn = _resistor_defn()
        ids = [uuid.uuid4() for _ in range(5)]
        insts = {c: _inst(f"R{i}", "R") for i, c in enumerate(ids)}
        layout = Layout()
        for i, c in enumerate(ids):
            layout.placements[c] = _place(c, float(i) + 0.5, 0.5)

        session = _FakeSession(
            board=board,
            layout=layout,
            components={"R": defn},
            instances=insts,
        )
        result = spread_components(ids, session)

        for cid in ids:
            pl = result.new_placements[cid]
            assert pl.position in pads, f"Position {pl.position} not on board pad"

    def test_no_pairwise_collisions_after_spread(self) -> None:
        board = _make_perfboard(20, 20)
        defn = _resistor_defn()
        ids = [uuid.uuid4() for _ in range(6)]
        insts = {c: _inst(f"R{i}", "R") for i, c in enumerate(ids)}
        layout = Layout()
        for c in ids:
            layout.placements[c] = _place(c, 10.5, 10.5)

        session = _FakeSession(
            board=board,
            layout=layout,
            components={"R": defn},
            instances=insts,
        )
        result = spread_components(ids, session)

        from protoboard.services.placement.auto_placer import _has_collision
        placed = [(cid, result.new_placements[cid]) for cid in ids]
        for i, (cid_i, pl_i) in enumerate(placed):
            existing = {cid_j: (pl_j, defn) for cid_j, pl_j in placed[:i]}
            assert not _has_collision(pl_i, defn, existing)

    def test_partial_spread_unselected_unchanged(self) -> None:
        board = _make_perfboard(20, 20)
        defn = _resistor_defn()
        all_ids = [uuid.uuid4() for _ in range(5)]
        mobile_ids = all_ids[:3]
        fixed_ids = all_ids[3:]
        insts = {c: _inst(f"R{i}", "R") for i, c in enumerate(all_ids)}
        layout = Layout()
        orig_positions = {c: Point(float(i) + 0.5, 0.5) for i, c in enumerate(all_ids)}
        for c in all_ids:
            layout.placements[c] = _place(c, orig_positions[c].x, orig_positions[c].y)

        session = _FakeSession(
            board=board,
            layout=layout,
            components={"R": defn},
            instances=insts,
        )
        result = spread_components(mobile_ids, session)

        # Fixed components must not appear in moved_ids
        for cid in fixed_ids:
            assert cid not in result.moved_ids
        # Fixed components must not appear in new_placements (they were not spread)
        for cid in fixed_ids:
            assert cid not in result.new_placements

    def test_single_component_returns_empty_result(self) -> None:
        board = _make_perfboard(10, 10)
        defn = _resistor_defn()
        c = uuid.uuid4()
        inst = _inst("R1", "R")
        layout = Layout()
        layout.placements[c] = _place(c, 0.5, 0.5)

        session = _FakeSession(
            board=board,
            layout=layout,
            components={"R": defn},
            instances={c: inst},
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
        insts = {c: _inst(f"R{i}", "R") for i, c in enumerate(ids)}
        layout = Layout()
        rotations = [0, 90, 180]
        for i, c in enumerate(ids):
            layout.placements[c] = ComponentPlacement(
                component_id=c,
                position=Point(5.5, 5.5),
                rotation=rotations[i],
                side="top",
            )

        session = _FakeSession(
            board=board,
            layout=layout,
            components={"R": defn},
            instances=insts,
        )
        result = spread_components(ids, session)

        for i, c in enumerate(ids):
            assert result.new_placements[c].rotation == rotations[i]

    def test_undo_restores_original_positions(self) -> None:
        board = _make_perfboard(20, 20)
        defn = _resistor_defn()
        ids = [uuid.uuid4() for _ in range(4)]
        insts = {c: _inst(f"R{i}", "R") for i, c in enumerate(ids)}
        layout = Layout()
        for c in ids:
            layout.placements[c] = _place(c, 10.5, 10.5)

        session = _FakeSession(
            board=board,
            layout=layout,
            components={"R": defn},
            instances=insts,
        )
        orig_positions = {c: layout.placements[c].position for c in ids}

        result = spread_components(ids, session)
        assert result.moved_ids, "No components moved; test is vacuous"

        # Build and execute MacroCommand as the GUI would.
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

        # Verify spread applied.
        for cid in result.moved_ids:
            assert session.layout.placements[cid].position != orig_positions[cid]

        # Undo.
        session.stack.undo(session)

        # All original positions restored.
        for cid in ids:
            assert session.layout.placements[cid].position == orig_positions[cid]


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def _mean_pairwise_dist(pts: list[Point]) -> float:
    n = len(pts)
    if n < 2:
        return 0.0
    total = 0.0
    count = 0
    for i in range(n):
        for j in range(i + 1, n):
            total += math.hypot(pts[j].x - pts[i].x, pts[j].y - pts[i].y)
            count += 1
    return total / count
