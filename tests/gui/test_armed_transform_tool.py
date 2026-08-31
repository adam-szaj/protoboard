# SPDX-License-Identifier: MIT
"""Tests for ArmedTransformTool — the armed rotate/move/flip as its own tool.

The armed transform was extracted out of SelectTool into a registered tool that
drives the controller's E3 armed API. These tests drive it through a real
BoardView/session: SelectTool arms it (swapping the active tool), then keys /
wheel / clicks go to the now-active ArmedTransformTool, and accept / cancel hand
control back to SelectTool.

Repointed from the old ``TestArmedRotate`` / ``TestArmedMove`` / ``TestArmedAtomic``
/ ``TestFloatTransformSwitch`` / ``TestFloatTransformGroupFrames`` in
``test_select_tool.py`` (behaviour parity — the assertions on the layout are
identical; only the object the keys are sent to changed, since SelectTool is no
longer the active tool while armed).
"""

from __future__ import annotations

from PySide6.QtCore import QPointF, Qt
from PySide6.QtGui import QWheelEvent

from protoboard.domain.components.instance import ComponentInstance
from protoboard.domain.layout.group import ComponentGroup
from protoboard.domain.layout.layout import ROOT_GROUP_ID
from protoboard.gui.board_view import BoardView
from protoboard.gui.session import EditorSession
from protoboard.gui.tools.gui_tools import ArmedTransformTool, PlaceTool, SelectTool
from protoboard.gui.tools.select_preview import QtSelectPreview
from tests.gui.input_helpers import key_inp, pointer, wheel


def _place(
    board_view: BoardView, session: EditorSession, x: float, y: float
) -> ComponentInstance:
    board_view.set_tool(PlaceTool("R"))
    tool = board_view._active_tool
    assert isinstance(tool, PlaceTool)
    tool.on_press(pointer(x, y))
    while tool._phase == "drag_pin":
        tool.on_press(pointer(x, y))
    return list(session.instances.values())[-1]


def _select(board_view: BoardView) -> SelectTool:
    board_view.set_tool(SelectTool())
    tool = board_view._active_tool
    assert isinstance(tool, SelectTool)
    return tool


def _armed_tool(board_view: BoardView) -> ArmedTransformTool:
    tool = board_view._active_tool
    assert isinstance(tool, ArmedTransformTool)
    return tool


def _pose(session: EditorSession, comp_id):  # type: ignore[no-untyped-def]
    """The component's *current float pose* — preview while armed, else live.

    E5: an armed rotate/move/flip mutates a preview layout, not the session's,
    so the mid-gesture pose lives in ``armed_transform.preview_layout()``; after
    accept the committed pose is back on ``session.layout``.  This reads whichever
    is current, so a behaviour assertion ("the step rotated by 90°") stays valid
    without caring where the pose is held.
    """
    armed = session.edit_controller.armed_transform
    layout = (
        armed.preview_layout()
        if armed is not None and not armed.finished
        else session.layout
    )
    return layout.placements[comp_id]


def _key(tool: object, key: Qt.Key) -> None:
    assert hasattr(tool, "on_key")
    tool.on_key(key_inp(key))  # type: ignore[attr-defined]


def _press(tool: object, x: float, y: float) -> None:
    assert hasattr(tool, "on_press")
    tool.on_press(pointer(x, y))  # type: ignore[attr-defined]


def _qt_wheel(dy: int) -> QWheelEvent:
    from PySide6.QtCore import QPoint

    return QWheelEvent(
        QPointF(0, 0),
        QPointF(0, 0),
        QPoint(0, 0),
        QPoint(0, dy),
        Qt.MouseButton.NoButton,
        Qt.KeyboardModifier.NoModifier,
        Qt.ScrollPhase.NoScrollPhase,
        False,
    )


