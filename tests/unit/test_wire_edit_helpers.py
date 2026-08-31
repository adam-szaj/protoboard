# SPDX-License-Identifier: MIT
"""Tests for the pure wire-fragment edit helpers (``gui/selection/wire_edit.py``).

These exercise the command builders that the v2 SelectTool's wire-edit actions
call — reroute / delete / excise / dedupe — directly on plain wire lists + a
``wire_net_map`` + a :class:`WireEditState`, with no Qt, no BoardView, no
SelectTool.  They mirror the legacy parity (``test_excise_cross_wire.py``) but
target the lifted free functions.
"""

from __future__ import annotations

import uuid
from collections.abc import Sequence
from pathlib import Path
from typing import cast

import pytest

from protoboard.controller.selection.wire_edit import (
    WireEditState,
    WireRange,
    build_delete_command,
    build_delete_range_edit,
    build_flip_fragment_command,
    build_reroute_command,
    build_set_insulated_range_command,
    dedupe_adjacent,
    excise_cross_wire_range,
    net_wires_ordered,
    outer_endpoint_idx,
)
from protoboard.domain.board.board import Board
from protoboard.domain.board.boxes import BoardGrid, PadAtom, SizePolicy
from protoboard.domain.board.pads import PadLayers
from protoboard.domain.commands.base import MacroCommand
from protoboard.domain.commands.component_cmds import (
    AddWireCommand,
    FlipWireFragmentCommand,
    RemoveWireCommand,
    ReplaceWireFragmentsCommand,
    SetWireInsulatedCommand,
)
from protoboard.domain.layout.wire import Wire
from protoboard.geometry import Point

_LIBRARY_DIR = Path(__file__).parent.parent / "fixtures" / "library"


def _wire(*pts: tuple[float, float], insulated: bool = False) -> Wire:
    return Wire(
        id=uuid.uuid4(),
        path=[Point(x, y) for x, y in pts],
        insulated=insulated,
        side="bottom",
    )


def _net_map(*wires: Wire) -> dict[uuid.UUID, uuid.UUID]:
    net_id = uuid.uuid4()
    return {w.id: net_id for w in wires}


# -- dedupe_adjacent ----------------------------------------------------------


class TestDedupeAdjacent:
    def test_collapses_consecutive_dups(self) -> None:
        out = dedupe_adjacent(
            [Point(0, 0), Point(0, 0), Point(1, 0), Point(1, 0), Point(2, 0)]
        )
        assert out == [Point(0, 0), Point(1, 0), Point(2, 0)]

    def test_keeps_non_adjacent_repeats(self) -> None:
        out = dedupe_adjacent([Point(0, 0), Point(1, 0), Point(0, 0)])
        assert out == [Point(0, 0), Point(1, 0), Point(0, 0)]

    def test_empty(self) -> None:
        assert dedupe_adjacent([]) == []


# -- outer_endpoint_idx (port of legacy static-method tests) -----------------


class TestOuterEndpointIdx:
    def test_outer_is_first_when_last_is_shared(self) -> None:
        a = _wire((1.5, 10.5), (2.5, 9.5))
        b = _wire((2.5, 9.5), (3.5, 10.5), (4.5, 11.5))
        assert outer_endpoint_idx(a, b) == 0

    def test_outer_is_last_when_first_is_shared(self) -> None:
        a = _wire((2.5, 9.5), (3.5, 10.5), (4.5, 11.5))
        b = _wire((1.5, 10.5), (2.5, 9.5))
        assert outer_endpoint_idx(a, b) == len(a.path) - 1

    def test_no_shared_falls_back_to_zero(self) -> None:
        a = _wire((0, 0), (1, 0))
        b = _wire((10, 10), (11, 10))
        assert outer_endpoint_idx(a, b) == 0


# -- net_wires_ordered --------------------------------------------------------


class TestNetWiresOrdered:
    def test_single_wire_chain(self) -> None:
        a = _wire((0, 0), (1, 0))
        nm = _net_map(a)
        net_id = nm[a.id]
        assert net_wires_ordered([a], nm, a.id, a.id, net_id) == [a]

    def test_two_wire_chain_sharing_a_point(self) -> None:
        a = _wire((0, 0), (1, 0))
        b = _wire((1, 0), (2, 0))
        nm = _net_map(a, b)
        chain = net_wires_ordered([a, b], nm, a.id, b.id, nm[a.id])
        assert chain is not None
        assert [w.id for w in chain] == [a.id, b.id]

    def test_disconnected_returns_none(self) -> None:
        a = _wire((0, 0), (1, 0))
        b = _wire((10, 0), (11, 0))
        nm = _net_map(a, b)
        assert net_wires_ordered([a, b], nm, a.id, b.id, nm[a.id]) is None


