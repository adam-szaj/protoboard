# SPDX-License-Identifier: MIT
"""Regression tests for cross-wire fragment excise.

Reproduces the bug discovered in user_tangled_04 where the algorithm
trimmed the *outer* end of the start wire instead of the inner one,
because it always treated ``path[0]`` as the outer endpoint regardless
of which side of the wire was actually shared with the next wire in the
chain.
"""

from __future__ import annotations

import uuid
from pathlib import Path

import pytest
from pytestqt.qtbot import QtBot

from protoboard.domain.board.board import Board
from protoboard.domain.board.boxes import BoardGrid, PadAtom, SizePolicy
from protoboard.domain.board.pads import PadLayers
from protoboard.domain.commands.component_cmds import (
    RemoveWireCommand,
    ReplaceWireFragmentsCommand,
)
from protoboard.domain.layout.wire import Wire
from protoboard.domain.schematic.net import Net, PinRef
from protoboard.geometry import Point
from protoboard.gui.board_view import BoardView
from protoboard.gui.session import EditorSession
from protoboard.gui.tools.select_tool import SelectTool
from protoboard.services.library.loader import load_library


_LIBRARY_DIR = Path(__file__).parent.parent / "fixtures" / "library"


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


@pytest.fixture()
def chain_session(qtbot: QtBot) -> EditorSession:
    """Session with two wires joined at (2.5, 9.5), like the user_tangled_04 case.

    wireA: (2.5,9.5) → … → (15.5,11.5)   — outer end on the right
    wireB: (1.5,10.5) → (2.5,9.5)        — outer end on the left
    Both share the point (2.5, 9.5).
    """
    library = load_library([_LIBRARY_DIR])
    sess = EditorSession(board=_perfboard(), library=library)
    wireA = Wire(
        id=uuid.uuid4(),
        path=[
            Point(2.5, 9.5),
            Point(3.5, 10.5),
            Point(4.5, 11.5),
            Point(5.5, 12.5),
            Point(6.5, 12.5),
            Point(7.5, 11.5),
            Point(8.5, 11.5),
        ],
        insulated=False,
        side="bottom",
    )
    wireB = Wire(
        id=uuid.uuid4(),
        path=[Point(1.5, 10.5), Point(2.5, 9.5)],
        insulated=False,
        side="bottom",
    )
    sess.layout.wires.extend([wireA, wireB])
    # Single net so _net_wires_ordered finds both.
    net_id = uuid.uuid4()
    sess.schematic.nets[net_id] = Net(
        id=net_id,
        name="N1",
        pins={PinRef(component_id=uuid.uuid4(), pin_number=1)},
    )
    return sess


def _excise(
    sess: EditorSession,
    start_wire: Wire,
    start_idx: int,
    end_wire: Wire,
    end_idx: int,
):
    """Drive the cross-wire excise path on a SelectTool bound to *sess*."""
    view = BoardView(sess)
    tool = SelectTool()
    view.set_tool(tool)
    tool._edit_start_wire_id = start_wire.id
    tool._edit_start_idx = start_idx
    tool._edit_end_wire_id = end_wire.id
    tool._edit_end_idx = end_idx
    result = tool._excise_cross_wire_range()
    return result, tool


class TestOuterEndpointIdx:
    def test_outer_is_first_when_last_is_shared(self) -> None:
        a = Wire(
            id=uuid.uuid4(),
            path=[Point(1.5, 10.5), Point(2.5, 9.5)],
            insulated=False, side="bottom",
        )
        b = Wire(
            id=uuid.uuid4(),
            path=[Point(2.5, 9.5), Point(3.5, 10.5), Point(4.5, 11.5)],
            insulated=False, side="bottom",
        )
        # a's last == b's first → a's outer end is index 0.
        assert SelectTool._outer_endpoint_idx(a, b) == 0

    def test_outer_is_last_when_first_is_shared(self) -> None:
        a = Wire(
            id=uuid.uuid4(),
            path=[Point(2.5, 9.5), Point(3.5, 10.5), Point(4.5, 11.5)],
            insulated=False, side="bottom",
        )
        b = Wire(
            id=uuid.uuid4(),
            path=[Point(1.5, 10.5), Point(2.5, 9.5)],
            insulated=False, side="bottom",
        )
        # a's first == b's last → a's outer end is index len-1.
        assert SelectTool._outer_endpoint_idx(a, b) == len(a.path) - 1

    def test_no_shared_falls_back_to_zero(self) -> None:
        a = Wire(
            id=uuid.uuid4(),
            path=[Point(0, 0), Point(1, 0)],
            insulated=False, side="bottom",
        )
        b = Wire(
            id=uuid.uuid4(),
            path=[Point(10, 10), Point(11, 10)],
            insulated=False, side="bottom",
        )
        assert SelectTool._outer_endpoint_idx(a, b) == 0