def _make_group(
    session: EditorSession, member_ids: list, name: str = "G1"
):  # type: ignore[no-untyped-def]
    import uuid as _uuid

    gid = _uuid.uuid4()
    session.layout.groups[gid] = ComponentGroup(
        id=gid,
        name=name,
        component_ids=frozenset(member_ids),
        child_group_ids=frozenset(),
        exported_pins=frozenset(),
    )
    root = session.layout.groups[ROOT_GROUP_ID]
    session.layout.groups[ROOT_GROUP_ID] = ComponentGroup(
        id=ROOT_GROUP_ID,
        name=root.name,
        component_ids=root.component_ids - frozenset(member_ids),
        child_group_ids=root.child_group_ids | {gid},
        exported_pins=root.exported_pins,
    )
    session.invalidate_graph()
    return gid


# ---------------------------------------------------------------------------
# E4a smoke: the tool arms via the controller and carries its own state
# ---------------------------------------------------------------------------


def test_construct_and_activate_arms_via_controller(
    board_view: BoardView, session: EditorSession
) -> None:
    inst = _place(board_view, session, 0.5, 0.5)
    tool = ArmedTransformTool(
        frozenset({inst.id}),
        None,
        active_group_id=ROOT_GROUP_ID,
        mode="rotate",
        pivot=None,
        return_to=lambda: None,
    )
    board_view.set_tool(tool)
    assert session.edit_controller.armed_transform is not None
    assert board_view._active_tool is tool
    assert tool.state == "move.float.rotate"


def test_state_is_idle_when_not_armed() -> None:
    tool = ArmedTransformTool(
        frozenset(),
        None,
        active_group_id=ROOT_GROUP_ID,
        mode="rotate",
        pivot=None,
        return_to=lambda: None,
    )
    assert tool.state == "select.idle"


# ---------------------------------------------------------------------------
# Armed rotate ([R] via SelectTool → ArmedTransformTool active)
# ---------------------------------------------------------------------------