# -- excise_cross_wire_range (parity with test_excise_cross_wire.py) ---------


class TestExciseCrossWire:
    def _chain(self) -> tuple[Wire, Wire, dict[uuid.UUID, uuid.UUID]]:
        # wireA shares its FIRST point (2.5,9.5) with wireB's LAST point.
        wire_a = _wire(
            (2.5, 9.5),
            (3.5, 10.5),
            (4.5, 11.5),
            (5.5, 12.5),
            (6.5, 12.5),
            (7.5, 11.5),
            (8.5, 11.5),
        )
        wire_b = _wire((1.5, 10.5), (2.5, 9.5))
        return wire_a, wire_b, _net_map(wire_a, wire_b)

    def test_excise_keeps_outer_suffix_of_start_wire(self) -> None:
        wire_a, wire_b, nm = self._chain()
        state = WireEditState(
            start_wire_id=wire_a.id,
            start_idx=3,
            end_wire_id=wire_b.id,
            end_idx=0,
        )
        result = excise_cross_wire_range([wire_a, wire_b], nm, state)
        assert result is not None
        macro, _start, _end = result
        assert isinstance(macro, MacroCommand)
        cmds = macro._commands
        # wireA inner endpoint → path[3]; kept suffix [3:] via Remove + Add.
        assert any(
            isinstance(c, RemoveWireCommand) and c.wire_id == wire_a.id for c in cmds
        )
        added_a = next(
            c
            for c in cmds
            if isinstance(c, AddWireCommand) and c.wire.path[0] == wire_a.path[3]
        )
        assert list(added_a.wire.path) == list(wire_a.path[3:])
        # wireB outer end is path[0], edit at path[0] → collapses → removed.
        assert any(
            isinstance(c, RemoveWireCommand) and c.wire_id == wire_b.id for c in cmds
        )

    def test_disconnected_returns_none(self) -> None:
        a = _wire((0, 0), (1, 0))
        b = _wire((10, 0), (11, 0))
        nm = _net_map(a, b)
        state = WireEditState(
            start_wire_id=a.id, start_idx=0, end_wire_id=b.id, end_idx=1
        )
        assert excise_cross_wire_range([a, b], nm, state) is None


# -- build_simplify_range_command (Stage 5: op-on-range, whole = full span) ----


def _simplify_inputs():
    """A SimplifyInputs snapshot from a real (empty) session."""
    from protoboard.controller.selection.wire_ops import build_simplify_inputs

    return build_simplify_inputs(_session())


def _simplify_wire(request):  # type: ignore[no-untyped-def]
    from protoboard.services.editing.routing_adapters import DefaultWireSimplifier

    return DefaultWireSimplifier().simplify(request)


