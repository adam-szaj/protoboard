# SPDX-License-Identifier: MIT
"""Whole-wire flip of a mixed wire toggles per fragment (regression for cd01742 sibling bug).

Before this fix, ``select_tool._move_selected_wires_to_side(None)`` read a
stale ``wire.side`` snapshot to decide the flip target, then built a
flatten ``SetWireSideCommand`` — collapsing a mixed ``[top,bottom,top]`` wire
to all-bottom instead of toggling each fragment. The fix splits the method
into a move-only ``_move_selected_wires_to_side(side: str)`` and a new
``_flip_selected_wires()`` that delegates to ``EditController.flip_wires``
(fragment-wise toggle, never flattens).
"""  # noqa: E501

from __future__ import annotations

import uuid
from pathlib import Path

import pytest

from protoboard.controller.selection.service import SelectionService
from protoboard.controller.tools.select_tool import SelectTool
from protoboard.domain.board.board import Board
from protoboard.domain.board.boxes import BoardGrid, PadAtom, PadLayers, SizePolicy
from protoboard.domain.layout.wire import Fragment, Wire
from protoboard.geometry import Point
from protoboard.gui.session import EditorSession
from protoboard.services.library.loader import load_library

_LIBRARY_DIR = Path(__file__).parent.parent / "fixtures" / "library"


def _perfboard(rows: int = 8, cols: int = 8) -> Board:
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


def _add_mixed_wire(session: EditorSession) -> uuid.UUID:
    """Build a three-fragment [top,bottom,top] wire, appended directly.

    The public ``add_wire`` verb only builds single-side wires; a mixed wire
    is constructed directly and appended to ``session.layout.wires`` (same
    approach as ``tests/unit/test_edit_controller_wire_lock_verbs.py``'s
    ``_add_mixed_wire``, extended to three fragments to match the regression
    scenario).
    """
    wire = Wire(
        id=uuid.uuid4(),
        insulated=True,
        fragments=[
            Fragment(path=[Point(1.0, 1.0), Point(2.0, 1.0)], side="top"),
            Fragment(path=[Point(2.0, 1.0), Point(3.0, 1.0)], side="bottom"),
            Fragment(path=[Point(3.0, 1.0), Point(4.0, 1.0)], side="top"),
        ],
    )
    session.layout.wires.append(wire)
    return wire.id


@pytest.fixture
def session_with_mixed_wire() -> tuple[EditorSession, SelectTool, uuid.UUID]:
    library = load_library([_LIBRARY_DIR])
    session = EditorSession(board=_perfboard(), library=library)
    wid = _add_mixed_wire(session)

    tool = SelectTool()
    tool._session = session  # type: ignore[assignment]
    tool._selection = SelectionService()
    tool.selected_wire_ids = {wid}

    return session, tool, wid


def test_whole_wire_flip_mixed_stays_mixed(
    session_with_mixed_wire: tuple[EditorSession, SelectTool, uuid.UUID],
) -> None:
    session, tool, wid = session_with_mixed_wire
    before = [f.side for f in session.layout.wires.get_or_raise(wid).fragments]
    assert before == ["top", "bottom", "top"]  # precondition

    tool._flip_selected_wires()  # new whole-wire flip entry

    after = [f.side for f in session.layout.wires.get_or_raise(wid).fragments]
    assert after == ["bottom", "top", "bottom"]  # toggled, NOT flattened


def test_move_to_side_still_flattens(
    session_with_mixed_wire: tuple[EditorSession, SelectTool, uuid.UUID],
) -> None:
    session, tool, wid = session_with_mixed_wire

    tool._move_selected_wires_to_side("bottom")

    sides = {f.side for f in session.layout.wires.get_or_raise(wid).fragments}
    assert sides == {"bottom"}
