# SPDX-License-Identifier: MIT
"""Tests for DragController — the headless drag / floating gesture (Phase 3).

Drives the controller directly (``update`` / ``commit`` / ``cancel`` with
``Point``s, no Qt mouse events) over a real ``EditorSession`` — which
structurally satisfies the ``DragContext`` seam — so the whole lifecycle is
exercised without synthesising QMouseEvents:

* sub-threshold press+release stays a click (``commit`` → ``False``, no move),
* a real drag translates the member by one delta and lands a
  ``MoveDragSetCommand`` on the undo stack on confirm,
* cancel restores the press-time position,
* the floating sub-state gates the commit (CLEAR auto-accepts; otherwise it
  waits for ``confirm_floating``).
"""

from __future__ import annotations

import uuid
from typing import cast

from protoboard.controller.editing.drag_set import DragSet
from protoboard.controller.selection.drag_controller import (
    DragContext,
    DragController,
    FloatingPhase,
)
from protoboard.controller.selection.mouse_input import Intent, Mods
from protoboard.domain.components.instance import ComponentInstance
from protoboard.domain.layout.drag_analysis import (
    DragAnalysis,
    DragSeverity,
)
from protoboard.geometry import Point
from protoboard.gui.board_view import BoardView
from protoboard.gui.session import EditorSession
from protoboard.gui.tools.gui_tools import PlaceTool
from tests.gui.input_helpers import pointer

_NO_MODS = Mods()


def _move(ctrl: DragController, x: float, y: float) -> None:
    ctrl.on_move(Point(x, y), _NO_MODS)


def _release(ctrl: DragController, x: float, y: float) -> None:
    ctrl.on_release(Point(x, y), _NO_MODS)


def _place(
    board_view: BoardView, session: EditorSession, x: float, y: float
) -> ComponentInstance:
    board_view.set_tool(PlaceTool("R"))
    tool = board_view.active_tool
    assert isinstance(tool, PlaceTool)
    tool.on_press(pointer(x, y))
    while tool._phase == "drag_pin":
        tool.on_press(pointer(x, y))
    return list(session.instances.values())[-1]


def _controller(
    session: EditorSession,
    comp_id: uuid.UUID,
    *,
    auto_accept_clear: bool = True,
) -> DragController:
    """Build a single-free-component DragController anchored at pin 1.

    No ``snap`` seam is wired — ``update`` translates by the raw delta, which
    keeps the geometry test-predictable.  Anchor-snap behaviour is covered by
    the SelectTool integration tests that pass the real ``view.snap``.
    """
    pl = session.layout.placements[comp_id]
    drag_set = DragSet(free_components=frozenset({comp_id}))

    def _execute(cmd: object) -> bool:
        session.stack.execute(cmd, session)  # type: ignore[arg-type]
        return True

    # ``EditorSession`` structurally satisfies the ``DragContext`` seam (its
    # ``layout`` returns the concrete ``Layout``, which Pyright won't accept as
    # the narrower ``_LayoutLike`` protocol); cast at the seam.
    return DragController(
        cast(DragContext, session),
        drag_set=drag_set,
        anchor_id=comp_id,
        anchor_pos=pl.position,
        press_pt=pl.position,
        grab_offset=(0.0, 0.0),
        execute=_execute,
        auto_accept_clear=auto_accept_clear,
    )


class TestThreshold:
    def test_subthreshold_release_is_click(
        self, board_view: BoardView, session: EditorSession
    ) -> None:
        inst = _place(board_view, session, 1.0, 1.0)
        orig = session.layout.placements[inst.id].position
        ctrl = _controller(session, inst.id)
        # A move smaller than the 0.5 start threshold then release = a click.
        _move(ctrl, orig.x + 0.2, orig.y)
        _release(ctrl, orig.x + 0.2, orig.y)
        assert ctrl.was_click is True
        assert ctrl.finished is True
        assert session.layout.placements[inst.id].position == orig

    def test_subthreshold_does_not_move(
        self, board_view: BoardView, session: EditorSession
    ) -> None:
        inst = _place(board_view, session, 1.0, 1.0)
        orig = session.layout.placements[inst.id].position
        ctrl = _controller(session, inst.id)
        _move(ctrl, orig.x + 0.1, orig.y + 0.1)
        # Below threshold → no mutation at all.
        assert session.layout.placements[inst.id].position == orig