class TestBuildSimplifyRange:
    def test_full_span_simplifies_whole_wire(self) -> None:
        from protoboard.controller.selection.wire_edit import (
            WireRange,
            build_simplify_range_command,
        )
        from protoboard.controller.selection.wire_ops import build_simplify_inputs
        from protoboard.domain.routing.simplify_mode import SimplifyMode

        sess = _session()
        # Collinear detour (1,0),(2,0) between endpoints → straightens to [(0,0),(3,0)].
        wire = _wire((0, 0), (1, 0), (2, 0), (3, 0), insulated=True)
        sess.layout.wires.append(wire)
        cmd = build_simplify_range_command(
            sess.layout,
            cast(Sequence[Wire], sess.layout.wires),
            WireRange.full_span(wire),
            build_simplify_inputs(sess),
            SimplifyMode.CHEBYSHEV,
            _simplify_wire,
        )
        assert isinstance(cmd, ReplaceWireFragmentsCommand)
        assert cmd.wire_id == wire.id  # id preserved → net derivation stable
        flat = [p for f in cmd.new_fragments for p in f.path]
        assert flat[0] == Point(0, 0) and flat[-1] == Point(3, 0)

    def test_sub_range_keeps_prefix_and_suffix(self) -> None:
        from protoboard.controller.selection.wire_edit import (
            WireRange,
            build_simplify_range_command,
        )
        from protoboard.controller.selection.wire_ops import build_simplify_inputs
        from protoboard.domain.routing.simplify_mode import SimplifyMode

        sess = _session()
        # Two collinear runs; simplify only the middle slice [1..4], keep the
        # first and last vertices as fixed context.
        wire = _wire((0, 0), (1, 0), (2, 0), (3, 0), (4, 0), (5, 0), insulated=True)
        sess.layout.wires.append(wire)
        r = WireRange(wire.id, 1, wire.id, 4)
        cmd = build_simplify_range_command(
            sess.layout,
            cast(Sequence[Wire], sess.layout.wires),
            r,
            build_simplify_inputs(sess),
            SimplifyMode.CHEBYSHEV,
            _simplify_wire,
        )
        assert isinstance(cmd, ReplaceWireFragmentsCommand)
        flat = [p for f in cmd.new_fragments for p in f.path]
        # Endpoints untouched; the redundant interior collapses.
        assert flat[0] == Point(0, 0) and flat[-1] == Point(5, 0)
        assert len(flat) < len(wire.path)

    def test_cross_wire_returns_none(self) -> None:
        from protoboard.controller.selection.wire_edit import (
            WireRange,
            build_simplify_range_command,
        )
        from protoboard.domain.routing.simplify_mode import SimplifyMode

        a = _wire((0, 0), (1, 0), insulated=True)
        b = _wire((1, 0), (2, 0), insulated=True)
        r = WireRange(a.id, 0, b.id, 1)
        cmd = build_simplify_range_command(
            None,
            [a, b],
            r,
            _simplify_inputs(),
            SimplifyMode.CHEBYSHEV,
            _simplify_wire,
        )
        assert cmd is None

    def test_nothing_to_shorten_returns_none(self) -> None:
        from protoboard.controller.selection.wire_edit import (
            WireRange,
            build_simplify_range_command,
        )
        from protoboard.domain.routing.simplify_mode import SimplifyMode

        # Already-minimal straight wire → no change.
        wire = _wire((0, 0), (3, 0), insulated=True)
        cmd = build_simplify_range_command(
            None,
            [wire],
            WireRange.full_span(wire),
            _simplify_inputs(),
            SimplifyMode.CHEBYSHEV,
            _simplify_wire,
        )
        assert cmd is None


# -- build_reroute_command ----------------------------------------------------


