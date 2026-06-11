# SPDX-License-Identifier: MIT
"""FlipWireFragmentCommand — multi-fragment Wire preservation.

A flip turns a same-side wire into a multi-fragment wire with a via at
each boundary.  The wire's id and endpoint knots survive — so two flips
on adjacent fragments leave a single Wire, not 2-4 separate Wires with
co-located stale joint knots.  Regression: the earlier implementation
created fresh Wire(id=uuid4) instances via ``layout.wires.append`` and
never went through ``add_wire`` / ``ensure_wire_knots``, producing the
``wire.fuse.skip_wire_count_neq_2`` log flood reported on 2026-06-11.
"""

from __future__ import annotations

import uuid

from protoboard.domain.board.board import Board
from protoboard.domain.board.boxes import BoardGrid, PadAtom, SizePolicy
from protoboard.domain.board.pads import PadLayers
from protoboard.domain.commands.component_cmds import (
    AddWireCommand,
    FlipWireFragmentCommand,
)
from protoboard.domain.components.definition import ComponentDefinition
from protoboard.domain.components.instance import ComponentInstance
from protoboard.domain.layout.knot import JointKnot
from protoboard.domain.layout.layout import Layout
from protoboard.domain.layout.wire import Wire
from protoboard.geometry import Point


def _perfboard(rows: int = 6, cols: int = 8) -> Board:
    """Perfboard with plated-through pads everywhere — every cell is a via."""
    return Board(root=BoardGrid(
        width_policy=SizePolicy.HUG,
        height_policy=SizePolicy.HUG,
        cell=PadAtom(
            width_policy=SizePolicy.HUG,
            height_policy=SizePolicy.HUG,
            layers=PadLayers(top=True, bottom=True, plated_through=True),
        ),
        rows=rows, cols=cols, row_gap=0.0, col_gap=0.0,
    ))


class _State:
    """Minimal ComponentState stub."""

    def __init__(self) -> None:
        self.board = _perfboard()
        self.layout = Layout(placements={}, wires=[])
        self.instances: dict[uuid.UUID, ComponentInstance] = {}
        self.components: dict[str, ComponentDefinition] = {}


def _add(state: _State, path: list[tuple[float, float]],
         *, insulated: bool = True, side: str = "top") -> AddWireCommand:
    wire = Wire(
        id=uuid.uuid4(),
        path=[Point(x, y) for x, y in path],
        insulated=insulated,
        side=side,  # type: ignore[arg-type]
    )
    cmd = AddWireCommand(wire=wire)
    cmd.apply(state)
    return cmd


def _joint_knots_at(state: _State, x: float, y: float) -> list[JointKnot]:
    return [
        k for k in state.layout.knots.values()
        if isinstance(k, JointKnot)
        and abs(k.position.x - x) < 1e-6
        and abs(k.position.y - y) < 1e-6
    ]


class TestSingleFlip:
    def test_middle_flip_yields_single_multi_fragment_wire(self) -> None:
        """Wire (5 vertices, top) with middle 3-vertex range flipped:
        result must be one Wire with three fragments and only the original
        endpoint joints — no new co-located JointKnots at the via points.
        """
        state = _State()
        _add(state, [(0.5, 0.5), (1.5, 0.5), (2.5, 0.5), (3.5, 0.5), (4.5, 0.5)])
        wire = list(state.layout.wires)[0]
        original_id = wire.id
        # Flip path[1..3] (vertices (1.5,0.5)..(3.5,0.5)) to bottom.
        cmd = FlipWireFragmentCommand(
            wire_id=wire.id, start_idx=1, end_idx=3, target_side="bottom",
        )
        cmd.apply(state)

        # Still exactly one wire, same id.
        assert len(state.layout.wires) == 1
        w = list(state.layout.wires)[0]
        assert w.id == original_id
        # Three fragments: top prefix, bottom middle, top suffix.
        sides = [f.side for f in w.fragments]
        assert sides == ["top", "bottom", "top"]
        # Path preserved verbatim.
        assert [(p.x, p.y) for p in w.path] == [
            (0.5, 0.5), (1.5, 0.5), (2.5, 0.5), (3.5, 0.5), (4.5, 0.5)
        ]
        # No new JointKnots minted at the via boundaries.
        assert _joint_knots_at(state, 1.5, 0.5) == []
        assert _joint_knots_at(state, 3.5, 0.5) == []

    def test_whole_wire_flip_keeps_one_fragment(self) -> None:
        state = _State()
        _add(state, [(0.5, 0.5), (1.5, 0.5), (2.5, 0.5)])
        wire = list(state.layout.wires)[0]
        cmd = FlipWireFragmentCommand(
            wire_id=wire.id, start_idx=0, end_idx=2, target_side="bottom",
        )
        cmd.apply(state)
        w = list(state.layout.wires)[0]
        assert w.id == wire.id
        assert len(w.fragments) == 1
        assert w.fragments[0].side == "bottom"


