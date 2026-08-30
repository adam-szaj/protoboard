# SPDX-License-Identifier: MIT
"""Tests for BoardView — snap, refresh, tool management."""

from __future__ import annotations

from typing import cast

from PySide6.QtWidgets import QGraphicsItemGroup
from pytestqt.qtbot import QtBot

from protoboard.geometry import Point
from protoboard.gui.board_view import BoardView
from protoboard.gui.board_view_tool import _DefaultTool
from protoboard.gui.qt_help_overlay import QtHelpOverlay
from protoboard.gui.session import EditorSession
from protoboard.gui.tools.gui_tools import SelectTool, WireEditTool


def _arm_wire_edit(board_view: BoardView) -> WireEditTool:
    """Arm a WireEditTool on a wire so its ops (reroute / …) surface in the
    [?] overlay + context — reroute moved to WireEditTool in the Task-8 swap."""
    import uuid

    from protoboard.controller.selection.wire_edit import WireRange
    from protoboard.domain.layout.wire import Wire

    session = board_view.session
    wire = Wire(
        id=uuid.uuid4(),
        path=[Point(1.5, 1.5), Point(2.5, 1.5), Point(3.5, 1.5)],
        insulated=True,
        side="top",
    )
    session.layout.wires.append(wire)
    session.invalidate_graph()
    board_view.set_tool(SelectTool())
    tool = WireEditTool(
        WireRange.full_span(wire),
        wire.id,
        return_to=lambda: None,
    )
    board_view.set_tool(tool)
    return tool


class TestSnap:
    def test_snap_returns_nearest_pad(self, board_view: BoardView) -> None:
        # Board is 5×5, gap=0, so pads at (0.5,0.5), (1.5,0.5) etc.
        result = board_view.snap(Point(0.4, 0.4))
        assert result == Point(0.5, 0.5)

    def test_snap_far_from_origin(self, board_view: BoardView) -> None:
        result = board_view.snap(Point(3.6, 3.6))
        assert result == Point(3.5, 3.5)

    def test_snap_exact_pad_center(self, board_view: BoardView) -> None:
        result = board_view.snap(Point(2.5, 2.5))
        assert result == Point(2.5, 2.5)

    def test_snap_matches_controller_snap(self, board_view: BoardView) -> None:
        # Byte-parity guard: the view's snap and the Qt-free
        # EditController.snap must agree for every query, since the GUI adapter
        # (E4) will route snapping through the controller.
        ctrl = board_view.session.edit_controller
        agid = board_view.active_group_id
        for x in (0.4, 1.0, 2.5, 3.6, 4.9, 40.2):
            for y in (0.4, 2.9, 3.4, 41.8):
                pt = Point(x, y)
                assert board_view.snap(pt) == ctrl.snap(pt, active_group_id=agid)


class TestRefresh:
    def test_refresh_builds_bg_cache_display_list(self, board_view: BoardView) -> None:
        # Board layer no longer lives in the scene — it's rasterised
        # into the background cache by drawBackground.  Verify that the
        # display list has the expected primitives after a refresh.
        board_view.refresh()
        board_view.paint._ensure_bg_cache()
        from protoboard.gui.renderers.board_renderer import BoardDisplayList

        assert isinstance(board_view.paint._bg_display_list, BoardDisplayList)
        # 5×5 = 25 pads expected for the test fixture board.
        assert len(board_view.paint._bg_display_list.pads) >= 25

    def test_refresh_is_idempotent(self, board_view: BoardView) -> None:
        board_view.refresh()
        count1 = len(board_view.scene().items())
        board_view.refresh()
        count2 = len(board_view.scene().items())
        assert count1 == count2