class TestBuildReroute:
    def test_same_wire_replaces_fragments_with_waypoints(self) -> None:
        sess = _session()
        wire = _wire((0, 0), (1, 0), (2, 0), (3, 0))
        sess.layout.wires.append(wire)
        nm = _net_map(wire)
        state = WireEditState(
            start_wire_id=wire.id,
            start_idx=1,
            end_wire_id=wire.id,
            end_idx=2,
            waypoints=(Point(1.5, 1.0),),
        )
        cmd = build_reroute_command(
            sess.layout, cast(Sequence[Wire], sess.layout.wires), nm, state
        )
        assert isinstance(cmd, ReplaceWireFragmentsCommand)
        assert cmd.wire_id == wire.id
        # New path = prefix[:2] + waypoint + suffix[2:] = the rerouted detour.
        new_pts = [p for frag in cmd.new_fragments for p in frag.path]
        assert Point(1.5, 1.0) in new_pts

    def test_same_wire_degenerate_path_returns_none(self) -> None:
        sess = _session()
        wire = _wire((0, 0), (1, 0))
        sess.layout.wires.append(wire)
        nm = _net_map(wire)
        # Whole-wire range with no waypoints collapses to < 2 points.
        state = WireEditState(
            start_wire_id=wire.id,
            start_idx=0,
            end_wire_id=wire.id,
            end_idx=1,
        )
        # prefix[:1] + [] + suffix[1:] = [p0, p1] — still 2 points, valid.
        cmd = build_reroute_command(
            sess.layout, cast(Sequence[Wire], sess.layout.wires), nm, state
        )
        assert isinstance(cmd, ReplaceWireFragmentsCommand)

    def test_cross_wire_builds_excise_plus_addwire_macro(self) -> None:
        sess = _session()
        wire_a = _wire((2.5, 9.5), (3.5, 10.5), (4.5, 11.5), (5.5, 12.5))
        wire_b = _wire((1.5, 10.5), (2.5, 9.5))
        sess.layout.wires.extend([wire_a, wire_b])
        nm = _net_map(wire_a, wire_b)
        state = WireEditState(
            start_wire_id=wire_a.id,
            start_idx=2,
            end_wire_id=wire_b.id,
            end_idx=0,
            waypoints=(Point(3.0, 9.0),),
        )
        cmd = build_reroute_command(
            sess.layout, cast(Sequence[Wire], sess.layout.wires), nm, state
        )
        assert isinstance(cmd, MacroCommand)
        # The outer macro = [excise_macro, AddWire(new_path)].
        assert any(isinstance(c, AddWireCommand) for c in cmd._commands)

    @pytest.mark.parametrize("insulated", [False, True])
    def test_cross_wire_reroute_inherits_insulation(self, insulated: bool) -> None:
        """A cross-wire reroute must keep the rerouted wire's insulation —
        a bare reroute stays bare (it used to be forced insulated)."""
        sess = _session()
        wire_a = _wire(
            (2.5, 9.5),
            (3.5, 10.5),
            (4.5, 11.5),
            (5.5, 12.5),
            insulated=insulated,
        )
        wire_b = _wire((1.5, 10.5), (2.5, 9.5), insulated=insulated)
        sess.layout.wires.extend([wire_a, wire_b])
        nm = _net_map(wire_a, wire_b)
        state = WireEditState(
            start_wire_id=wire_a.id,
            start_idx=2,
            end_wire_id=wire_b.id,
            end_idx=0,
            waypoints=(Point(3.0, 9.0),),
        )
        cmd = build_reroute_command(
            sess.layout, cast(Sequence[Wire], sess.layout.wires), nm, state
        )
        assert isinstance(cmd, MacroCommand)
        add = next(c for c in cmd._commands if isinstance(c, AddWireCommand))
        assert add.wire.insulated is insulated


# -- build_delete_command -----------------------------------------------------


class TestBuildDelete:
    def test_whole_same_wire_removes_wire(self) -> None:
        wire = _wire((0, 0), (1, 0), (2, 0))
        nm = _net_map(wire)
        state = WireEditState(
            start_wire_id=wire.id,
            start_idx=0,
            end_wire_id=wire.id,
            end_idx=2,
        )
        cmd = build_delete_command([wire], nm, state)
        assert isinstance(cmd, RemoveWireCommand)
        assert cmd.wire_id == wire.id

    def test_partial_same_wire_cuts_into_two_halves(self) -> None:
        wire = _wire((0, 0), (1, 0), (2, 0), (3, 0), (4, 0))
        nm = _net_map(wire)
        # Remove interior [2..3): keep left [0..2] and right [3..4].
        state = WireEditState(
            start_wire_id=wire.id,
            start_idx=1,
            end_wire_id=wire.id,
            end_idx=3,
        )
        cmd = build_delete_command([wire], nm, state)
        assert isinstance(cmd, MacroCommand)
        adds = [c for c in cmd._commands if isinstance(c, AddWireCommand)]
        assert len(adds) == 2
        assert any(isinstance(c, RemoveWireCommand) for c in cmd._commands)

    def test_cross_wire_delete_is_excise(self) -> None:
        wire_a = _wire((2.5, 9.5), (3.5, 10.5), (4.5, 11.5), (5.5, 12.5))
        wire_b = _wire((1.5, 10.5), (2.5, 9.5))
        nm = _net_map(wire_a, wire_b)
        state = WireEditState(
            start_wire_id=wire_a.id,
            start_idx=2,
            end_wire_id=wire_b.id,
            end_idx=0,
        )
        cmd = build_delete_command([wire_a, wire_b], nm, state)
        assert isinstance(cmd, MacroCommand)


# -- build_set_insulated_range_command ----------------------------------------