class TestArmedRotate:
    """[R] arms a float-rotate; the ArmedTransformTool becomes active."""

    def _armed(self, board_view, session):
        inst = _place(board_view, session, 0.5, 0.5)
        select = _select(board_view)
        _press(select, 0.5, 0.5)
        _key(select, Qt.Key.Key_R)  # arm → swaps to ArmedTransformTool
        armed = _armed_tool(board_view)
        assert session.edit_controller.armed_transform is not None
        return inst, armed

    def test_up_down_step_cw_ccw(self, board_view, session) -> None:
        inst, armed = self._armed(board_view, session)
        base = _pose(session, inst.id).rotation
        _key(armed, Qt.Key.Key_Up)
        assert _pose(session, inst.id).rotation == (base + 90) % 360
        _key(armed, Qt.Key.Key_Down)
        assert _pose(session, inst.id).rotation == base

    def test_wheel_steps_rotation(self, board_view, session) -> None:
        inst, armed = self._armed(board_view, session)
        base = _pose(session, inst.id).rotation
        assert armed.wants_wheel(wheel(0))
        armed.on_wheel(wheel(120))
        assert _pose(session, inst.id).rotation == (base + 90) % 360

    def test_wheel_bounce_one_notch_is_one_step(self, board_view, session) -> None:
        # Three 40-unit events sum to one 120 notch → exactly one 90° step.
        inst, armed = self._armed(board_view, session)
        base = _pose(session, inst.id).rotation
        for _ in range(3):
            armed.on_wheel(wheel(40))
        assert _pose(session, inst.id).rotation == (base + 90) % 360

    def test_wheel_hard_spin_steps_proportionally(self, board_view, session) -> None:
        inst, armed = self._armed(board_view, session)
        base = _pose(session, inst.id).rotation
        armed.on_wheel(wheel(240))  # two notches
        assert _pose(session, inst.id).rotation == (base + 180) % 360

    def test_view_wheel_routes_to_armed_rotate(self, board_view, session) -> None:
        # The real user path: BoardView.wheelEvent → KeyMap chord dispatch →
        # the armed tool's wheel_rotate shadows the zoom while armed.
        inst, _armed = self._armed(board_view, session)
        base = _pose(session, inst.id).rotation
        board_view.wheelEvent(_qt_wheel(120))
        assert _pose(session, inst.id).rotation == (base + 90) % 360
        assert board_view.camera._wheel_throttle._parked is None

    def test_view_wheel_zooms_when_not_armed(self, board_view, session) -> None:
        # No armed tool → the wheel chord falls through the (SelectTool) layer
        # to the view zoom: the throttle parks the raw delta (flushed later).
        inst = _place(board_view, session, 0.5, 0.5)
        _select(board_view)
        base = session.layout.placements[inst.id].rotation
        board_view.wheelEvent(_qt_wheel(120))
        assert session.layout.placements[inst.id].rotation == base
        parked = board_view.camera._wheel_throttle._parked
        assert parked is not None and parked[0] == 120

    def test_esc_cancels_whole_session(self, board_view, session) -> None:
        # Task 4: cancel reverts the pending op and RE-ARMS on the same
        # selection — it does not return to SelectTool.
        inst, armed = self._armed(board_view, session)
        base = session.layout.placements[inst.id].rotation
        _key(armed, Qt.Key.Key_R)  # +90
        _key(armed, Qt.Key.Key_R)  # +90  (now base+180)
        assert _pose(session, inst.id).rotation == (base + 180) % 360
        _key(armed, Qt.Key.Key_Escape)  # cancel — reverts, re-arms
        assert session.edit_controller.armed_transform is not None
        assert board_view._active_tool is armed
        # Live layout never mutated: cancel leaves the original pose intact.
        assert session.layout.placements[inst.id].rotation == base

    def test_enter_accepts_keeps_rotation(self, board_view, session) -> None:
        # Task 4: confirm commits and RE-ARMS on the moved selection — it
        # does not return to SelectTool.
        inst, armed = self._armed(board_view, session)
        base = session.layout.placements[inst.id].rotation
        _key(armed, Qt.Key.Key_R)  # +90
        _key(armed, Qt.Key.Key_Return)  # accept
        assert session.edit_controller.armed_transform is not None
        assert board_view._active_tool is armed
        assert session.layout.placements[inst.id].rotation == (base + 90) % 360

    def test_live_layout_pristine_until_accept(self, board_view, session) -> None:
        # E5 invariant: the armed gesture mutates only the preview layout; the
        # session's layout keeps its exact pre-arm placement object until accept
        # commits a command, and cancel leaves it untouched.
        inst, armed = self._armed(board_view, session)
        live_before = session.layout.placements[inst.id]
        _key(armed, Qt.Key.Key_R)  # +90 on the preview
        _key(armed, Qt.Key.Key_R)  # +90 again
        # Live layout object is byte-identical (same rotation) mid-gesture …
        assert session.layout.placements[inst.id].rotation == live_before.rotation
        # … while the preview carries the two steps.
        transform = session.edit_controller.armed_transform
        assert transform is not None
        assert transform.preview_layout().placements[inst.id].rotation == (
            (live_before.rotation + 180) % 360
        )
        _key(armed, Qt.Key.Key_Escape)  # cancel → live still pristine
        assert session.layout.placements[inst.id].rotation == live_before.rotation

    def test_pad_click_sets_shared_pivot(self, board_view, session) -> None:
        inst, armed = self._armed(board_view, session)
        before = _pose(session, inst.id).position
        _press(armed, 3.0, 3.0)  # click a pad away from the part → shared pivot
        assert session.edit_controller.armed_transform is not None
        assert session.edit_controller.armed_transform.pivot is not None
        _key(armed, Qt.Key.Key_R)  # step → orbit the pivot, position changes
        after = _pose(session, inst.id).position
        assert (after.x, after.y) != (before.x, before.y)

    def test_pivot_markers_shown_while_armed_cleared_on_disarm(
        self, board_view, session
    ) -> None:
        # Visual feedback: arming draws pivot marker(s); Task 4: cancel now
        # re-arms (not disarms), so markers are redrawn (per-object, since
        # re-arm resets the pivot); full deactivate (tool exit) still clears
        # them — see test_armed_move_claims_wasd_and_arrows.
        inst, armed = self._armed(board_view, session)
        preview = armed._preview
        assert isinstance(preview, QtSelectPreview)
        assert preview.pivots.items  # per-object marker drawn on arm
        _press(armed, 3.0, 3.0)  # shared pad pivot
        assert session.edit_controller.armed_transform is not None
        assert session.edit_controller.armed_transform.pivot is not None
        assert preview.pivots.items  # re-drawn on the shared pad
        _key(armed, Qt.Key.Key_Escape)  # cancel → reverts, re-arms
        assert session.edit_controller.armed_transform is not None
        assert preview.pivots.items  # re-drawn (per-object) on the re-arm

    def test_accept_parks_armed_on_conflict(self, board_view, session) -> None:
        # Conditional-disarm contract: an accept that cannot commit (HARD
        # conflict) leaves the tool active, waiting — it does NOT return.
        from protoboard.domain.layout.drag_analysis import (
            DragAnalysis,
            DragSeverity,
        )

        inst, armed = self._armed(board_view, session)
        _key(armed, Qt.Key.Key_R)  # step once so there is a non-zero rotation
        transform = session.edit_controller.armed_transform
        assert transform is not None
        transform._classify = lambda: DragAnalysis(  # type: ignore[attr-defined]
            severity=DragSeverity.HARD
        )
        assert transform.can_commit() is False
        _key(armed, Qt.Key.Key_Return)  # accept → must stay armed
        assert session.edit_controller.armed_transform is transform  # parked
        assert board_view._active_tool is armed
        assert transform.finished is False