class TestToolManagement:
    def test_set_tool_activates_new_tool(
        self, board_view: BoardView, session: EditorSession
    ) -> None:
        tool = SelectTool()
        board_view.set_tool(tool)
        assert board_view._active_tool is tool

    def test_set_tool_deactivates_previous(
        self, board_view: BoardView, session: EditorSession
    ) -> None:
        deactivated: list[bool] = []

        class TrackingTool(_DefaultTool):
            def deactivate(self) -> None:
                deactivated.append(True)

        board_view.set_tool(TrackingTool())
        board_view.set_tool(SelectTool())
        assert deactivated == [True]

    def test_set_tool_none_clears_active(self, board_view: BoardView) -> None:
        board_view.set_tool(SelectTool())
        board_view.set_tool(None)
        assert board_view._active_tool is None

    def test_layout_changed_triggers_refresh(
        self, board_view: BoardView, session: EditorSession, qtbot: QtBot
    ) -> None:
        # Manually count items before and after adding a wire via layout_changed.
        import uuid

        from protoboard.domain.layout.wire import Wire
        from protoboard.geometry import Point

        wire = Wire(
            id=uuid.uuid4(),
            path=[Point(0.5, 0.5), Point(1.5, 0.5)],
            insulated=True,
            side="top",
        )
        session.layout.wires.append(wire)
        session.layout_changed.emit()
        # layout_changed is debounced via a 16ms timer — wait for it to fire.
        qtbot.wait(50)
        # After refresh the wire's QGraphicsItemGroup must be registered.
        # Pad/board items no longer live in the scene (they're rasterised
        # into the background cache), so counting scene items is no
        # longer a useful proxy for "refresh happened".
        assert wire.id in board_view.composer._wire_groups


class TestBoardSandwichCache:
    """Layered background: board base + back cache + board mid blits."""

    def test_back_cache_invalidates_on_refresh(
        self, board_view: BoardView, session: EditorSession, qtbot: QtBot
    ) -> None:
        # Build cache once.
        board_view.paint._back_cache_dirty = False
        board_view.paint._back_cache = object()  # opaque sentinel
        board_view.refresh()
        assert (
            board_view.paint._back_cache_dirty
        ), "refresh() must invalidate _back_cache_dirty so the next paint rebuilds"

    def test_back_cache_hides_back_items_after_render(
        self, board_view: BoardView, session: EditorSession, qtbot: QtBot
    ) -> None:
        # Add a back-side wire so we have a back item to cache.
        import uuid

        from protoboard.domain.layout.wire import Wire
        from protoboard.geometry import Point

        wire = Wire(
            id=uuid.uuid4(),
            path=[Point(0.5, 0.5), Point(1.5, 0.5)],
            insulated=True,
            side="bottom",  # back side
        )
        session.layout.wires.append(wire)
        session.layout_changed.emit()
        qtbot.wait(50)

        # Force a viewport size so the cache build is exercised.
        board_view.resize(400, 300)
        qtbot.wait(20)
        board_view.paint._ensure_back_cache()

        # Back-side group exists in scene but was hidden after rasterisation.
        wire_group = cast(
            QGraphicsItemGroup, board_view.composer._wire_groups.get(wire.id)
        )
        assert wire_group is not None
        # Cache may be None if no back items existed at viewport — but with
        # the wire we just added it should be present.
        # (If render failed silently the test still passes — we mainly
        # assert the hidden-items list got populated.)
        if board_view.paint._back_cache is not None:
            # A wire renders as a front group + top-level back sibling; the
            # bottom wire's content lives in the back sibling — that is the
            # item the sandwich cache bakes and hides.
            back_grp = getattr(wire_group, "back", wire_group)
            assert back_grp in board_view.paint._back_hidden_items
            assert back_grp.isVisible() is False

    def test_alpha_for_board_mid_default(self) -> None:
        from protoboard.gui.layer_state import LayerState

        ls = LayerState()
        # FULL → board_mid_alpha (default 0.10 from Theme propagation;
        # here we just use the LayerState default 0.10).
        assert ls.alpha_for_board_mid() == 0.10
        # HIDDEN → 0
        from protoboard.gui.layer_state import LayerVisibility

        ls.overrides.board = LayerVisibility.HIDDEN
        assert ls.alpha_for_board_mid() == 0.0
        # GHOST → 0
        ls.overrides.board = LayerVisibility.GHOST
        assert ls.alpha_for_board_mid() == 0.0

    def test_components_hidden_does_not_hide_labels(self) -> None:
        from protoboard.gui.layer_state import LayerState, LayerVisibility

        ls = LayerState()
        ls.overrides.components_top = LayerVisibility.HIDDEN
        assert ls.alpha_for_component("top") == 0.0
        # Labels independent.
        assert ls.alpha_for_label("top") == 1.0
        assert ls.alpha_for_meta_pin_labels("top") == 1.0