class TestBuildSetInsulatedRangeCommand:
    def _wire(self) -> Wire:
        # 5 collinear points, one fragment (same side), bare.
        return Wire(
            id=uuid.uuid4(),
            path=[Point(0, 0), Point(1, 0), Point(2, 0), Point(3, 0), Point(4, 0)],
            insulated=False,
            side="top",
            color=None,
        )

    def test_full_span_returns_set_insulated_command_id_preserving(self) -> None:
        wire = self._wire()
        nm = {wire.id: uuid.uuid4()}
        state = WireRange.full_span(wire)
        result = build_set_insulated_range_command([wire], nm, state, insulated=True)
        assert result is not None
        assert result.was_cut is False
        assert result.survivor_wire_ids == ()
        assert isinstance(result.command, SetWireInsulatedCommand)
        assert result.command.wire_id == wire.id
        assert result.command.insulated is True

    def test_sub_range_splits_into_three_middle_insulated(self) -> None:
        wire = self._wire()
        nm = {wire.id: uuid.uuid4()}
        state = WireRange(wire.id, 1, wire.id, 3)  # middle slice idx 1..3
        result = build_set_insulated_range_command([wire], nm, state, insulated=True)
        assert result is not None
        assert result.was_cut is True
        # Two fresh survivors (left + right); target is also fresh but the
        # survivors are what the tool re-arms against.
        assert len(result.survivor_wire_ids) == 2
        assert isinstance(result.command, MacroCommand)
        assert any(isinstance(c, RemoveWireCommand) for c in result.command._commands)
        adds = [c for c in result.command._commands if isinstance(c, AddWireCommand)]
        assert len(adds) == 3
        by_len = sorted(adds, key=lambda c: len(c.wire.path))
        # left = path[:2] (2 pts), right = path[3:] (2 pts), target = path[1:4] (3 pts)
        target = by_len[-1]
        others = by_len[:-1]
        assert len(target.wire.path) == 3
        assert target.wire.insulated is True
        assert all(len(c.wire.path) == 2 for c in others)
        assert all(c.wire.insulated is False for c in others)
        assert all(c.wire.side == "top" for c in adds)
        assert {c.wire.id for c in adds} == set(result.survivor_wire_ids) | {
            target.wire.id
        }

    def test_sub_range_touching_end_yields_two_pieces(self) -> None:
        wire = self._wire()
        nm = {wire.id: uuid.uuid4()}
        state = WireRange(wire.id, 0, wire.id, 2)  # slice at the left end
        result = build_set_insulated_range_command([wire], nm, state, insulated=True)
        assert result is not None
        assert result.was_cut is True
        assert isinstance(result.command, MacroCommand)
        adds = [c for c in result.command._commands if isinstance(c, AddWireCommand)]
        # insulated target (idx 0..2) + bare right (idx 2..4) — no left piece.
        assert len(adds) == 2
        assert len(result.survivor_wire_ids) == 1

    def test_noop_when_slice_already_at_target_returns_none(self) -> None:
        wire = self._wire()  # bare
        nm = {wire.id: uuid.uuid4()}
        state = WireRange(wire.id, 1, wire.id, 3)
        result = build_set_insulated_range_command([wire], nm, state, insulated=False)
        assert result is None  # already bare, no change

    def test_cross_wire_is_refused(self) -> None:
        w1 = self._wire()
        w2 = Wire(
            id=uuid.uuid4(),
            path=[Point(4, 0), Point(5, 0)],
            insulated=False,
            side="top",
            color=None,
        )
        nm = {w1.id: uuid.uuid4(), w2.id: uuid.uuid4()}
        state = WireRange(w1.id, 2, w2.id, 1)  # cross-wire
        assert state.is_cross
        result = build_set_insulated_range_command([w1, w2], nm, state, insulated=True)
        assert result is None


# -- build_delete_range_edit --------------------------------------------------


class TestBuildDeleteRangeEdit:
    def _wire(self) -> Wire:
        return Wire(
            id=uuid.uuid4(),
            path=[Point(0, 0), Point(1, 0), Point(2, 0), Point(3, 0), Point(4, 0)],
            insulated=False,
            side="top",
            color=None,
        )

    def test_full_span_delete_is_not_a_cut(self) -> None:
        wire = self._wire()
        nm = {wire.id: uuid.uuid4()}
        state = WireRange.full_span(wire)
        result = build_delete_range_edit([wire], nm, state)
        assert result is not None
        assert result.was_cut is True  # a cut happened...
        assert result.survivor_wire_ids == ()  # ...but nothing survived → tool exits

    def test_sub_range_delete_reports_two_survivors(self) -> None:
        wire = self._wire()
        nm = {wire.id: uuid.uuid4()}
        state = WireRange(wire.id, 1, wire.id, 3)
        result = build_delete_range_edit([wire], nm, state)
        assert result is not None
        assert result.was_cut is True
        assert len(result.survivor_wire_ids) == 2

    def test_invalid_cross_returns_none(self) -> None:
        w1 = self._wire()
        w2 = Wire(
            id=uuid.uuid4(),
            path=[Point(9, 9), Point(10, 9)],
            insulated=False,
            side="top",
            color=None,
        )
        nm = {w1.id: uuid.uuid4(), w2.id: uuid.uuid4()}
        state = WireRange(w1.id, 2, w2.id, 1)  # disconnected nets → excise None
        result = build_delete_range_edit([w1, w2], nm, state)
        assert result is None


