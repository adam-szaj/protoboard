# SPDX-License-Identifier: MIT
"""EditController facade — active-tool ownership + command dispatch, no Qt.

``tool_commands()`` / ``dispatch_tool_command()`` are the seam a headless
driver (or the GUI's KeyMap-resolved ``on_key``) uses to run a tool's command
by *action id* — never by touching a key. Mirrors the fixture pattern of
``test_edit_controller_delete.py`` (a bare ``HeadlessSession``, no Qt).
"""

from __future__ import annotations

import uuid
from collections.abc import Callable
from pathlib import Path

from protoboard.controller.editing.item_handle import ItemId
from protoboard.controller.tools.base import ControllerTool
from protoboard.controller.tools.select_tool import SelectTool
from protoboard.domain.board.board import Board
from protoboard.domain.board.boxes import BoardGrid, PadAtom, PadLayers, SizePolicy
from protoboard.domain.components.instance import ComponentInstance
from protoboard.domain.layout.layout import ROOT_GROUP_ID
from protoboard.domain.layout.placement import ComponentPlacement
from protoboard.geometry import Point
from protoboard.headless_session import HeadlessSession
from protoboard.services.library.loader import load_library

_LIBRARY_DIR = Path(__file__).parent.parent / "fixtures" / "library"


def _perfboard(rows: int = 5, cols: int = 5) -> Board:
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


def _session() -> HeadlessSession:
    library = load_library([_LIBRARY_DIR])
    return HeadlessSession(board=_perfboard(), library=library)


def _place(session: HeadlessSession, x: float, y: float, refdes: str) -> uuid.UUID:
    """Add an "R" instance + placement at (x, y); returns its component id."""
    defn = session.components["R"]
    inst = ComponentInstance.new(
        definition_ref="R",
        refdes=refdes,
        value="",
    ).bind(defn, session.package_families)
    session.instances[inst.id] = inst
    session.layout.placements[inst.id] = ComponentPlacement(
        component_id=inst.id,
        position=Point(x, y),
        rotation=0,
        side="top",
    )
    session.invalidate_graph()
    return inst.id


class _DeleteSelectedTool(ControllerTool):
    """A minimal Qt-free tool whose one command deletes the model selection.

    Stands in for a GUI tool's ``command_table()`` entry without pulling in
    SelectTool's GUI-side ``SelectionService`` — the model-side selection
    (``EditingContext.selection()``) is all a headless-safe delete command needs.
    """

    ACTION_ID = "delete-selected-tool.action.delete"

    def command_table(self) -> dict[str, Callable[[], object]]:
        return {self.ACTION_ID: self._delete}

    def _delete(self) -> bool:
        return self._session.edit_controller.delete(active_group_id=ROOT_GROUP_ID)


def test_set_and_get_active_tool() -> None:
    session = _session()
    ctl = session.edit_controller
    tool = SelectTool()
    ctl.set_active_tool(tool)
    assert ctl.active_tool is tool


def test_set_active_tool_to_none_clears_it() -> None:
    session = _session()
    ctl = session.edit_controller
    ctl.set_active_tool(SelectTool())
    ctl.set_active_tool(None)
    assert ctl.active_tool is None


def test_tool_commands_lists_action_ids() -> None:
    session = _session()
    ctl = session.edit_controller
    ctl.set_active_tool(SelectTool())
    cmds = ctl.tool_commands()
    assert "select-tool.action.delete" in cmds


def test_tool_commands_empty_with_no_active_tool() -> None:
    session = _session()
    ctl = session.edit_controller
    assert ctl.tool_commands() == {}


def test_dispatch_tool_command_runs_the_command() -> None:
    session = _session()
    ctl = session.edit_controller
    cid = _place(session, 0.5, 0.5, "R1")
    session.select([ItemId.component(cid)])

    tool = _DeleteSelectedTool()
    tool.activate(session, view=None)  # type: ignore[arg-type]
    ctl.set_active_tool(tool)

    handled = ctl.dispatch_tool_command(_DeleteSelectedTool.ACTION_ID)

    assert handled is True
    assert cid not in session.layout.placements


def test_dispatch_tool_command_unknown_action_returns_false() -> None:
    session = _session()
    ctl = session.edit_controller
    ctl.set_active_tool(SelectTool())
    assert ctl.dispatch_tool_command("no-such-tool.action.nope") is False


def test_dispatch_tool_command_with_no_active_tool_returns_false() -> None:
    session = _session()
    ctl = session.edit_controller
    assert ctl.dispatch_tool_command("anything") is False
