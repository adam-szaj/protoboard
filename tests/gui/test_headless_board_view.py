# SPDX-License-Identifier: MIT
"""HeadlessBoardView: tool + camera flows with no realized widget (qt-ports §4).

These are the tests the Protocol swap unlocks: ToolHost dispatch, ViewCamera
math and tool logic run against the double — a real QGraphicsScene, the
CameraMath viewport, recorded notifications — without qtbot or an event loop
(the QApplication the suite already owns is enough for QTimer/QGraphicsScene).
"""

from __future__ import annotations

from PySide6.QtCore import Qt

from protoboard.geometry import Point
from protoboard.gui.session import EditorSession
from protoboard.gui.tools.select_tool import SelectTool
from tests.fakes.board_view import HeadlessBoardView
from tests.gui.input_helpers import key_inp, pointer


def _headless(session: EditorSession) -> HeadlessBoardView:
    return HeadlessBoardView(session)


class TestToolLifecycle:
    def test_set_tool_activates_and_builds_keymap_layer(
        self, session: EditorSession
    ) -> None:
        view = _headless(session)
        tool = SelectTool()
        view.tools.set_tool(tool)
        assert view.active_tool is tool
        assert view.refresh_count == 1
        # The tool's KeyMap layer resolves its own actions.
        assert view.keymap.resolve("select-tool.action.reroute") == "W"

    def test_status_publication_records_structurally(
        self, session: EditorSession
    ) -> None:
        view = _headless(session)
        tool = SelectTool()
        view.tools.set_tool(tool)
        tool._status("hello", 1234)
        evt = view.notifications.status[-1]
        assert evt.kind == "select.status"
        assert evt.data["timeout_ms"] == 1234

    def test_selection_click_selects_component_headless(
        self, session: EditorSession
    ) -> None:
        # A full user flow — place + click-select — with no widget at all.
        from protoboard.gui.tools.place_tool import PlaceTool

        view = _headless(session)
        view.tools.set_tool(PlaceTool("R"))
        place = view.active_tool
        assert isinstance(place, PlaceTool)
        place.on_press(pointer(0.5, 0.5))
        while place._phase == "drag_pin":
            place.on_press(pointer(0.5, 0.5))
        assert len(session.layout.placements) == 1

        select = SelectTool()
        view.tools.set_tool(select)
        select.on_press(pointer(0.5, 0.5))
        hits = {s.to_hit().kind for s in select._selection.selected()}
        assert "component" in hits


class TestKeyDispatchHeadless:
    def test_escape_ladder_records_escape(self, session: EditorSession) -> None:
        from PySide6.QtCore import QEvent
        from PySide6.QtGui import QKeyEvent

        view = _headless(session)
        view.tools.set_tool(SelectTool())
        ev = QKeyEvent(
            QEvent.Type.KeyPress, Qt.Key.Key_Escape,
            Qt.KeyboardModifier.NoModifier,
        )
        ev.ignore()
        view.tools.handle_key_press(ev)
        assert view.notifications.escapes == 1

    def test_help_overlay_toggle_via_port(self, session: EditorSession) -> None:
        view = _headless(session)
        view.tools.set_tool(SelectTool())
        view.tools._toggle_shortcuts_overlay()
        assert view.help_overlay.help_visible
        assert view.help_overlay.html is not None
        assert "Reroute" in view.help_overlay.html
        view.tools._toggle_shortcuts_overlay()
        assert not view.help_overlay.help_visible

    def test_tool_consumes_key_through_dispatch(
        self, session: EditorSession
    ) -> None:
        view = _headless(session)
        tool = SelectTool()
        view.tools.set_tool(tool)
        # [R] with nothing selected reports "select first" and consumes.
        assert tool.on_key(key_inp(Qt.Key.Key_R)) is True
        assert view.notifications.status  # armed-rotate hint or refusal


class TestCameraMathHeadless:
    def test_zoom_by_scales_within_limits(self, session: EditorSession) -> None:
        view = _headless(session)
        before = view.viewport_port.view_scale()
        view.camera.zoom_by(1.5)
        after = view.viewport_port.view_scale()
        assert after > before

    def test_zoom_out_hits_floor_and_centers_board(
        self, session: EditorSession
    ) -> None:
        view = _headless(session)
        view.camera.zoom_by(1e-6)  # slam into the zoom-out floor
        b = session.board.bounds
        centre = view.viewport_port.map_view_to_scene(Point(320.0, 240.0))
        assert abs(centre.x - (b.x + b.width / 2)) < 0.51
        assert abs(centre.y - (b.y + b.height / 2)) < 0.51

    def test_fit_board_frames_bounds(self, session: EditorSession) -> None:
        view = _headless(session)
        view.camera.fit_board()
        visible = view.viewport_port.visible_scene_rect()
        b = session.board.bounds
        assert visible.contains(Point(b.x + 0.1, b.y + 0.1))
        assert visible.contains(
            Point(b.x + b.width - 0.1, b.y + b.height - 0.1)
        )

    def test_mirror_toggle_flips_sign(self, session: EditorSession) -> None:
        view = _headless(session)
        assert view.viewport_port.view_transform().a > 0
        view.camera.toggle_mirror_h()
        assert view.viewport_port.view_transform().a < 0
        assert view.camera.mirror_flags() == (True, False)


class TestContextHeadless:
    def test_get_context_at_needs_no_widget(
        self, session: EditorSession
    ) -> None:
        view = _headless(session)
        view.tools.set_tool(SelectTool())
        ctx = view.tools.get_context_at(Point(1.5, 1.5), Point(30.0, 40.0))
        assert any(
            o.action_id == "select-tool.action.autoroute" for o in ctx.actions
        )
        view.notify_context(ctx)
        assert view.notifications.contexts == [ctx]