# -- add_factory injection (Stage 2: net-aware fresh-wire adds) ----------------
#
# The default keeps the purity contract (plain AddWireCommand); a caller can
# inject any factory to reclassify the freshly-minted surviving/replacement
# wires (the tool injects one running resolve_wire_nets — proven here with a
# marker command so the seam is covered without a live session).


class _MarkerAdd:
    """Stand-in for a net-aware add command — proves add_factory is honoured."""

    def __init__(self, wire: Wire) -> None:
        self.wire = wire


class TestAddFactoryInjection:
    def test_default_mints_plain_add_wire(self) -> None:
        wire = _wire((0, 0), (1, 0), (2, 0), (3, 0), (4, 0))
        nm = _net_map(wire)
        state = WireEditState(
            start_wire_id=wire.id, start_idx=1, end_wire_id=wire.id, end_idx=3
        )
        cmd = build_delete_command([wire], nm, state)  # no add_factory
        assert isinstance(cmd, MacroCommand)
        assert all(
            isinstance(c, (AddWireCommand, RemoveWireCommand)) for c in cmd._commands
        )

    def test_injected_factory_replaces_the_adds(self) -> None:
        wire = _wire((0, 0), (1, 0), (2, 0), (3, 0), (4, 0))
        nm = _net_map(wire)
        state = WireEditState(
            start_wire_id=wire.id, start_idx=1, end_wire_id=wire.id, end_idx=3
        )
        cmd = build_delete_command(
            [wire],
            nm,
            state,
            cast("object", _MarkerAdd),  # type: ignore[arg-type]
        )
        assert isinstance(cmd, MacroCommand)
        adds = [c for c in cmd._commands if isinstance(c, _MarkerAdd)]
        assert len(adds) == 2  # both surviving halves went through the factory
        assert not any(isinstance(c, AddWireCommand) for c in cmd._commands)

    def test_cross_wire_excise_honours_factory(self) -> None:
        wire_a = _wire((2.5, 9.5), (3.5, 10.5), (4.5, 11.5), (5.5, 12.5))
        wire_b = _wire((1.5, 10.5), (2.5, 9.5))
        nm = _net_map(wire_a, wire_b)
        state = WireEditState(
            start_wire_id=wire_a.id,
            start_idx=2,
            end_wire_id=wire_b.id,
            end_idx=0,
        )
        result = excise_cross_wire_range(
            [wire_a, wire_b],
            nm,
            state,
            cast("object", _MarkerAdd),  # type: ignore[arg-type]
        )
        assert result is not None
        macro, _s, _e = result
        assert isinstance(macro, MacroCommand)
        assert any(isinstance(c, _MarkerAdd) for c in macro._commands)
        assert not any(isinstance(c, AddWireCommand) for c in macro._commands)


# -- WireEditState ------------------------------------------------------------


class TestWireEditState:
    def test_is_cross(self) -> None:
        a, b = uuid.uuid4(), uuid.uuid4()
        assert WireEditState(a, 0, b, 1).is_cross is True
        assert WireEditState(a, 0, a, 2).is_cross is False

    def test_with_waypoints_is_immutable_copy(self) -> None:
        a = uuid.uuid4()
        s0 = WireEditState(a, 0, a, 2)
        s1 = s0.with_waypoints([Point(1, 1)])
        assert s0.waypoints == ()
        assert s1.waypoints == (Point(1, 1),)
        assert s1.start_idx == 0 and s1.end_idx == 2


