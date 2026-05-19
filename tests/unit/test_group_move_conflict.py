# SPDX-License-Identifier: MIT
"""Tests for check_group_move_conflicts — atomic group-move conflict classification."""

from __future__ import annotations

import uuid

from protoboard.domain.components.definition import ComponentDefinition
from protoboard.domain.components.footprints import SIL
from protoboard.domain.components.instance import ComponentInstance
from protoboard.domain.layout.group import ComponentGroup
from protoboard.domain.layout.pad_conflict import (
    MoveConflictSeverity,
    check_group_move_conflicts,
)
from protoboard.domain.layout.placement import ComponentPlacement
from protoboard.domain.layout.wire import WireSegment
from protoboard.domain.schematic.net import Net
from protoboard.domain.schematic.schematic import PinRef, Schematic
from protoboard.geometry import Point

ROOT_GID = uuid.UUID("00000000-0000-0000-0000-000000000001")

# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

def _defn(name: str, n: int = 1) -> ComponentDefinition:
    return ComponentDefinition(
        name=name, footprint=SIL(n), pin_names={i: str(i) for i in range(1, n + 1)}
    )


def _inst(defn: ComponentDefinition) -> ComponentInstance:
    return ComponentInstance.new(definition_ref=defn.name, refdes="X1", value="")


def _pl(comp_id: uuid.UUID, x: float, y: float) -> ComponentPlacement:
    return ComponentPlacement(
        component_id=comp_id, position=Point(x, y), rotation=0, side="top"
    )


def _wire(*pts: tuple[float, float]) -> WireSegment:
    return WireSegment(
        id=uuid.uuid4(),
        path=[Point(x, y) for x, y in pts],
        insulated=True,
        side="top",
    )


def _schematic(*nets: tuple[uuid.UUID, list[tuple[uuid.UUID, int]]]) -> Schematic:
    net_objs: dict[uuid.UUID, Net] = {}
    for net_id, pins in nets:
        net_objs[net_id] = Net(
            id=net_id,
            name=f"N{net_id.int}",
            pins=frozenset(PinRef(component_id=c, pin_number=p) for c, p in pins),
        )
    return Schematic(nets=net_objs)


def _root_group(*comp_ids: uuid.UUID) -> dict[uuid.UUID, ComponentGroup]:
    return {
        ROOT_GID: ComponentGroup(
            id=ROOT_GID,
            name="root",
            component_ids=frozenset(comp_ids),
            child_group_ids=frozenset(),
            exported_pins=frozenset(),
        )
    }


# ---------------------------------------------------------------------------
# Tests
# ---------------------------------------------------------------------------