class TestDragMove:
    def test_drag_translates_by_delta(
        self, board_view: BoardView, session: EditorSession
    ) -> None:
        inst = _place(board_view, session, 1.0, 1.0)
        orig = session.layout.placements[inst.id].position
        ctrl = _controller(session, inst.id)
        _move(ctrl, orig.x + 2.0, orig.y + 1.0)
        pos = session.layout.placements[inst.id].position
        assert (pos.x, pos.y) == (orig.x + 2.0, orig.y + 1.0)

    def test_drag_release_lands_command_on_stack(
        self, board_view: BoardView, session: EditorSession
    ) -> None:
        inst = _place(board_view, session, 1.0, 1.0)
        orig = session.layout.placements[inst.id].position
        depth_before = len(session.stack._undo)  # type: ignore[attr-defined]
        ctrl = _controller(session, inst.id)
        _move(ctrl, orig.x + 2.0, orig.y)
        _release(ctrl, orig.x + 2.0, orig.y)
        # CLEAR + auto-accept committed directly.
        assert ctrl.was_click is False
        assert ctrl.phase is FloatingPhase.COMMITTED
        assert len(session.stack._undo) == depth_before + 1  # type: ignore[attr-defined]
        # The move is undoable back to the original position.
        session.stack.undo(session)  # type: ignore[arg-type]
        assert session.layout.placements[inst.id].position == orig

    def test_idempotent_move_from_origin(
        self, board_view: BoardView, session: EditorSession
    ) -> None:
        inst = _place(board_view, session, 1.0, 1.0)
        orig = session.layout.placements[inst.id].position
        ctrl = _controller(session, inst.id)
        # Two moves: the second must translate from the ORIGIN, not compound.
        _move(ctrl, orig.x + 3.0, orig.y)
        _move(ctrl, orig.x + 1.0, orig.y)
        pos = session.layout.placements[inst.id].position
        assert (pos.x, pos.y) == (orig.x + 1.0, orig.y)


class TestCancel:
    def test_cancel_intent_restores_position(
        self, board_view: BoardView, session: EditorSession
    ) -> None:
        inst = _place(board_view, session, 1.0, 1.0)
        orig = session.layout.placements[inst.id].position
        ctrl = _controller(session, inst.id)
        _move(ctrl, orig.x + 2.0, orig.y + 2.0)
        assert ctrl.on_event(Intent.CANCEL) is True
        assert session.layout.placements[inst.id].position == orig
        assert ctrl.finished is True

    def test_cancel_leaves_stack_untouched(
        self, board_view: BoardView, session: EditorSession
    ) -> None:
        inst = _place(board_view, session, 1.0, 1.0)
        depth_before = len(session.stack._undo)  # type: ignore[attr-defined]
        orig = session.layout.placements[inst.id].position
        ctrl = _controller(session, inst.id)
        _move(ctrl, orig.x + 2.0, orig.y)
        ctrl.on_event(Intent.CANCEL)
        assert len(session.stack._undo) == depth_before  # type: ignore[attr-defined]


class TestFloating:
    def test_clear_without_auto_accept_floats(
        self, board_view: BoardView, session: EditorSession
    ) -> None:
        inst = _place(board_view, session, 1.0, 1.0)
        orig = session.layout.placements[inst.id].position
        ctrl = _controller(session, inst.id, auto_accept_clear=False)
        _move(ctrl, orig.x + 2.0, orig.y)
        _release(ctrl, orig.x + 2.0, orig.y)
        # No auto-accept → wait for the user even on a CLEAR drag.
        assert ctrl.is_floating
        assert ctrl.finished is False
        assert ctrl.on_event(Intent.CONFIRM) is True
        assert ctrl.phase is FloatingPhase.COMMITTED

    def test_floating_confirm_commits(
        self, board_view: BoardView, session: EditorSession
    ) -> None:
        inst = _place(board_view, session, 1.0, 1.0)
        orig = session.layout.placements[inst.id].position
        depth_before = len(session.stack._undo)  # type: ignore[attr-defined]
        ctrl = _controller(session, inst.id, auto_accept_clear=False)
        _move(ctrl, orig.x + 2.0, orig.y)
        _release(ctrl, orig.x + 2.0, orig.y)
        ctrl.on_event(Intent.CONFIRM)
        assert len(session.stack._undo) == depth_before + 1  # type: ignore[attr-defined]

    def test_floating_cancel_reverts(
        self, board_view: BoardView, session: EditorSession
    ) -> None:
        inst = _place(board_view, session, 1.0, 1.0)
        orig = session.layout.placements[inst.id].position
        ctrl = _controller(session, inst.id, auto_accept_clear=False)
        _move(ctrl, orig.x + 2.0, orig.y)
        _release(ctrl, orig.x + 2.0, orig.y)
        assert ctrl.is_floating
        ctrl.on_event(Intent.CANCEL)
        assert session.layout.placements[inst.id].position == orig
        assert ctrl.finished is True