class TestWireRange:
    """The unified range abstraction — whole wire is the full-span special case."""

    def test_alias_is_identity(self) -> None:
        from protoboard.controller.selection.wire_edit import WireRange

        assert WireEditState is WireRange

    def test_full_span_covers_whole_path(self) -> None:
        from protoboard.controller.selection.wire_edit import WireRange

        w = _wire((0, 0), (1, 0), (2, 0), (3, 0))
        r = WireRange.full_span(w)
        assert (r.start_wire_id, r.start_idx) == (w.id, 0)
        assert (r.end_wire_id, r.end_idx) == (w.id, len(w.path) - 1)
        assert not r.is_cross
        assert r.is_full_span(w)

    def test_sub_range_is_not_full_span(self) -> None:
        from protoboard.controller.selection.wire_edit import WireRange

        w = _wire((0, 0), (1, 0), (2, 0), (3, 0))
        assert not WireRange(w.id, 1, w.id, 2).is_full_span(w)

    def test_cross_range_is_never_full_span(self) -> None:
        from protoboard.controller.selection.wire_edit import WireRange

        a = _wire((0, 0), (1, 0))
        b = _wire((1, 0), (2, 0))
        r = WireRange(a.id, 0, b.id, 1)
        assert r.is_cross
        assert not r.is_full_span(a)


def _session():
    from protoboard.gui.session import EditorSession
    from protoboard.services.library.loader import load_library

    library = load_library([_LIBRARY_DIR])
    return EditorSession(board=_perfboard(), library=library)


def _perfboard(rows: int = 32, cols: int = 32) -> Board:
    grid = BoardGrid(
        width_policy=SizePolicy.HUG,
        height_policy=SizePolicy.HUG,
        cell=PadAtom(
            width_policy=SizePolicy.HUG,
            height_policy=SizePolicy.HUG,
            layers=PadLayers(top=True, bottom=True, plated_through=True),
        ),
        rows=rows,
        cols=cols,
        row_gap=0.0,
        col_gap=0.0,
    )
    return Board(root=grid)


# -- build_flip_fragment_command (Stage 0 golden: whole-wire vs fragment) ------
#
# Characterises the CURRENT behaviour of the fragment-flip builder so the
# WireRange unification (plan Stage 4) can prove it unchanged. Also pins the
# fact that a *cross-wire* selection returns None (the tool then shows a status
# message — it does NOT silently widen to a whole-wire move), correcting the
# earlier docs/core-wire-semantics.md §6.1 claim.


class TestBuildFlipFragmentCommand:
    def test_cross_wire_returns_none(self) -> None:
        a = _wire((1.5, 1.5), (2.5, 1.5))
        b = _wire((2.5, 1.5), (3.5, 1.5))
        state = WireEditState(
            start_wire_id=a.id, start_idx=0, end_wire_id=b.id, end_idx=1
        )
        assert state.is_cross
        assert build_flip_fragment_command([a, b], state) is None

    def test_degenerate_range_returns_none(self) -> None:
        w = _wire((1.5, 1.5), (2.5, 1.5), (3.5, 1.5))
        state = WireEditState(
            start_wire_id=w.id, start_idx=1, end_wire_id=w.id, end_idx=1
        )
        assert build_flip_fragment_command([w], state) is None

    def test_missing_wire_returns_none(self) -> None:
        w = _wire((1.5, 1.5), (2.5, 1.5))
        state = WireEditState(
            start_wire_id=uuid.uuid4(),
            start_idx=0,
            end_wire_id=uuid.uuid4(),
            end_idx=1,
        )
        assert build_flip_fragment_command([w], state) is None

    def test_full_span_flips_whole_wire(self) -> None:
        # range == [0 : len(path)-1] is the whole-wire special case: the builder
        # emits a FlipWireFragmentCommand spanning the entire path.
        w = _wire((1.5, 1.5), (2.5, 1.5), (3.5, 1.5))  # side="bottom"
        state = WireEditState(
            start_wire_id=w.id,
            start_idx=0,
            end_wire_id=w.id,
            end_idx=len(w.path) - 1,
        )
        cmd = build_flip_fragment_command([w], state)
        assert isinstance(cmd, FlipWireFragmentCommand)
        assert cmd.wire_id == w.id
        assert (cmd.start_idx, cmd.end_idx) == (0, len(w.path) - 1)
        assert cmd.target_side == "top"  # opposite of bottom

    def test_target_side_override(self) -> None:
        w = _wire((1.5, 1.5), (2.5, 1.5))
        state = WireEditState(
            start_wire_id=w.id, start_idx=0, end_wire_id=w.id, end_idx=1
        )
        cmd = build_flip_fragment_command([w], state, target_side="bottom")
        assert isinstance(cmd, FlipWireFragmentCommand)
        assert cmd.target_side == "bottom"


