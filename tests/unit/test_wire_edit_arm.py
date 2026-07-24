# SPDX-License-Identifier: MIT
"""Tests for :class:`WireEditArm` — the Qt-free command-building core.

The arm is a thin, stateful dispatcher over the already-Qt-free range builders
in ``controller/selection/wire_edit.py``.  Its ONLY held state is the injected
``add_factory``; per-op geometry (layout / wires / range / graph / endpoints) is
passed at call time exactly as the underlying builders require.  Each method
returns a ``Command | None`` and does NOT execute / clear / refresh — the tool
owns commit.  These tests prove parity with the direct builder calls and that
the injected factory is honoured.

Fixture patterns mirror ``tests/unit/test_wire_edit_helpers.py``.
"""

from __future__ import annotations

import uuid
from collections.abc import Sequence
from pathlib import Path
from typing import cast

import pytest

from protoboard.controller.editing.wire_edit_arm import WireEditArm
from protoboard.controller.selection.wire_edit import (
    WireEditState,
    WireRange,
    build_simplify_range_command,
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
)
from protoboard.domain.layout.wire import Wire
from protoboard.geometry import Point

_LIBRARY_DIR = Path(__file__).parent.parent / "fixtures" / "library"


def _wire(
    *pts: tuple[float, float], insulated: bool = False, side: str = "bottom"
) -> Wire:
    return Wire(
        id=uuid.uuid4(),
        path=[Point(x, y) for x, y in pts],
        insulated=insulated,
        side=side,
    )


def _net_map(*wires: Wire) -> dict[uuid.UUID, uuid.UUID]:
    net_id = uuid.uuid4()
    return {w.id: net_id for w in wires}


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


def _session():  # type: ignore[no-untyped-def]
    from protoboard.gui.session import EditorSession
    from protoboard.services.library.loader import load_library

    library = load_library([_LIBRARY_DIR])
    return EditorSession(board=_perfboard(), library=library)


# -- directive acceptance test: simplify == the whole-wire builder ------------


def test_simplify_full_span_equals_whole_wire() -> None:
    """``WireEditArm().simplify(...)`` on a full-span range yields a command
    equivalent to calling ``build_simplify_range_command(...)`` directly — the
    unit-level proof that the arm's simplify == the builder."""
    from protoboard.controller.selection.wire_ops import build_simplify_inputs
    from protoboard.services.routing.postprocess import SimplifyMode

    sess = _session()
    wire = _wire((0, 0), (1, 0), (2, 0), (3, 0), insulated=True)
    sess.layout.wires.append(wire)
    wires = cast(Sequence[Wire], sess.layout.wires)
    rng = WireRange.full_span(wire)
    inputs = build_simplify_inputs(sess)

    direct = build_simplify_range_command(
        sess.layout, wires, rng, inputs, SimplifyMode.CHEBYSHEV
    )
    via_arm = WireEditArm().simplify(
        sess.layout, wires, rng, inputs, SimplifyMode.CHEBYSHEV
    )

    assert isinstance(direct, ReplaceWireFragmentsCommand)
    assert isinstance(via_arm, ReplaceWireFragmentsCommand)
    assert via_arm.wire_id == direct.wire_id == wire.id
    direct_flat = [p for f in direct.new_fragments for p in f.path]
    arm_flat = [p for f in via_arm.new_fragments for p in f.path]
    assert arm_flat == direct_flat
    assert arm_flat[0] == Point(0, 0) and arm_flat[-1] == Point(3, 0)


def test_delete_full_span_collapses_to_remove_wire() -> None:
    wire = _wire((0, 0), (1, 0), (2, 0))
    nm = _net_map(wire)
    cmd = WireEditArm().delete([wire], nm, WireRange.full_span(wire))
    assert isinstance(cmd, RemoveWireCommand)
    assert cmd.wire_id == wire.id


def test_flip_builds_flip_fragment_command() -> None:
    wire = _wire((1.5, 1.5), (2.5, 1.5), (3.5, 1.5))
    state = WireEditState(
        start_wire_id=wire.id, start_idx=0, end_wire_id=wire.id, end_idx=2
    )
    cmd = WireEditArm().flip([wire], state)
    assert isinstance(cmd, FlipWireFragmentCommand)
    assert cmd.wire_id == wire.id
    assert (cmd.start_idx, cmd.end_idx) == (0, 2)
    assert cmd.target_side == "top"  # opposite of bottom


# -- add_factory injection ----------------------------------------------------


class _MarkerAdd:
    """Stand-in for a net-aware add command — proves add_factory is honoured."""

    def __init__(self, wire: Wire) -> None:
        self.wire = wire


def test_default_arm_mints_plain_add_wire() -> None:
    """A mint-a-wire op via the default arm produces ``AddWireCommand``s."""
    wire = _wire((0, 0), (1, 0), (2, 0), (3, 0), (4, 0))
    nm = _net_map(wire)
    state = WireEditState(
        start_wire_id=wire.id, start_idx=1, end_wire_id=wire.id, end_idx=3
    )
    cmd = WireEditArm().delete([wire], nm, state)  # cut interior → two halves
    assert isinstance(cmd, MacroCommand)
    adds = [c for c in cmd._commands if isinstance(c, AddWireCommand)]
    assert len(adds) == 2
    assert not any(isinstance(c, _MarkerAdd) for c in cmd._commands)