def _stub_classify(
    ctrl: DragController, severity_at_x: dict[float, DragSeverity]
) -> None:
    """Make ``ctrl._classify`` return a severity that depends on the live x.

    Keyed on the dragged component's *current* position so a test can stage a
    trajectory that passes through a conflicting x and releases on a clear one —
    exercising the release-time re-classification.
    """
    comp_id = next(iter(ctrl.member_ids))

    def fake() -> DragAnalysis:
        x = ctrl._ctx.layout.placements[comp_id].position.x  # type: ignore[attr-defined]
        sev = severity_at_x.get(round(x, 3), DragSeverity.CLEAR)
        return DragAnalysis(severity=sev)

    ctrl._classify = fake  # type: ignore[method-assign]


class TestReleaseReclassifies:
    """Release acts on the *release* position, not a stale on_move decision."""

    def test_transient_conflict_does_not_block_clean_release(
        self, board_view: BoardView, session: EditorSession
    ) -> None:
        # Drag through a HARD-conflicting x (orig+1), then release on a clear
        # spot (orig+3).  The transient conflict must NOT stick: the clean
        # release commits.
        inst = _place(board_view, session, 1.0, 1.0)
        orig = session.layout.placements[inst.id].position
        ctrl = _controller(session, inst.id)
        _stub_classify(ctrl, {round(orig.x + 1.0, 3): DragSeverity.HARD})
        _move(ctrl, orig.x + 1.0, orig.y)  # through the conflict
        assert ctrl.last_decision is not None
        assert ctrl.last_decision.severity is DragSeverity.HARD
        _release(ctrl, orig.x + 3.0, orig.y)  # land clear
        assert ctrl.phase is FloatingPhase.COMMITTED
        assert session.layout.placements[inst.id].position == Point(
            orig.x + 3.0, orig.y
        )

    def test_clean_trajectory_then_conflict_release_floats(
        self, board_view: BoardView, session: EditorSession
    ) -> None:
        # Inverse: clean on the way, but the release lands on a HARD conflict —
        # it must float (be blocked), not commit on a stale CLEAR.
        inst = _place(board_view, session, 1.0, 1.0)
        orig = session.layout.placements[inst.id].position
        ctrl = _controller(session, inst.id)
        _stub_classify(ctrl, {round(orig.x + 3.0, 3): DragSeverity.HARD})
        _move(ctrl, orig.x + 1.0, orig.y)  # clean
        _release(ctrl, orig.x + 3.0, orig.y)  # release on conflict
        assert ctrl.is_floating
        assert ctrl.finished is False


class TestSoftAutoCommit:
    """MINIMUM auto-accept: SOFT commits on release; HARD stays floating."""

    def test_soft_release_auto_commits(
        self, board_view: BoardView, session: EditorSession
    ) -> None:
        inst = _place(board_view, session, 1.0, 1.0)
        orig = session.layout.placements[inst.id].position
        depth_before = len(session.stack._undo)  # type: ignore[attr-defined]
        ctrl = _controller(session, inst.id)
        _stub_classify(ctrl, {round(orig.x + 2.0, 3): DragSeverity.SOFT})
        _move(ctrl, orig.x + 2.0, orig.y)
        _release(ctrl, orig.x + 2.0, orig.y)
        # SOFT is committable → auto-accept applies it straight away.
        assert ctrl.phase is FloatingPhase.COMMITTED
        assert len(session.stack._undo) == depth_before + 1  # type: ignore[attr-defined]

    def test_hard_release_stays_floating(
        self, board_view: BoardView, session: EditorSession
    ) -> None:
        inst = _place(board_view, session, 1.0, 1.0)
        orig = session.layout.placements[inst.id].position
        ctrl = _controller(session, inst.id)
        _stub_classify(ctrl, {round(orig.x + 2.0, 3): DragSeverity.HARD})
        _move(ctrl, orig.x + 2.0, orig.y)
        _release(ctrl, orig.x + 2.0, orig.y)
        # HARD cannot be accepted → held floating, awaiting move/cancel.
        assert ctrl.is_floating
        assert ctrl.finished is False