class TestSplitPreservesFragmentSides:
    """Splitting a mixed-side wire must keep each segment's live side.

    Regression: after a fragment flip ([F]) produced ``[top, bottom, top]``,
    a set-insulated ([I]) on the same wire rebuilt the pieces path-style with
    the scalar ``wire.side`` (the construction-time snapshot), flattening the
    flipped section back onto one layer.
    """

    def _mixed_wire(self) -> Wire:
        from protoboard.domain.layout.wire import Fragment

        # 5 points / 4 segments, sides: t t b t.
        return Wire(
            id=uuid.uuid4(),
            fragments=[
                Fragment(path=[Point(0, 0), Point(1, 0), Point(2, 0)], side="top"),
                Fragment(path=[Point(2, 0), Point(3, 0)], side="bottom"),
                Fragment(path=[Point(3, 0), Point(4, 0)], side="top"),
            ],
            insulated=False,
        )

    @staticmethod
    def _seg_sides(wire: Wire) -> list[str]:
        return [f.side for f in wire.fragments for _ in range(len(f.path) - 1)]

    def test_fragments_for_slice_preserves_segment_sides(self) -> None:
        from protoboard.domain.layout.wire import fragments_for_slice

        wire = self._mixed_wire()
        frags = fragments_for_slice(wire, 1, 4)  # segments 1..3: t b t
        assert [f.side for f in frags] == ["top", "bottom", "top"]
        assert frags[0].path == [Point(1, 0), Point(2, 0)]
        assert frags[1].path == [Point(2, 0), Point(3, 0)]
        assert frags[2].path == [Point(3, 0), Point(4, 0)]

    def test_set_insulated_split_keeps_mixed_sides(self) -> None:
        wire = self._mixed_wire()
        nm = {wire.id: uuid.uuid4()}
        state = WireRange(wire.id, 1, wire.id, 3)  # covers segments 1 (t), 2 (b)
        result = build_set_insulated_range_command([wire], nm, state, insulated=True)
        assert result is not None
        assert isinstance(result.command, MacroCommand)
        adds = [c for c in result.command._commands if isinstance(c, AddWireCommand)]
        assert len(adds) == 3
        rebuilt = sorted(adds, key=lambda c: c.wire.path[0].x)
        left, target, right = (c.wire for c in rebuilt)
        assert self._seg_sides(left) == ["top"]  # segment 0
        assert self._seg_sides(target) == ["top", "bottom"]  # segments 1..2
        assert self._seg_sides(right) == ["top"]  # segment 3
        assert target.insulated is True

    def test_delete_split_keeps_mixed_sides(self) -> None:
        wire = self._mixed_wire()
        nm = _net_map(wire)
        state = WireEditState(
            start_wire_id=wire.id,
            start_idx=1,
            end_wire_id=wire.id,
            end_idx=3,
        )
        cmd = build_delete_command([wire], nm, state)
        assert isinstance(cmd, MacroCommand)
        adds = [c for c in cmd._commands if isinstance(c, AddWireCommand)]
        assert len(adds) == 2
        halves = sorted(adds, key=lambda c: c.wire.path[0].x)
        assert self._seg_sides(halves[0].wire) == ["top"]  # segment 0
        assert self._seg_sides(halves[1].wire) == ["top"]  # segment 3
        # Deleting a leading range: what remains spans the bottom fragment —
        # its sides must be the LIVE per-segment sides, not wire.side.
        wire2 = self._mixed_wire()
        nm2 = _net_map(wire2)
        state2 = WireEditState(
            start_wire_id=wire2.id,
            start_idx=0,
            end_wire_id=wire2.id,
            end_idx=2,
        )
        cmd2 = build_delete_command([wire2], nm2, state2)
        assert isinstance(cmd2, MacroCommand)
        adds2 = [c for c in cmd2._commands if isinstance(c, AddWireCommand)]
        assert len(adds2) == 1
        assert self._seg_sides(adds2[0].wire) == ["bottom", "top"]  # segs 2..3


if __name__ == "__main__":  # pragma: no cover
    pytest.main([__file__, "-v"])