class TestExciseCrossWire:
    def test_excise_keeps_outer_suffix_of_start_wire(
        self, chain_session: EditorSession
    ) -> None:
        """The user_tangled_04 scenario.

        wireA shares its *first* point with wireB; excise edit_idx=3 on
        wireA must keep the *suffix* [3..end] (containing the outer end),
        not the prefix [0..3] which the buggy version produced.
        """
        wireA, wireB = chain_session.layout.wires
        result, _tool = _excise(chain_session, wireA, 3, wireB, 0)
        assert result is not None
        macro, _start, _end = result

        # WireA inner endpoint moves to path[3]; we replace it with a fresh
        # wire (Remove + Add) so endpoint knots get re-resolved.  Find both
        # commands and verify the added wire's path is the kept suffix.
        from protoboard.domain.commands.component_cmds import AddWireCommand
        assert any(
            isinstance(c, RemoveWireCommand) and c.wire_id == wireA.id
            for c in macro._commands
        )
        added_a = next(
            c for c in macro._commands
            if isinstance(c, AddWireCommand)
            and c.wire.path[0] == wireA.path[3]
        )
        assert list(added_a.wire.path) == list(wireA.path[3:])
        # And wireB, whose outer end is path[0] and edit is at path[0],
        # collapses to nothing → removed entirely.
        assert any(
            isinstance(c, RemoveWireCommand) and c.wire_id == wireB.id
            for c in macro._commands
        )

    def test_excise_drops_outer_end_when_edit_at_outer(
        self, chain_session: EditorSession
    ) -> None:
        """Editing exactly at the outer endpoint of a wire removes that wire."""
        wireA, wireB = chain_session.layout.wires
        # On wireB, outer endpoint is path[0] (=(1.5,10.5)); edit there.
        # On wireA, outer endpoint is path[-1]; edit at index 3 (inner).
        result, _tool = _excise(chain_session, wireA, 3, wireB, 0)
        assert result is not None
        macro, _, _ = result
        # wireB collapsed → RemoveWireCommand.
        assert any(
            isinstance(c, RemoveWireCommand) and c.wire_id == wireB.id
            for c in macro._commands
        )

    def test_excise_on_swapped_start_end(
        self, chain_session: EditorSession
    ) -> None:
        """Symmetry: swap which wire is start vs end and verify both branches."""
        wireA, wireB = chain_session.layout.wires
        # start=wireB at idx 0 (outer), end=wireA at idx 3 (inner).
        result, _tool = _excise(chain_session, wireB, 0, wireA, 3)
        assert result is not None
        macro, _, _ = result
        # wireB outer end is path[0]; edit_idx == outer_idx → remove.
        assert any(
            isinstance(c, RemoveWireCommand) and c.wire_id == wireB.id
            for c in macro._commands
        )
        # wireA outer end is path[-1]; edit at index 3 → keep suffix [3:].
        # Replaced via Remove + AddWire so endpoint knots get re-resolved.
        from protoboard.domain.commands.component_cmds import AddWireCommand
        assert any(
            isinstance(c, RemoveWireCommand) and c.wire_id == wireA.id
            for c in macro._commands
        )
        added_a = next(
            c for c in macro._commands
            if isinstance(c, AddWireCommand)
            and c.wire.path[0] == wireA.path[3]
        )
        assert list(added_a.wire.path) == list(wireA.path[3:])