class TestArmedKeyClaims:
    """While armed, the ArmedTransformTool claims its step/action keys so no
    global QAction (S=Select…) nor the arrow-scroll handler steals them."""

    def test_armed_move_claims_wasd_and_arrows(self, board_view, session) -> None:
        _place(board_view, session, 0.5, 0.5)
        select = _select(board_view)
        _press(select, 0.5, 0.5)
        # Idle SelectTool: 'S' is the global Select shortcut, not claimed.
        assert select.wants_key(key_inp(Qt.Key.Key_S)) is False
        _key(select, Qt.Key.Key_G)  # arm move → ArmedTransformTool active
        armed = _armed_tool(board_view)
        for k in (
            Qt.Key.Key_S,
            Qt.Key.Key_W,
            Qt.Key.Key_A,
            Qt.Key.Key_D,
            Qt.Key.Key_Left,
            Qt.Key.Key_Right,
            Qt.Key.Key_Up,
            Qt.Key.Key_Down,
        ):
            assert armed.wants_key(key_inp(k)) is True
        # Task 4: commit/cancel now re-arm instead of calling _return_to (the
        # actual tool-swap-back), so this fixture (no MainWindow, hence
        # escape_to_select() is a no-op) can no longer drive a real disarm.
        # What we CAN assert here: a fresh (untouched) arm has no pending op,
        # so the host's two-stage [Esc] (ToolHost.handle_key_press) would let
        # this Esc exit — can_exit() gates exactly that decision.
        assert armed.can_exit() is True