class TestCheckGroupMoveConflicts:

    def test_clear_when_no_wires_no_pad_conflict(self) -> None:
        defn = _defn("A")
        inst = _inst(defn)
        pl = _pl(inst.id, 0.0, 0.0)
        groups = _root_group(inst.id)

        result = check_group_move_conflicts(
            moved_ids={inst.id},
            active_group_id=ROOT_GID,
            layout_placements={inst.id: pl},
            layout_wires=[],
            layout_groups=groups,
            instances={inst.id: inst},
            components={defn.name: defn},
            schematic=_schematic(),
        )
        assert result.severity == MoveConflictSeverity.CLEAR

    def test_blocked_when_pad_on_pad_different_net(self) -> None:
        """Moving comp A onto comp B's pad with a different net → BLOCKED."""
        net_a, net_b = uuid.uuid4(), uuid.uuid4()
        defn_a = _defn("A")
        defn_b = _defn("B")
        inst_a = _inst(defn_a)
        inst_b = _inst(defn_b)
        # After drag, A lands on same pad as B
        pl_a = _pl(inst_a.id, 3.0, 0.0)   # new position, pin at (3,0)
        pl_b = _pl(inst_b.id, 3.0, 0.0)   # existing, pin also at (3,0)
        groups = _root_group(inst_a.id)

        sch = _schematic(
            (net_a, [(inst_a.id, 1)]),
            (net_b, [(inst_b.id, 1)]),
        )

        result = check_group_move_conflicts(
            moved_ids={inst_a.id},
            active_group_id=ROOT_GID,
            layout_placements={inst_a.id: pl_a, inst_b.id: pl_b},
            layout_wires=[],
            layout_groups=groups,
            instances={inst_a.id: inst_a, inst_b.id: inst_b},
            components={"A": defn_a, "B": defn_b},
            schematic=sch,
        )
        assert result.severity == MoveConflictSeverity.BLOCKED
        assert len(result.blocking_pad_positions) == 1

    def test_removable_when_wire_from_active_group_crosses_pad(self) -> None:
        """A wire in the active group crosses an alien pad → REMOVABLE."""
        net_a, net_b = uuid.uuid4(), uuid.uuid4()
        defn_a = _defn("A")
        defn_b = _defn("B")
        inst_a = _inst(defn_a)
        inst_b = _inst(defn_b)
        pl_a = _pl(inst_a.id, 0.0, 0.0)
        pl_b = _pl(inst_b.id, 5.0, 0.0)  # external, pad at (5,0)

        # Wire in active group passing through (5,0)
        w = _wire((0.0, 0.0), (7.0, 0.0))

        groups = _root_group(inst_a.id)
        sch = _schematic(
            (net_a, [(inst_a.id, 1)]),
            (net_b, [(inst_b.id, 1)]),
        )

        result = check_group_move_conflicts(
            moved_ids={inst_a.id},
            active_group_id=ROOT_GID,
            layout_placements={inst_a.id: pl_a, inst_b.id: pl_b},
            layout_wires=[w],
            layout_groups=groups,
            instances={inst_a.id: inst_a, inst_b.id: inst_b},
            components={"A": defn_a, "B": defn_b},
            schematic=sch,
        )
        assert result.severity == MoveConflictSeverity.REMOVABLE
        assert w.id in result.removable_wire_ids

    def test_blocked_by_subgroup_wire_crossing_pad(self) -> None:
        """Wire internal to a subgroup crosses an alien pad → BLOCKED."""
        net_a, net_b = uuid.uuid4(), uuid.uuid4()
        child_gid = uuid.uuid4()

        defn_a = _defn("A")
        defn_b = _defn("B")
        # SIL(2) places pins vertically: pin1 at (x,y), pin2 at (x,y+1).
        # inst_c at (0,0): pins at (0,0) and (0,1). bbox = (0,0,0,1).
        # Wire (0,0)→(0,1) is fully internal to subgroup bbox (tol=0.5 → x in [-0.5,0.5]).
        # B's external pad at (0, 0.5) — midpoint of subgroup wire → crossing.
        defn_c = _defn("C", n=2)
        inst_a = _inst(defn_a)
        inst_b = _inst(defn_b)
        inst_c = _inst(defn_c)

        pl_a = _pl(inst_a.id, 3.0, 0.0)
        pl_c = _pl(inst_c.id, 0.0, 0.0)   # subgroup: pins at (0,0) and (0,1)
        pl_b = _pl(inst_b.id, 0.0, 0.5)   # external, pad at (0,0.5) — inside wire

        subgroup_wire = _wire((0.0, 0.0), (0.0, 1.0))

        groups: dict[uuid.UUID, ComponentGroup] = {
            ROOT_GID: ComponentGroup(
                id=ROOT_GID,
                name="root",
                component_ids=frozenset({inst_a.id}),
                child_group_ids=frozenset({child_gid}),
                exported_pins=frozenset(),
            ),
            child_gid: ComponentGroup(
                id=child_gid,
                name="sub",
                component_ids=frozenset({inst_c.id}),
                child_group_ids=frozenset(),
                exported_pins=frozenset(),
            ),
        }

        sch = _schematic(
            (net_a, [(inst_a.id, 1), (inst_c.id, 1)]),
            (net_b, [(inst_b.id, 1)]),
        )

        result = check_group_move_conflicts(
            moved_ids={inst_a.id, inst_c.id},
            active_group_id=ROOT_GID,
            layout_placements={inst_a.id: pl_a, inst_b.id: pl_b, inst_c.id: pl_c},
            layout_wires=[subgroup_wire],
            layout_groups=groups,
            instances={inst_a.id: inst_a, inst_b.id: inst_b, inst_c.id: inst_c},
            components={"A": defn_a, "B": defn_b, "C": defn_c},
            schematic=sch,
        )
        assert result.severity == MoveConflictSeverity.BLOCKED

    def test_group_with_two_members_one_conflicts_returns_blocked(self) -> None:
        """Group has 2 members; one conflicts pad-pad different-net → BLOCKED (atomic)."""
        net_a, net_b, net_c = uuid.uuid4(), uuid.uuid4(), uuid.uuid4()
        defn_a = _defn("A")
        defn_b = _defn("B")
        defn_ext = _defn("EXT")
        inst_a = _inst(defn_a)
        inst_b = _inst(defn_b)
        inst_ext = _inst(defn_ext)

        # Group members: A at (0,0), B at (1,0)
        # External: EXT at (5,0)
        # After drag: A moves to (5,0) — conflicts with EXT
        pl_a = _pl(inst_a.id, 5.0, 0.0)  # new (post-drag)
        pl_b = _pl(inst_b.id, 6.0, 0.0)  # new (post-drag, no conflict)
        pl_ext = _pl(inst_ext.id, 5.0, 0.0)  # existing external

        groups = {
            ROOT_GID: ComponentGroup(
                id=ROOT_GID,
                name="root",
                component_ids=frozenset({inst_a.id, inst_b.id}),
                child_group_ids=frozenset(),
                exported_pins=frozenset(),
            )
        }
        sch = _schematic(
            (net_a, [(inst_a.id, 1)]),
            (net_b, [(inst_b.id, 1)]),
            (net_c, [(inst_ext.id, 1)]),
        )

        result = check_group_move_conflicts(
            moved_ids={inst_a.id, inst_b.id},
            active_group_id=ROOT_GID,
            layout_placements={inst_a.id: pl_a, inst_b.id: pl_b, inst_ext.id: pl_ext},
            layout_wires=[],
            layout_groups=groups,
            instances={inst_a.id: inst_a, inst_b.id: inst_b, inst_ext.id: inst_ext},
            components={"A": defn_a, "B": defn_b, "EXT": defn_ext},
            schematic=sch,
        )
        assert result.severity == MoveConflictSeverity.BLOCKED