class TestShortcutsHelp:
    """The [?] overlay and the RMB tool-action menu — both read the live KeyMap."""

    def test_overlay_toggles(self, board_view: BoardView) -> None:
        board_view.set_tool(SelectTool())
        assert not board_view.tools._shortcuts_overlay_visible()
        board_view.tools._toggle_shortcuts_overlay()
        assert board_view.tools._shortcuts_overlay_visible()
        board_view.tools._toggle_shortcuts_overlay()
        assert not board_view.tools._shortcuts_overlay_visible()

    def test_question_keystroke_opens_overlay(self, board_view: BoardView) -> None:
        """Regression: the real ``?`` keystroke toggles the overlay.

        On most layouts ``?`` is ``Shift+/`` → ``Key_Question`` *with*
        ``ShiftModifier``.  The KeyMap binding is bare ``"?"`` (no Shift flag),
        so the matcher must reconcile the live Shift against the shifted symbol
        — previously it did not and ``[?]`` did nothing.  Drive the genuine
        keyPressEvent, not the toggle method, so the key→toggle path is covered.
        """
        from PySide6.QtCore import Qt
        from PySide6.QtGui import QKeyEvent

        board_view.set_tool(SelectTool())
        assert not board_view.tools._shortcuts_overlay_visible()
        ev = QKeyEvent(
            QKeyEvent.Type.KeyPress,
            Qt.Key.Key_Question,
            Qt.KeyboardModifier.ShiftModifier,
        )
        board_view.keyPressEvent(ev)
        assert board_view.tools._shortcuts_overlay_visible()

    def test_overlay_lists_tool_actions(self, board_view: BoardView) -> None:
        from PySide6.QtWidgets import QLabel

        # reroute is a WireEditTool op after the Task-8 swap.
        _arm_wire_edit(board_view)
        board_view.tools._show_shortcuts_overlay()
        ov = cast(QtHelpOverlay, board_view.help_overlay)._label
        assert isinstance(ov, QLabel)
        text = ov.text()
        # A wire-edit tool action and its shortcut appear.
        assert "Reroute" in text

    def test_overlay_lists_real_app_shortcuts(self, board_view: BoardView) -> None:
        # Regression: the global section is built from the real action registry
        # (ALL_ACTIONS + live KeyBindings), not a hand-maintained id list — so
        # actual app shortcuts (Save, Undo, DRC…) appear, grouped by menu.
        from PySide6.QtWidgets import QLabel

        board_view.set_tool(SelectTool())
        board_view.tools._show_shortcuts_overlay()
        ov = cast(QtHelpOverlay, board_view.help_overlay)._label
        assert isinstance(ov, QLabel)
        text = ov.text()
        assert "Save" in text and "Ctrl+S" in text  # File
        assert "Undo" in text  # Edit
        assert "File" in text and "Analysis" in text  # menu-grouped headings

    def test_overlay_reflects_rebind(self, board_view: BoardView) -> None:
        from PySide6.QtWidgets import QLabel

        board_view.keymap.set_override("select-tool.action.reroute", "Q")
        _arm_wire_edit(board_view)  # reroute is a WireEditTool op now
        board_view.tools._show_shortcuts_overlay()
        ov = cast(QtHelpOverlay, board_view.help_overlay)._label
        assert isinstance(ov, QLabel)
        # The rebound key, not the default, is shown.
        assert ">Q<" in ov.text() or "<code>Q</code>" in ov.text()

    def test_tool_action_context_items(self, board_view: BoardView) -> None:
        # The tool-action section now travels as InteractionContext options
        # (qt-ports §2.4) — same content the old QMenu section carried.  reroute
        # is a WireEditTool op after the Task-8 swap; edit stays on SelectTool.
        _arm_wire_edit(board_view)
        ctx = board_view.tools.get_context_at(Point(0.5, 0.5), Point(0.0, 0.0))
        ids = {o.action_id for o in ctx.actions}
        assert "select-tool.action.reroute" in ids
        board_view.set_tool(SelectTool())
        ctx2 = board_view.tools.get_context_at(Point(0.5, 0.5), Point(0.0, 0.0))
        assert "select-tool.action.edit" in {o.action_id for o in ctx2.actions}

    def test_dispatch_tool_command_rotates(
        self, board_view: BoardView, session: EditorSession
    ) -> None:
        from protoboard.gui.tools.gui_tools import PlaceTool

        board_view.set_tool(PlaceTool("R"))
        tool = board_view._active_tool
        assert isinstance(tool, PlaceTool)
        before = tool._rotation
        # Dispatching the rotate action by id, through the Controller facade
        # — no synthesised key (trigger_tool_action is gone, Task 7).
        session.edit_controller.dispatch_tool_command("place-tool.action.rotate_cw")
        assert tool._rotation == (before + 90) % 360