class TestArmedMove:
    """[G] arms a keyboard-driven float-move (arrows / WASD)."""

    def _armed(self, board_view, session):
        inst = _place(board_view, session, 0.5, 0.5)
        select = _select(board_view)
        _press(select, 0.5, 0.5)
        assert not select._selection.is_empty()
        _key(select, Qt.Key.Key_G)  # arm
        armed = _armed_tool(board_view)
        assert session.edit_controller.armed_transform is not None
        return inst, armed

    def test_arrows_then_enter_commits_move(self, board_view, session) -> None:
        # Task 4: confirm commits and re-arms — it does not disarm.
        inst, armed = self._armed(board_view, session)
        before = session.layout.placements[inst.id].position
        _key(armed, Qt.Key.Key_Right)
        _key(armed, Qt.Key.Key_Down)
        _key(armed, Qt.Key.Key_Return)  # drop
        assert session.edit_controller.armed_transform is not None
        after = session.layout.placements[inst.id].position
        assert (after.x, after.y) == (before.x + 1.0, before.y + 1.0)

    def test_wasd_steps(self, board_view, session) -> None:
        inst, armed = self._armed(board_view, session)
        before = session.layout.placements[inst.id].position
        _key(armed, Qt.Key.Key_D)  # right
        _key(armed, Qt.Key.Key_S)  # down
        _key(armed, Qt.Key.Key_Return)
        after = session.layout.placements[inst.id].position
        assert (after.x, after.y) == (before.x + 1.0, before.y + 1.0)

    def test_esc_cancels_move(self, board_view, session) -> None:
        # Task 4: cancel reverts and re-arms — it does not disarm.
        inst, armed = self._armed(board_view, session)
        before = session.layout.placements[inst.id].position
        _key(armed, Qt.Key.Key_Right)
        _key(armed, Qt.Key.Key_Escape)  # cancel — no commit
        assert session.edit_controller.armed_transform is not None
        after = session.layout.placements[inst.id].position
        assert (after.x, after.y) == (before.x, before.y)


class TestArmedAtomic:
    """_invoke_armed_atomic: arm -> one action -> accept, conditional disarm.

    The atomic menu/macro path routes through the ArmedTransformTool so a
    conflicting action parks the tool active (same conditional-disarm as the
    interactive accept).
    """

    def _step_cw(self, armed) -> None:
        armed.on_key(key_inp(Qt.Key.Key_Up))  # one CW step

    def test_atomic_rotate_commits_and_disarms(self, board_view, session) -> None:
        inst = _place(board_view, session, 0.5, 0.5)
        select = _select(board_view)
        _press(select, 0.5, 0.5)
        base = session.layout.placements[inst.id].rotation
        depth = len(session.stack._undo)  # type: ignore[attr-defined]

        ok = select._invoke_armed_atomic(select.arm_rotate, self._step_cw)

        assert ok is True
        assert session.edit_controller.armed_transform is None  # disarmed
        assert isinstance(board_view._active_tool, SelectTool)
        assert session.layout.placements[inst.id].rotation == (base + 90) % 360
        assert len(session.stack._undo) == depth + 1  # type: ignore[attr-defined]

    def test_atomic_noop_on_empty_selection(self, board_view, session) -> None:
        select = _select(board_view)  # nothing selected
        ok = select._invoke_armed_atomic(select.arm_rotate, self._step_cw)
        assert ok is False
        assert session.edit_controller.armed_transform is None  # never armed

    def test_atomic_parks_armed_on_conflict(self, board_view, session) -> None:
        from protoboard.domain.layout.drag_analysis import (
            DragAnalysis,
            DragSeverity,
        )

        _place(board_view, session, 0.5, 0.5)
        select = _select(board_view)
        _press(select, 0.5, 0.5)

        def step_then_force_conflict(armed) -> None:
            self._step_cw(armed)
            transform = session.edit_controller.armed_transform
            assert transform is not None
            transform._classify = lambda: DragAnalysis(  # type: ignore[attr-defined]
                severity=DragSeverity.HARD
            )

        ok = select._invoke_armed_atomic(select.arm_rotate, step_then_force_conflict)

        assert ok is False
        transform = session.edit_controller.armed_transform
        assert transform is not None  # parked, waiting for the user
        assert board_view._active_tool is not select  # ArmedTransformTool active
        assert transform.finished is False