class TestCheckGroupMoveConflictsPadIndex:
    """Parity check: pad_index= branch returns the same severity / blocking
    positions as the legacy from-layout branch."""

    def _build_index(self, board, placements, instances, components, schematic,
                     wires=None):
        from protoboard.domain.board.pad_state import PadIndex
        from protoboard.domain.layout.layout import Layout

        lay = Layout()
        lay.ensure_root_group()
        lay.placements.update(placements)
        if wires:
            lay.wires.extend(wires)
        return PadIndex(board, lay, schematic, components, instances)

    def _perfboard(self, rows: int, cols: int):
        from protoboard.domain.board.board import Board
        from protoboard.domain.board.boxes import BoardGrid, PadAtom, SizePolicy
        from protoboard.domain.board.pads import PadLayers
        grid = BoardGrid(
            width_policy=SizePolicy.HUG, height_policy=SizePolicy.HUG,
            cell=PadAtom(
                width_policy=SizePolicy.HUG, height_policy=SizePolicy.HUG,
                layers=PadLayers(top=True, bottom=True, plated_through=True),
            ),
            rows=rows, cols=cols, row_gap=0.0, col_gap=0.0,
        )
        return Board(root=grid)

    def test_pad_on_pad_blocked_via_pad_index(self) -> None:
        """check_group_move_conflicts with pad_index=… returns BLOCKED for
        a pad-on-pad different-net move just like the legacy branch.

        SIL(1) pin1 at placement.position; pad centers are at (col+0.5, row+0.5).
        We place both components at (2.5, 2.5) so their pins land on a real pad.
        """
        net_a, net_b = uuid.uuid4(), uuid.uuid4()
        defn_a, defn_b = _defn("A"), _defn("B")
        inst_a, inst_b = _inst(defn_a), _inst(defn_b)
        pl_a = _pl(inst_a.id, 2.5, 2.5)
        pl_b = _pl(inst_b.id, 2.5, 2.5)
        groups = _root_group(inst_a.id)
        sch = _schematic(
            (net_a, [(inst_a.id, 1)]),
            (net_b, [(inst_b.id, 1)]),
        )

        board = self._perfboard(5, 5)
        idx = self._build_index(
            board,
            {inst_a.id: pl_a, inst_b.id: pl_b},
            {inst_a.id: inst_a, inst_b.id: inst_b},
            {"A": defn_a, "B": defn_b},
            sch,
        )

        # Legacy path.
        legacy = check_group_move_conflicts(
            moved_ids={inst_a.id},
            active_group_id=ROOT_GID,
            layout_placements={inst_a.id: pl_a, inst_b.id: pl_b},
            layout_wires=[],
            layout_groups=groups,
            instances={inst_a.id: inst_a, inst_b.id: inst_b},
            components={"A": defn_a, "B": defn_b},
            schematic=sch,
        )

        # pad_index= branch.
        via_idx = check_group_move_conflicts(
            moved_ids={inst_a.id},
            active_group_id=ROOT_GID,
            layout_placements={inst_a.id: pl_a, inst_b.id: pl_b},
            layout_wires=[],
            layout_groups=groups,
            instances={inst_a.id: inst_a, inst_b.id: inst_b},
            components={"A": defn_a, "B": defn_b},
            schematic=sch,
            pad_index=idx,
        )

        assert legacy.severity == via_idx.severity == MoveConflictSeverity.BLOCKED

    def test_clear_via_pad_index(self) -> None:
        """No-conflict scenario stays CLEAR through the pad_index path."""
        defn = _defn("A")
        inst = _inst(defn)
        pl = _pl(inst.id, 0.5, 0.5)  # pad center
        groups = _root_group(inst.id)
        sch = _schematic()
        board = self._perfboard(3, 3)
        idx = self._build_index(
            board, {inst.id: pl}, {inst.id: inst}, {"A": defn}, sch,
        )
        via_idx = check_group_move_conflicts(
            moved_ids={inst.id},
            active_group_id=ROOT_GID,
            layout_placements={inst.id: pl},
            layout_wires=[],
            layout_groups=groups,
            instances={inst.id: inst},
            components={defn.name: defn},
            schematic=sch,
            pad_index=idx,
        )
        assert via_idx.severity == MoveConflictSeverity.CLEAR
