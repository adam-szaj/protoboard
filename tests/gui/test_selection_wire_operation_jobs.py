# SPDX-License-Identifier: MIT
"""Selection wire jobs retain cancellation and unchanged-result interaction behavior."""

from __future__ import annotations

import uuid

import pytest

from protoboard.controller.ports.routing import UnweaveResult
from protoboard.domain.layout.wire import Wire
from protoboard.geometry import Point
from protoboard.gui.session import EditorSession
from protoboard.gui.tools.gui_tools import SelectTool
from tests.fakes.board_view import FakeExecutor, HeadlessBoardView


@pytest.mark.parametrize("operation", ["simplify", "unweave"])
def test_cancelled_wire_job_preserves_layout_and_history(
    session: EditorSession, operation: str
) -> None:
    wires = [
        Wire(id=uuid.uuid4(), path=[Point(0.5, y), Point(2.5, y)]) for y in (0.5, 1.5)
    ]
    session.layout.wires.extend(wires)
    view = HeadlessBoardView(session)
    tool = SelectTool()
    view.tools.set_tool(tool)
    assert isinstance(view.executor, FakeExecutor)
    view.executor.cancel_next = True
    wire_ids = {w.id for w in wires}

    if operation == "simplify":
        tool.simplify_wires(wire_ids)
    else:
        tool.unweave_wires(wire_ids)

    assert list(session.layout.wires) == wires
    assert not session.stack.can_undo()
    assert len(view.executor.jobs) == 1


@pytest.mark.parametrize("before_crossings", [0, 2])
def test_unchanged_unweave_result_does_not_commit(
    session: EditorSession, monkeypatch: pytest.MonkeyPatch, before_crossings: int
) -> None:
    wires = [
        Wire(id=uuid.uuid4(), path=[Point(0.5, y), Point(2.5, y)]) for y in (0.5, 1.5)
    ]
    session.layout.wires.extend(wires)
    monkeypatch.setattr(
        session.edit_controller,
        "unweave_wires",
        lambda targets: UnweaveResult(targets, before_crossings, before_crossings),
    )
    view = HeadlessBoardView(session)
    tool = SelectTool()
    view.tools.set_tool(tool)
    tool.unweave_wires({w.id for w in wires})

    assert list(session.layout.wires) == wires
    assert not session.stack.can_undo()