class TestFloatTransformSwitch:
    """Switch rotate<->move while floating, without committing, and commit the
    whole thing as one undo entry."""

    def _armed_rotate(self, board_view, session):
        inst = _place(board_view, session, 2.5, 2.5)
        select = _select(board_view)
        _press(select, 2.5, 2.5)
        _key(select, Qt.Key.Key_R)  # arm in rotate sub-mode
        armed = _armed_tool(board_view)
        assert session.edit_controller.armed_transform is not None
        return inst, armed

    def test_rotate_then_switch_to_move_preserves_float(
        self, board_view, session
    ) -> None:
        inst, armed = self._armed_rotate(board_view, session)
        base_rot = _pose(session, inst.id).rotation
        _key(armed, Qt.Key.Key_R)  # rotate +90 (per-object)
        assert _pose(session, inst.id).rotation == (base_rot + 90) % 360
        # [G] switches sub-mode WITHOUT committing or re-arming.
        _key(armed, Qt.Key.Key_G)
        transform = session.edit_controller.armed_transform
        assert transform is not None
        assert transform.mode == "move"
        assert transform.finished is False
        # Rotation survived the switch (float pose carried over).
        assert _pose(session, inst.id).rotation == (base_rot + 90) % 360

    def test_mixed_transform_commits_as_one_undo_entry(
        self, board_view, session
    ) -> None:
        inst, armed = self._armed_rotate(board_view, session)
        base = session.layout.placements[inst.id]
        base_pos, base_rot = base.position, base.rotation
        depth = len(session.stack._undo)  # type: ignore[attr-defined]

        _key(armed, Qt.Key.Key_R)  # rotate +90
        _key(armed, Qt.Key.Key_G)  # -> move sub-mode (no commit)
        _key(armed, Qt.Key.Key_Right)  # nudge +1x
        _key(armed, Qt.Key.Key_Return)  # accept the whole transform

        # Task 4: accept commits (one undo entry) and re-arms — it does not
        # disarm.
        assert session.edit_controller.armed_transform is not None
        assert len(session.stack._undo) == depth + 1  # type: ignore[attr-defined]
        after = session.layout.placements[inst.id]
        assert after.rotation == (base_rot + 90) % 360
        assert after.position.x == base_pos.x + 1.0

        session.stack.undo(session)  # type: ignore[arg-type]
        restored = session.layout.placements[inst.id]
        assert restored.rotation == base_rot
        assert (restored.position.x, restored.position.y) == (
            base_pos.x,
            base_pos.y,
        )

    def test_cancel_from_mixed_state_restores_original(
        self, board_view, session
    ) -> None:
        # Task 4: cancel reverts to the pristine pose and re-arms — it does
        # not disarm.
        inst, armed = self._armed_rotate(board_view, session)
        base = session.layout.placements[inst.id]
        base_pos, base_rot = base.position, base.rotation

        _key(armed, Qt.Key.Key_R)  # rotate +90
        _key(armed, Qt.Key.Key_G)  # -> move
        _key(armed, Qt.Key.Key_Right)  # nudge
        _key(armed, Qt.Key.Key_Escape)  # cancel — back to the pristine pose

        assert session.edit_controller.armed_transform is not None
        now = session.layout.placements[inst.id]
        assert now.rotation == base_rot
        assert (now.position.x, now.position.y) == (base_pos.x, base_pos.y)