class TestAdjacentFlips:
    """The original bug: two flips on adjacent fragments produced
    separate Wire instances with co-located stale JointKnots."""

    def test_two_flips_on_adjacent_fragments_stay_one_wire(self) -> None:
        # 6 vertices → 5 segments.
        state = _State()
        _add(state, [
            (0.5, 0.5), (1.5, 0.5), (2.5, 0.5), (3.5, 0.5), (4.5, 0.5),
            (5.5, 0.5),
        ])
        wire = list(state.layout.wires)[0]
        wire_id = wire.id

        # Flip 1: vertices [1..3] to bottom — flips segments [1..2].
        FlipWireFragmentCommand(
            wire_id=wire_id, start_idx=1, end_idx=3, target_side="bottom",
        ).apply(state)
        assert len(state.layout.wires) == 1
        w = list(state.layout.wires)[0]
        assert [f.side for f in w.fragments] == ["top", "bottom", "top"]

        # Flip 2: vertices [3..5] to bottom — flips segments [3..4].
        # This is the adjacent flip the user reported: the new range
        # starts at the via vertex that ended the previous flip.
        FlipWireFragmentCommand(
            wire_id=wire_id, start_idx=3, end_idx=5, target_side="bottom",
        ).apply(state)

        # Must still be ONE wire with the same id.
        assert len(state.layout.wires) == 1, (
            f"Adjacent flips spawned extra wires: "
            f"{[str(w.id)[:8] for w in state.layout.wires]}"
        )
        w = list(state.layout.wires)[0]
        assert w.id == wire_id

        # Fragments coalesce: top prefix [0..1], then a single bottom run
        # covering vertices [1..5] = the entire suffix of the wire.
        sides = [f.side for f in w.fragments]
        assert sides == ["top", "bottom"], (
            f"Expected [top, bottom] after coalescing adjacent flips, got {sides}"
        )

        # No degree-1 JointKnots at any internal pad — those were the
        # smoking gun in the wire-merge log dump.
        for x in (1.5, 2.5, 3.5, 4.5):
            jks = _joint_knots_at(state, x, 0.5)
            assert jks == [], (
                f"Unexpected JointKnot at ({x}, 0.5) after adjacent flips: "
                f"{[str(k.id)[:8] for k in jks]}"
            )

    def test_revert_restores_pre_flip_state(self) -> None:
        state = _State()
        _add(state, [(0.5, 0.5), (1.5, 0.5), (2.5, 0.5), (3.5, 0.5)])
        wire = list(state.layout.wires)[0]
        wire_id = wire.id
        pre_path = [(p.x, p.y) for p in wire.path]
        pre_sides = [f.side for f in wire.fragments]

        cmd = FlipWireFragmentCommand(
            wire_id=wire_id, start_idx=1, end_idx=2, target_side="bottom",
        )
        cmd.apply(state)
        cmd.revert(state)

        assert len(state.layout.wires) == 1
        w = list(state.layout.wires)[0]
        assert w.id == wire_id
        assert [(p.x, p.y) for p in w.path] == pre_path
        assert [f.side for f in w.fragments] == pre_sides


