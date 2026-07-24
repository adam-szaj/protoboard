# SPDX-License-Identifier: MIT
"""Wire-edit set-insulated + unified wait-for-re-pick tool behaviour.

Drives the extracted :class:`WireEditTool` headlessly (harness copied from
``tests/gui/test_wire_edit_tool.py``).  Task 4 wires ``delete`` onto the shared
``_apply_range_edit`` decision path: a sub-range cut now DISARMS the fragment
and waits for a fresh re-pick (``_awaiting_repick``) instead of exiting, while a
full-span delete (whole wire gone, no survivors) still exits.
"""
from __future__ import annotations

import pytest

from protoboard.controller.selection.wire_edit import WireRange
from protoboard.domain.layout.wire import Wire
from protoboard.geometry import Point
from protoboard.gui.session import EditorSession
from protoboard.gui.tools.gui_tools import SelectTool, WireEditTool
from tests.fakes.board_view import HeadlessBoardView


# --------------------------------------------------------------------------- #
# Helpers (copied from tests/gui/test_wire_edit_tool.py)
# --------------------------------------------------------------------------- #


def _detour_wire(session: EditorSession) -> Wire:
    # A 5-point orthogonal wire routed through the controller so it carries real
    # endpoint knots.  A sub-range delete of idx 1..3 leaves two surviving
    # halves; a full-span delete removes the whole wire.
    wire_id = session.edit_controller.add_wire(
        [Point(1.5, 1.5), Point(2.5, 1.5), Point(3.5, 1.5),
         Point(3.5, 2.5), Point(3.5, 3.5)],
        insulated=True, side="top",
    )
    assert wire_id is not None
    wire = session.layout.wires.get(wire_id)
    assert wire is not None
    return wire


def _arm(view: HeadlessBoardView, wire: Wire) -> WireEditTool:
    """Set a plain SelectTool as the return target, then arm a full-span
    WireEditTool on *wire* through the ToolHost (so activate() runs)."""
    select = SelectTool()
    view.tools.set_tool(select)
    tool = WireEditTool(
        WireRange.full_span(wire),
        wire.id,
        return_to=lambda: view.tools.set_tool(select),
    )
    view.tools.set_tool(tool)
    return tool


def _arm_sub_range(tool: WireEditTool, start_idx: int, end_idx: int) -> None:
    """Overwrite the armed range with a middle sub-range on the same wire.

    The ops read ``self._wire_edit`` directly, so assigning it here is the
    Qt-free equivalent of a two-vertex pick landing on this sub-range.
    """
    wire_id = tool._wire_edit_wire_id
    assert wire_id is not None
    tool._wire_edit = WireRange(wire_id, start_idx, wire_id, end_idx)


@pytest.fixture
def wire_edit_tool_on_5pt_wire(session: EditorSession) -> WireEditTool:
    """A WireEditTool armed full-span on a 5-point wire, ready for a sub-range."""
    view = HeadlessBoardView(session)
    wire = _detour_wire(session)
    return _arm(view, wire)


# --------------------------------------------------------------------------- #
# Sub-range delete now waits for a re-pick (was _exit before)
# --------------------------------------------------------------------------- #


def test_sub_range_delete_now_waits_for_repick(
    wire_edit_tool_on_5pt_wire: WireEditTool,
) -> None:
    tool = wire_edit_tool_on_5pt_wire  # armed on a full-span 5-pt wire
    _arm_sub_range(tool, 1, 3)
    tool._delete_fragment()
    assert tool._awaiting_repick is True     # was _exit() before — now waits
    assert tool._wire_edit is None
    # anchor reassigned to a surviving wire that still exists
    assert tool._wire_edit_wire_id is not None
    assert tool._wire_by_id(tool._wire_edit_wire_id) is not None


def test_full_span_delete_still_exits(
    wire_edit_tool_on_5pt_wire: WireEditTool,
) -> None:
    tool = wire_edit_tool_on_5pt_wire  # armed full-span
    exited = {"v": False}
    tool._return_to = lambda: exited.__setitem__("v", True)
    tool._delete_fragment()
    assert exited["v"] is True
    assert tool._awaiting_repick is False