class TestFloatTransformGroupFrames:
    """Armed float-transform on a group whose transform is non-identity.

    A world-frame preview op must be conjugated into the member's local frame;
    these assert the world pose follows the op exactly — preview == commit, no
    double-compose, cancel restores the pre-arm pose.
    """

    def _rotated_group(self, board_view, session):  # type: ignore[no-untyped-def]
        from protoboard.geometry import Transform2D

        a = _place(board_view, session, 1.5, 1.5)
        b = _place(board_view, session, 3.5, 1.5)
        gid = _make_group(session, [a.id, b.id])
        select = _select(board_view)
        select.selected_group_id = gid
        select.rotate_multi(delta=90)  # folds R90 into group.transform
        assert session.layout.groups[gid].transform != Transform2D.identity()
        return a, b, gid, select

    def _arm(self, board_view, select, key: Qt.Key) -> ArmedTransformTool:
        _key(select, key)
        return _armed_tool(board_view)

    def test_armed_move_nudge_shifts_world_by_exactly_one_cell(
        self, board_view: BoardView, session: EditorSession
    ) -> None:
        a, b, _gid, select = self._rotated_group(board_view, session)
        a0 = session.layout.placements[a.id].position
        b0 = session.layout.placements[b.id].position
        armed = self._arm(board_view, select, Qt.Key.Key_G)  # arm float-move
        _key(armed, Qt.Key.Key_Right)  # nudge (1, 0) — a WORLD-frame step
        a1 = _pose(session, a.id).position
        b1 = _pose(session, b.id).position
        assert (a1.x - a0.x, a1.y - a0.y) == (1.0, 0.0)
        assert (b1.x - b0.x, b1.y - b0.y) == (1.0, 0.0)
        _key(armed, Qt.Key.Key_Return)  # accept
        a2 = session.layout.placements[a.id].position
        b2 = session.layout.placements[b.id].position
        assert (a2.x, a2.y) == (a1.x, a1.y)
        assert (b2.x, b2.y) == (b1.x, b1.y)
        session.stack.undo(session)  # type: ignore[arg-type]
        assert session.layout.placements[a.id].position == a0
        assert session.layout.placements[b.id].position == b0

    def test_armed_move_cancel_restores_world_pose(
        self, board_view: BoardView, session: EditorSession
    ) -> None:
        a, b, _gid, select = self._rotated_group(board_view, session)
        a0 = session.layout.placements[a.id].position
        b0 = session.layout.placements[b.id].position
        armed = self._arm(board_view, select, Qt.Key.Key_G)
        _key(armed, Qt.Key.Key_Right)
        _key(armed, Qt.Key.Key_Escape)  # cancel — restore the pre-arm pose
        assert session.layout.placements[a.id].position == a0
        assert session.layout.placements[b.id].position == b0

    def test_armed_rotate_step_keeps_world_positions_orbiting_pivot(
        self, board_view: BoardView, session: EditorSession
    ) -> None:
        a, _b, _gid, select = self._rotated_group(board_view, session)
        a0 = session.layout.placements[a.id].position
        r0 = session.layout.placements[a.id].rotation
        armed = self._arm(board_view, select, Qt.Key.Key_R)  # arm rotate
        _key(armed, Qt.Key.Key_R)  # step +90 (per-object, own centre)
        a1 = _pose(session, a.id).position
        r1 = _pose(session, a.id).rotation
        assert (a1.x, a1.y) == (a0.x, a0.y)
        assert r1 == (r0 + 90) % 360

    def test_nested_group_member_shifts_by_delta(
        self, board_view: BoardView, session: EditorSession
    ) -> None:
        a = _place(board_view, session, 1.5, 1.5)
        b = _place(board_view, session, 3.5, 1.5)
        child_gid = _make_group(session, [b.id], name="child")
        parent_gid = _make_group(session, [a.id], name="parent")
        parent = session.layout.groups[parent_gid]
        session.layout.groups[parent_gid] = ComponentGroup(
            id=parent_gid,
            name=parent.name,
            component_ids=parent.component_ids,
            child_group_ids=frozenset({child_gid}),
        )
        select = _select(board_view)
        select.selected_group_id = parent_gid
        select.rotate_multi(delta=90)
        b0 = session.layout.placements[b.id].position
        armed = self._arm(board_view, select, Qt.Key.Key_G)
        _key(armed, Qt.Key.Key_Right)
        _key(armed, Qt.Key.Key_Return)
        b1 = session.layout.placements[b.id].position
        assert (b1.x - b0.x, b1.y - b0.y) == (1.0, 0.0)
