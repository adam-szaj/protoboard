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
        insulated=False, side="top",
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


# --------------------------------------------------------------------------- #
# Task 5: [I] set-insulated fragment command (toggle)
# --------------------------------------------------------------------------- #


def test_set_insulated_sub_range_toggles_and_waits(
    wire_edit_tool_on_5pt_wire: WireEditTool,
) -> None:
    tool = wire_edit_tool_on_5pt_wire  # bare wire
    _arm_sub_range(tool, 1, 3)
    tool._set_insulated_fragment()
    # wire was bare -> toggled to insulated on the middle; a cut happened
    assert tool._awaiting_repick is True
    wires = list(tool._session.layout.wires)
    assert len(wires) == 3
    assert any(w.insulated for w in wires)     # the insulated middle
    assert any(not w.insulated for w in wires) # the bare survivors


def test_set_insulated_full_span_stays_armed(
    wire_edit_tool_on_5pt_wire: WireEditTool,
) -> None:
    tool = wire_edit_tool_on_5pt_wire  # armed full-span, bare
    tool._set_insulated_fragment()
    assert tool._awaiting_repick is False
    assert tool._wire_edit is not None          # re-armed full span
    assert list(tool._session.layout.wires)[0].insulated is True


def test_set_insulated_action_id_in_command_table(
    wire_edit_tool_on_5pt_wire: WireEditTool,
) -> None:
    tool = wire_edit_tool_on_5pt_wire
    assert "select-tool.action.set_insulated_fragment" in tool.command_table()
    assert "select-tool.action.set_insulated_fragment" in tool.displayable_action_ids()


# --------------------------------------------------------------------------- #
# Fold-in: a failed/zero-length re-pick during the wait re-arms the pick
# --------------------------------------------------------------------------- #


def test_failed_repick_rearms_pick(
    wire_edit_tool_on_5pt_wire: WireEditTool,
) -> None:
    tool = wire_edit_tool_on_5pt_wire
    _arm_sub_range(tool, 1, 3)
    tool._set_insulated_fragment()
    assert tool._awaiting_repick is True
    assert tool.has_pending_op() is True  # the re-pick armed by _arm_repick

    # Simulate a failed re-pick: only one vertex resolved (the <2 case).
    tool._range_repicked([])
    assert tool._awaiting_repick is True
    assert tool.has_pending_op() is True  # re-armed, not stuck