class TestFusePhase0Rescue:
    """``FuseTouchingWiresCommand`` Phase 0 merges co-located degree-1
    JointKnots so legacy projects (or any wire-merge run on artefacts
    from the pre-fix flip-fragment) can collapse cleanly.
    """

    def _make_legacy_orphans(self) -> _State:
        """Hand-craft the bug state directly in the layout: two wires of
        the same net, same side, both insulated, each with its own
        degree-1 JointKnot at the same pad position (4.5, 0.5)."""
        from protoboard.domain.layout.knot import JointKnot
        from protoboard.domain.layout.wire import Fragment, KnotRef
        state = _State()
        # Wire 1: (0.5, 0.5) → (4.5, 0.5).
        w1_a_id = uuid.uuid4()
        w1_b_id = uuid.uuid4()
        w1 = Wire(
            id=uuid.uuid4(),
            path=[Point(0.5, 0.5), Point(4.5, 0.5)],
            insulated=True, side="top",
            fragments=[Fragment(path=[Point(0.5, 0.5), Point(4.5, 0.5)], side="top")],
            a=KnotRef(id=w1_a_id), b=KnotRef(id=w1_b_id),
        )
        # Wire 2: (4.5, 0.5) → (4.5, 4.5).  Endpoint sits on the same
        # pad as w1's `b`, but uses its own JointKnot — the orphan
        # scenario.
        w2_a_id = uuid.uuid4()
        w2_b_id = uuid.uuid4()
        w2 = Wire(
            id=uuid.uuid4(),
            path=[Point(4.5, 0.5), Point(4.5, 4.5)],
            insulated=True, side="top",
            fragments=[Fragment(path=[Point(4.5, 0.5), Point(4.5, 4.5)], side="top")],
            a=KnotRef(id=w2_a_id), b=KnotRef(id=w2_b_id),
        )
        # Install knots + wires.
        state.layout.knots[w1_a_id] = JointKnot(
            id=w1_a_id, position=Point(0.5, 0.5), wire_ids=frozenset({w1.id}),
        )
        state.layout.knots[w1_b_id] = JointKnot(
            id=w1_b_id, position=Point(4.5, 0.5), wire_ids=frozenset({w1.id}),
        )
        state.layout.knots[w2_a_id] = JointKnot(
            id=w2_a_id, position=Point(4.5, 0.5), wire_ids=frozenset({w2.id}),
        )
        state.layout.knots[w2_b_id] = JointKnot(
            id=w2_b_id, position=Point(4.5, 4.5), wire_ids=frozenset({w2.id}),
        )
        state.layout.wires.append(w1)
        state.layout.wires.append(w2)
        return state

    def test_phase0_merges_colocated_degree_1_pair(self) -> None:
        from protoboard.domain.commands.component_cmds import (
            FuseTouchingWiresCommand,
        )
        state = self._make_legacy_orphans()
        wire_ids = frozenset(w.id for w in state.layout.wires)

        # Before: two distinct degree-1 JointKnots at (4.5, 0.5).
        pre_at_pad = _joint_knots_at(state, 4.5, 0.5)
        assert len(pre_at_pad) == 2

        cmd = FuseTouchingWiresCommand(wire_ids=wire_ids)
        cmd.apply(state)

        # After Phase 0 + collapse: the two wires fused into one Wire,
        # so the pad position now has either zero JointKnots (collapsed
        # away) or one knot of degree ≤ 2 — the smoking-gun pattern of
        # two separate degree-1 knots is gone.
        post_at_pad = _joint_knots_at(state, 4.5, 0.5)
        assert len(post_at_pad) <= 1, (
            f"Phase 0 left {len(post_at_pad)} co-located JointKnots at (4.5, 0.5)"
        )

    def test_phase0_revert_restores_pre_apply_state(self) -> None:
        from protoboard.domain.commands.component_cmds import (
            FuseTouchingWiresCommand,
        )
        state = self._make_legacy_orphans()
        wire_ids = frozenset(w.id for w in state.layout.wires)
        n_wires_pre = len(state.layout.wires)
        knots_pre = {k: (type(v).__name__, v.wire_ids)
                     for k, v in state.layout.knots.items()}

        cmd = FuseTouchingWiresCommand(wire_ids=wire_ids)
        cmd.apply(state)
        cmd.revert(state)

        # Wires restored.
        assert len(state.layout.wires) == n_wires_pre
        # Knot population restored — every pre-apply knot exists again
        # with the same wire_ids back-reference.
        for k_id, (kind, wires) in knots_pre.items():
            assert k_id in state.layout.knots, f"knot {k_id} missing after revert"
            k = state.layout.knots[k_id]
            assert type(k).__name__ == kind
            assert k.wire_ids == wires, f"knot {k_id} wire_ids: {k.wire_ids} vs {wires}"


class TestNoStaleJointKnots:
    """Direct regression of the wire-merge log dump pattern: many
    degree-1 JointKnots at the same pad position."""

    def test_no_colocated_degree_1_joints_after_flips(self) -> None:
        state = _State()
        _add(state, [
            (0.5, 0.5), (1.5, 0.5), (2.5, 0.5), (3.5, 0.5), (4.5, 0.5),
            (5.5, 0.5),
        ])
        wire_id = list(state.layout.wires)[0].id

        FlipWireFragmentCommand(
            wire_id=wire_id, start_idx=1, end_idx=3, target_side="bottom",
        ).apply(state)
        FlipWireFragmentCommand(
            wire_id=wire_id, start_idx=3, end_idx=5, target_side="bottom",
        ).apply(state)

        # Count distinct JointKnots per (x, y) position.
        from collections import defaultdict
        per_pos: dict[tuple[float, float], list[JointKnot]] = defaultdict(list)
        for k in state.layout.knots.values():
            if isinstance(k, JointKnot):
                per_pos[(k.position.x, k.position.y)].append(k)

        for pos, knots in per_pos.items():
            assert len(knots) <= 1, (
                f"Found {len(knots)} co-located JointKnots at {pos}: "
                f"{[str(k.id)[:8] for k in knots]} (degrees "
                f"{[len(k.wire_ids) for k in knots]})"
            )