def test_injected_factory_is_used_for_mint_a_wire_op() -> None:
    """A custom-factory arm routes every freshly-minted surviving wire through
    the injected factory — proving the arm's one piece of state is honoured."""
    wire = _wire((0, 0), (1, 0), (2, 0), (3, 0), (4, 0))
    nm = _net_map(wire)
    state = WireEditState(
        start_wire_id=wire.id, start_idx=1, end_wire_id=wire.id, end_idx=3
    )
    arm = WireEditArm(add_factory=cast("object", _MarkerAdd))  # type: ignore[arg-type]
    cmd = arm.delete([wire], nm, state)
    assert isinstance(cmd, MacroCommand)
    markers = [c for c in cmd._commands if isinstance(c, _MarkerAdd)]
    assert len(markers) == 2  # both surviving halves went through the factory
    assert not any(isinstance(c, AddWireCommand) for c in cmd._commands)


def test_injected_factory_used_by_cross_wire_reroute() -> None:
    """Cross-wire reroute mints a replacement wire — it must go through the
    injected factory too."""
    sess = _session()
    wire_a = _wire((2.5, 9.5), (3.5, 10.5), (4.5, 11.5), (5.5, 12.5))
    wire_b = _wire((1.5, 10.5), (2.5, 9.5))
    sess.layout.wires.extend([wire_a, wire_b])
    wires = cast(Sequence[Wire], sess.layout.wires)
    nm = _net_map(wire_a, wire_b)
    state = WireEditState(
        start_wire_id=wire_a.id, start_idx=2,
        end_wire_id=wire_b.id, end_idx=0,
        waypoints=(Point(3.0, 9.0),),
    )
    arm = WireEditArm(add_factory=cast("object", _MarkerAdd))  # type: ignore[arg-type]
    cmd = arm.reroute(sess.layout, wires, nm, state)
    assert isinstance(cmd, MacroCommand)
    assert any(isinstance(c, _MarkerAdd) for c in cmd._commands)
    assert not any(isinstance(c, AddWireCommand) for c in cmd._commands)


# -- reroute / autoroute parity ----------------------------------------------


def test_reroute_same_wire_replaces_fragments() -> None:
    sess = _session()
    wire = _wire((0, 0), (1, 0), (2, 0), (3, 0))
    sess.layout.wires.append(wire)
    wires = cast(Sequence[Wire], sess.layout.wires)
    nm = _net_map(wire)
    state = WireEditState(
        start_wire_id=wire.id, start_idx=1, end_wire_id=wire.id, end_idx=2,
        waypoints=(Point(1.5, 1.0),),
    )
    cmd = WireEditArm().reroute(sess.layout, wires, nm, state)
    assert isinstance(cmd, ReplaceWireFragmentsCommand)
    assert cmd.wire_id == wire.id
    new_pts = [p for f in cmd.new_fragments for p in f.path]
    assert Point(1.5, 1.0) in new_pts


def test_autoroute_returns_command_or_none() -> None:
    """The arm's autoroute runs A* then feeds the interior waypoints into the
    reroute builder, mirroring ``_autoroute_fragment``.  On a real (empty-layout)
    session graph, A* between two adjacent hole centres yields a route → a
    ``ReplaceWireFragmentsCommand`` (same-wire); ``None`` would mean no path."""
    sess = _session()
    wire = _wire((1.5, 1.5), (2.5, 1.5))
    sess.layout.wires.append(wire)
    wires = cast(Sequence[Wire], sess.layout.wires)
    nm = _net_map(wire)
    state = WireEditState(
        start_wire_id=wire.id, start_idx=0, end_wire_id=wire.id, end_idx=1
    )
    graph = sess.connectivity_graph()
    cmd = WireEditArm().autoroute(
        sess.layout, wires, nm, state, graph,
        Point(1.5, 1.5), Point(2.5, 1.5), "bottom",
    )
    assert cmd is None or isinstance(cmd, (ReplaceWireFragmentsCommand, MacroCommand))


# -- set_insulated / delete_range (WireRangeEdit) -----------------------------


def test_arm_set_insulated_sub_range_reports_survivors():
    wire = Wire(id=uuid.uuid4(),
                path=[Point(0, 0), Point(1, 0), Point(2, 0), Point(3, 0), Point(4, 0)],
                insulated=False, side="top", color=None)
    nm = {wire.id: uuid.uuid4()}
    state = WireRange(wire.id, 1, wire.id, 3)
    result = WireEditArm().set_insulated([wire], nm, state, insulated=True)
    assert result is not None and result.was_cut is True
    assert len(result.survivor_wire_ids) == 2


def test_arm_delete_range_full_span_no_survivors():
    wire = Wire(id=uuid.uuid4(),
                path=[Point(0, 0), Point(1, 0), Point(2, 0)],
                insulated=False, side="top", color=None)
    nm = {wire.id: uuid.uuid4()}
    result = WireEditArm().delete_range([wire], nm, WireRange.full_span(wire))
    assert result is not None and result.survivor_wire_ids == ()


if __name__ == "__main__":  # pragma: no cover
    pytest.main([__file__, "-v"])
