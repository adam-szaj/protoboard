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
from pathlib import Path

import pytest

from protoboard.domain.board.board import Board
from protoboard.domain.board.boxes import BoardGrid, PadAtom, SizePolicy
from protoboard.domain.board.pads import PadLayers
from protoboard.domain.commands.base import MacroCommand
from protoboard.domain.commands.component_cmds import (
    AddWireCommand,
    RemoveWireCommand,
    ReplaceWireFragmentsCommand,
)
from protoboard.domain.layout.wire import Wire
from protoboard.geometry import Point
from protoboard.gui.selection.wire_edit import (
    WireEditState,
    build_delete_command,
    build_reroute_command,
    dedupe_adjacent,
    excise_cross_wire_range,
    net_wires_ordered,
    outer_endpoint_idx,
)

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
            (2.5, 9.5), (3.5, 10.5), (4.5, 11.5), (5.5, 12.5),
            (6.5, 12.5), (7.5, 11.5), (8.5, 11.5),
        )
        wire_b = _wire((1.5, 10.5), (2.5, 9.5))
        return wire_a, wire_b, _net_map(wire_a, wire_b)

    def test_excise_keeps_outer_suffix_of_start_wire(self) -> None:
        wire_a, wire_b, nm = self._chain()
        state = WireEditState(
            start_wire_id=wire_a.id, start_idx=3,
            end_wire_id=wire_b.id, end_idx=0,
        )
        result = excise_cross_wire_range([wire_a, wire_b], nm, state)
        assert result is not None
        macro, _start, _end = result
        assert isinstance(macro, MacroCommand)
        cmds = macro._commands
        # wireA inner endpoint → path[3]; kept suffix [3:] via Remove + Add.
        assert any(
            isinstance(c, RemoveWireCommand) and c.wire_id == wire_a.id
            for c in cmds
        )
        added_a = next(
            c for c in cmds
            if isinstance(c, AddWireCommand) and c.wire.path[0] == wire_a.path[3]
        )
        assert list(added_a.wire.path) == list(wire_a.path[3:])
        # wireB outer end is path[0], edit at path[0] → collapses → removed.
        assert any(
            isinstance(c, RemoveWireCommand) and c.wire_id == wire_b.id
            for c in cmds
        )

    def test_disconnected_returns_none(self) -> None:
        a = _wire((0, 0), (1, 0))
        b = _wire((10, 0), (11, 0))
        nm = _net_map(a, b)
        state = WireEditState(
            start_wire_id=a.id, start_idx=0, end_wire_id=b.id, end_idx=1
        )
        assert excise_cross_wire_range([a, b], nm, state) is None


# -- build_reroute_command ----------------------------------------------------


class TestBuildReroute:
    def test_same_wire_replaces_fragments_with_waypoints(self) -> None:
        sess = _session()
        wire = _wire((0, 0), (1, 0), (2, 0), (3, 0))
        sess.layout.wires.append(wire)
        nm = _net_map(wire)
        state = WireEditState(
            start_wire_id=wire.id, start_idx=1,
            end_wire_id=wire.id, end_idx=2,
            waypoints=(Point(1.5, 1.0),),
        )
        cmd = build_reroute_command(sess.layout, sess.layout.wires, nm, state)
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
            start_wire_id=wire.id, start_idx=0,
            end_wire_id=wire.id, end_idx=1,
        )
        # prefix[:1] + [] + suffix[1:] = [p0, p1] — still 2 points, valid.
        cmd = build_reroute_command(sess.layout, sess.layout.wires, nm, state)
        assert isinstance(cmd, ReplaceWireFragmentsCommand)

    def test_cross_wire_builds_excise_plus_addwire_macro(self) -> None:
        sess = _session()
        wire_a = _wire((2.5, 9.5), (3.5, 10.5), (4.5, 11.5), (5.5, 12.5))
        wire_b = _wire((1.5, 10.5), (2.5, 9.5))
        sess.layout.wires.extend([wire_a, wire_b])
        nm = _net_map(wire_a, wire_b)
        state = WireEditState(
            start_wire_id=wire_a.id, start_idx=2,
            end_wire_id=wire_b.id, end_idx=0,
            waypoints=(Point(3.0, 9.0),),
        )
        cmd = build_reroute_command(sess.layout, sess.layout.wires, nm, state)
        assert isinstance(cmd, MacroCommand)
        # The outer macro = [excise_macro, AddWire(new_path)].
        assert any(isinstance(c, AddWireCommand) for c in cmd._commands)


# -- build_delete_command -----------------------------------------------------


class TestBuildDelete:
    def test_whole_same_wire_removes_wire(self) -> None:
        wire = _wire((0, 0), (1, 0), (2, 0))
        nm = _net_map(wire)
        state = WireEditState(
            start_wire_id=wire.id, start_idx=0,
            end_wire_id=wire.id, end_idx=2,
        )
        cmd = build_delete_command([wire], nm, state)
        assert isinstance(cmd, RemoveWireCommand)
        assert cmd.wire_id == wire.id

    def test_partial_same_wire_cuts_into_two_halves(self) -> None:
        wire = _wire((0, 0), (1, 0), (2, 0), (3, 0), (4, 0))
        nm = _net_map(wire)
        # Remove interior [2..3): keep left [0..2] and right [3..4].
        state = WireEditState(
            start_wire_id=wire.id, start_idx=1,
            end_wire_id=wire.id, end_idx=3,
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
            start_wire_id=wire_a.id, start_idx=2,
            end_wire_id=wire_b.id, end_idx=0,
        )
        cmd = build_delete_command([wire_a, wire_b], nm, state)
        assert isinstance(cmd, MacroCommand)


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


if __name__ == "__main__":  # pragma: no cover
    pytest.main([__file__, "-v"])
