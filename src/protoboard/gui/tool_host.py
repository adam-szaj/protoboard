# SPDX-License-Identifier: MIT
"""ToolHost — tool lifecycle, input dispatch, canvas menu and [?] overlay.

Extracted from ``BoardView`` (phase 5 of the decomposition).  Owns the active
tool + the permanent-tool list, the namespaced :class:`KeyMap` tools register
their actions in, the mouse-pan state, the right-click canvas menu and the
``[?]`` shortcut-help overlay.  The Qt input overrides stay on ``BoardView``
and delegate here; the host talks to the canvas only through
:class:`~protoboard.gui.board_view_tool.AbstractBoardView`.
"""

from __future__ import annotations

from typing import TYPE_CHECKING

from PySide6.QtCore import QEvent, QPoint, QPointF, Qt
from PySide6.QtGui import QKeyEvent, QMouseEvent

from protoboard.gui.board_view_tool import _Tool
from protoboard.gui.keymap import GlobalKeyMap, KeyMap
from protoboard.services.debug_trace import traced

if TYPE_CHECKING:
    from protoboard.gui.board_view_tool import AbstractBoardView


class ToolHost:
    """Tool lifecycle + input dispatch for the board canvas."""

    def __init__(self, view: AbstractBoardView) -> None:
        self._view = view
        self._active_tool: _Tool | None = None
        # Tools always notified on scene_cleared, active or not (MainWindow
        # parks the long-lived SelectTool here).
        self.permanent_tools: list[_Tool] = []
        # Namespaced keybindings (Stage 1).  Owned here so a tool always has a
        # KeyMap to consult for the contextual shortcut veto, even headless
        # (BoardView without a MainWindow).  MainWindow loads user overrides
        # into it; tools register their own actions on activate().
        self._keymap = KeyMap(GlobalKeyMap.with_globals())
        # ``[?]`` shortcut-help overlay — a QLabel parked over the viewport,
        # built lazily on first toggle.
        self._shortcuts_overlay: object | None = None
        # Mouse-pan state (middle button, or Ctrl+LMB the tool declined).
        self._panning = False
        self._ctrl_panning = False
        self._pan_start = QPointF()

    # -- tool lifecycle ----------------------------------------------------------

    @property
    def active_tool(self) -> _Tool | None:
        """The tool currently receiving delegated input events."""
        return self._active_tool

    @property
    def keymap(self) -> KeyMap:
        """The namespaced keybindings the active tool consults for the veto."""
        return self._keymap

    @traced()
    def set_tool(self, tool: _Tool | None) -> None:
        """Swap the active tool (deactivate old, activate + register new)."""
        view = self._view
        # While autoroute is running, mutating tools (Place / Route / Cut)
        # are not allowed — fall back to the SelectTool instance (which is
        # purely read-only).  SelectTool itself, or None, is always allowed.
        if tool is not None and view.session.autoroute_active:
            from protoboard.gui.tools.select_tool import SelectTool as _ST
            if not isinstance(tool, _ST):
                tool = None
        if self._active_tool is not None:
            self._active_tool.deactivate()
        self._active_tool = tool
        view.highlight.set_highlighted_net_silent(None)
        # Tool change may affect board_alpha (e.g. ghost mode) — rebuild board layer.
        view.mark_board_layer_dirty()
        if tool is not None:
            tool.activate(view.session, view)
            self._register_tool_keymap(tool)
        view.refresh()

    @traced()
    def detach_active_tool(self) -> None:
        """Deactivate + drop the active tool without a refresh (session swap)."""
        if self._active_tool is not None:
            self._active_tool.deactivate()
            self._active_tool = None

    @traced()
    def _register_tool_keymap(self, tool: _Tool) -> None:
        """Merge a tool's declared key actions into the KeyMap.

        Best-effort: a binding that collides with a ``final`` global action is
        logged and skipped rather than crashing the tool switch.
        """
        actions = tool.key_actions()
        if not actions:
            return
        from protoboard.gui.keymap import FinalShortcutError
        try:
            self._keymap.register_tool_actions(actions)
        except FinalShortcutError as exc:
            import structlog
            structlog.get_logger(__name__).warning(
                "keymap.tool_action_rejected", error=str(exc)
            )

    @traced()
    def fire_on_scene_cleared(self) -> None:
        """Notify tools that the scene was wiped (board layer rebuild)."""
        notified: set[int] = set()
        for tool in self.permanent_tools:
            tool.on_scene_cleared()
            notified.add(id(tool))
        if self._active_tool is not None and id(self._active_tool) not in notified:
            self._active_tool.on_scene_cleared()

    # -- tool actions (menu / synthesised keys) --------------------------------------

    @traced()
    def trigger_tool_action(self, action_id: str) -> None:
        """Run *action_id* by synthesising its resolved key into the tool."""
        from protoboard.gui.shortcuts_help import shortcut_to_key_mods
        tool = self._active_tool
        if tool is None:
            return
        decoded = shortcut_to_key_mods(self._keymap.resolve(action_id))
        if decoded is None:
            return
        key, mods = decoded
        event = QKeyEvent(
            QEvent.Type.KeyPress, key, Qt.KeyboardModifier(mods)
        )
        tool.on_key(event)

    @traced()
    def add_tool_action_menu(self, menu: object) -> dict[object, str]:
        """Add the active tool's actions to *menu*; return {QAction: action_id}.

        Empty (no tool, or the tool declares no actions) → adds nothing.
        """
        from PySide6.QtWidgets import QMenu
        assert isinstance(menu, QMenu)
        tool = self._active_tool
        if tool is None:
            return {}
        action_ids = tool.displayable_action_ids()
        if not action_ids:
            return {}
        from protoboard.gui.shortcuts_help import (
            category_label,
            tool_shortcut_rows,
        )
        rows = tool_shortcut_rows(self._keymap, action_ids)
        header = category_label(action_ids[0]) + " actions"
        menu.addSection(header)
        items: dict[object, str] = {}
        for row in rows:
            text = f"{row.label}\t{row.shortcut}" if row.shortcut else row.label
            act = menu.addAction(text)
            items[act] = row.action_id
        menu.addSeparator()
        return items

    @traced()
    def show_canvas_menu(self, vp_pos: QPoint, scene_pt: QPointF) -> None:
        """Generic right-click canvas menu (no tool consumed the press)."""
        from PySide6.QtWidgets import QMenu

        view = self._view
        menu = QMenu(view)
        # The active tool's own actions, with their current shortcuts, so the
        # keyboard operations are discoverable + clickable.  Triggering one
        # synthesises its resolved key and feeds it back through on_key — the
        # real dispatch path, never duplicated behaviour.
        tool_action_items = self.add_tool_action_menu(menu)
        act_center = menu.addAction("Center view here")
        act_escape = menu.addAction("Cancel tool (Esc)")
        chosen = menu.exec(view.mapToGlobal(vp_pos))
        if chosen is None:
            return
        if chosen is act_center:
            view.center_on_scene_point(scene_pt)
        elif chosen is act_escape:
            view.escape_tool.emit()
        elif chosen in tool_action_items:
            self.trigger_tool_action(tool_action_items[chosen])

    # -- [?] shortcut-help overlay -----------------------------------------

    @traced()
    def _shortcuts_overlay_visible(self) -> bool:
        # Track shown-state via ``isHidden()`` (False once ``show()`` is called)
        # rather than ``isVisible()`` — the latter is False in a headless test
        # whose parent viewport is never mapped.
        from PySide6.QtWidgets import QLabel
        ov = self._shortcuts_overlay
        return isinstance(ov, QLabel) and not ov.isHidden()

    @traced()
    def _toggle_shortcuts_overlay(self) -> None:
        if self._shortcuts_overlay_visible():
            self._hide_shortcuts_overlay()
        else:
            self._show_shortcuts_overlay()

    @traced()
    def _hide_shortcuts_overlay(self) -> None:
        from PySide6.QtWidgets import QLabel
        ov = self._shortcuts_overlay
        if isinstance(ov, QLabel):
            ov.hide()

    def _app_keybindings(self) -> object | None:
        """The live ``KeyBindings`` table (with user overrides), or None.

        Held by the MainWindow; the overlay reaches it through ``window()`` so
        the global section reflects real, currently-bound shortcuts.  None ⇒
        ``app_shortcut_sections`` falls back to the shipped defaults.
        """
        win = self._view.window()
        return getattr(win, "_keybindings", None)

    @traced()
    def _show_shortcuts_overlay(self) -> None:
        """Float a panel listing the globally + tool-relevant shortcuts.

        Reads the live :class:`KeyMap`, so the panel always reflects the user's
        current bindings.  Global rows first, then the active tool's actions.
        """
        from PySide6.QtCore import Qt as _Qt
        from PySide6.QtWidgets import QLabel

        from protoboard.gui.shortcuts_help import (
            app_shortcut_sections,
            category_label,
            tool_shortcut_rows,
        )

        view = self._view
        # Application shortcuts straight from the authoritative registry +
        # the live KeyBindings (user overrides included), grouped by menu —
        # replaces the old hand-maintained "Stage 1" id list.
        sections: list[tuple[str, list[object]]] = [
            (title, list(rows))
            for title, rows in app_shortcut_sections(self._app_keybindings())
        ]
        # Then the active tool's own (armed) actions, resolved through the KeyMap.
        tool = self._active_tool
        if tool is not None:
            tool_ids = tool.displayable_action_ids()
            if tool_ids:
                sections.append((
                    category_label(tool_ids[0]),
                    list(tool_shortcut_rows(self._keymap, tool_ids)),
                ))

        html = self._render_shortcuts_html(sections)
        ov = self._shortcuts_overlay
        if not isinstance(ov, QLabel):
            ov = QLabel(view.viewport())
            ov.setTextFormat(_Qt.TextFormat.RichText)
            ov.setStyleSheet(
                "QLabel { background: rgba(20,22,28,235); color: #E6E6E6; "
                "border: 1px solid #4A5060; border-radius: 6px; padding: 10px; }"
            )
            self._shortcuts_overlay = ov
        ov.setText(html)
        ov.adjustSize()
        margin = 12
        ov.move(view.viewport().width() - ov.width() - margin, margin)
        ov.show()
        ov.raise_()

    @staticmethod
    def _render_shortcuts_html(
        sections: list[tuple[str, list[object]]]
    ) -> str:
        from protoboard.gui.shortcuts_help import ShortcutRow

        parts = [
            "<div style='font-size:11pt'><b>Keyboard shortcuts</b> "
            "<span style='color:#9098A8'>(? to close)</span></div>"
        ]
        for title, rows in sections:
            parts.append(
                f"<div style='margin-top:8px;color:#9098A8'><b>{title}</b></div>"
                "<table cellspacing='2'>"
            )
            for row in rows:
                assert isinstance(row, ShortcutRow)
                sc = row.shortcut or "—"
                parts.append(
                    f"<tr><td style='padding-right:18px'>{row.label}</td>"
                    f"<td style='color:#7FD0FF'><code>{sc}</code></td></tr>"
                )
            parts.append("</table>")
        return "".join(parts)

    # -- input dispatch -----------------------------------------------------------
    #
    # The Qt overrides on BoardView forward here.  Handlers return True when
    # the event was consumed; False tells the view to fall through to the
    # QGraphicsView default behaviour.

    def handle_mouse_press(self, event: QMouseEvent) -> bool:
        view = self._view
        ctrl = event.modifiers() & Qt.KeyboardModifier.ControlModifier
        if event.button() == Qt.MouseButton.MiddleButton:
            self._panning = True
            self._pan_start = event.position()
            view.setCursor(Qt.CursorShape.ClosedHandCursor)
        elif event.button() == Qt.MouseButton.LeftButton and ctrl:
            # Ctrl+LeftButton: check if tool wants to handle it first (component
            # drag); if the tool consumed it (returns True), skip view pan.
            scene_pt = view.mapToScene(event.pos())
            if self._active_tool is not None and self._active_tool.on_press(
                event, scene_pt
            ):
                return True
            # No tool consumed it — pan the view.
            self._ctrl_panning = True
            self._panning = True
            self._pan_start = event.position()
            view.setCursor(Qt.CursorShape.ClosedHandCursor)
        elif event.button() == Qt.MouseButton.RightButton:
            scene_pt = view.mapToScene(event.pos())
            handler = getattr(self._active_tool, "on_right_press", None)
            consumed = handler(scene_pt) if handler is not None else False
            if not consumed:
                self.show_canvas_menu(event.pos(), scene_pt)
        elif self._active_tool is not None:
            scene_pt = view.mapToScene(event.pos())
            self._active_tool.on_press(event, scene_pt)
        else:
            return False  # fall through to QGraphicsView
        return True

    def handle_mouse_move(self, event: QMouseEvent) -> bool:
        view = self._view
        if self._panning:
            delta = event.position() - self._pan_start
            self._pan_start = event.position()
            view.horizontalScrollBar().setValue(
                int(view.horizontalScrollBar().value() - delta.x())
            )
            view.verticalScrollBar().setValue(
                int(view.verticalScrollBar().value() - delta.y())
            )
        scene_pt = view.mapToScene(event.pos())
        view.cursor_moved.emit(scene_pt.x(), scene_pt.y())
        if not self._panning and self._active_tool is not None:
            self._active_tool.on_move(event, scene_pt)
        elif not self._panning:
            return False  # fall through to QGraphicsView
        return True

    def handle_mouse_release(self, event: QMouseEvent) -> bool:
        view = self._view
        if event.button() == Qt.MouseButton.MiddleButton and self._panning:
            self._panning = False
            view.setCursor(Qt.CursorShape.ArrowCursor)
        elif event.button() == Qt.MouseButton.LeftButton and self._ctrl_panning:
            self._panning = False
            self._ctrl_panning = False
            view.setCursor(Qt.CursorShape.ArrowCursor)
        elif self._active_tool is not None:
            scene_pt = view.mapToScene(event.pos())
            self._active_tool.on_release(event, scene_pt)
        else:
            return False  # fall through to QGraphicsView
        return True

    def handle_double_click(self, event: QMouseEvent) -> None:
        view = self._view
        left = Qt.MouseButton.LeftButton
        if event.button() == left and self._active_tool is not None:
            scene_pt = view.mapToScene(event.pos())
            handler = getattr(self._active_tool, "on_double_press", None)
            if handler is not None:
                handler(scene_pt)

    def wants_shortcut_override(self, event: QEvent) -> bool:
        """Give the active tool first refusal on a global shortcut's key.

        Application shortcuts are ``WindowShortcut`` QActions, so Qt would fire
        them (switch tool on ``[W]``, focus library on ``[A]``, delete on
        ``Del``) before the focused board view ever sees the key.  On a
        ``ShortcutOverride`` we ask the active tool whether it needs that key
        *right now* (e.g. a selected wire-edit fragment); if it does, accepting
        the event vetoes the shortcut and Qt re-delivers the key as a normal
        ``KeyPress`` → ``keyPressEvent`` → ``tool.on_key``.
        """
        tool = self._active_tool
        return (
            tool is not None
            and isinstance(event, QKeyEvent)
            and tool.wants_key(event)
        )

    def handle_key_press(self, event: QKeyEvent) -> None:
        view = self._view
        if event.isAutoRepeat():
            event.ignore()
            return
        key = event.key()
        # ``?`` toggles the shortcut-help overlay (resolved through the KeyMap,
        # so a rebind moves it).  Esc closes it before anything else.
        if self._keymap.matches(
            key, event.modifiers(), "global.action.show_shortcuts"
        ):
            self._toggle_shortcuts_overlay()
            event.accept()
            return
        if key == Qt.Key.Key_Escape:
            if self._shortcuts_overlay_visible():
                self._hide_shortcuts_overlay()
                event.accept()
                return
            if self._active_tool is not None:
                self._active_tool.on_key(event)
            view.escape_tool.emit()
            return
        # An armed tool (float-rotate / keyboard-move) claims arrows for
        # stepping — give it first crack BEFORE the arrow-scroll fallback, else
        # the view scrolls instead.  Gated on wants_key (the tool decides by
        # action id), so an idle tool still scrolls on arrows as before.
        tool = self._active_tool
        if tool is not None and tool.wants_key(event):
            tool.on_key(event)
            if event.isAccepted():
                return

        # Arrow keys — scroll by one raster unit (only without modifier).
        if not event.modifiers() and key in (
            Qt.Key.Key_Left, Qt.Key.Key_Right,
            Qt.Key.Key_Up, Qt.Key.Key_Down,
        ):
            # Convert 1 raster unit to viewport pixels using current transform.
            px = max(int(view.transform().m11()), 1)
            py = max(int(view.transform().m22()), 1)
            if key == Qt.Key.Key_Left:
                view.horizontalScrollBar().setValue(
                    view.horizontalScrollBar().value() - px
                )
            elif key == Qt.Key.Key_Right:
                view.horizontalScrollBar().setValue(
                    view.horizontalScrollBar().value() + px
                )
            elif key == Qt.Key.Key_Up:
                view.verticalScrollBar().setValue(
                    view.verticalScrollBar().value() - py
                )
            elif key == Qt.Key.Key_Down:
                view.verticalScrollBar().setValue(
                    view.verticalScrollBar().value() + py
                )
            return
        if self._active_tool is not None:
            self._active_tool.on_key(event)
        # Forward to MainWindow so global shortcuts (S/W/C/H/F5/F6/Ctrl+0…) work
        # regardless of which widget has focus.  Tools call event.accept() for keys
        # they handle exclusively (R, Del, A, Enter, F) to prevent double-dispatch.
        if not event.isAccepted():
            view.window().keyPressEvent(event)

    def handle_key_release(self, event: QKeyEvent) -> None:
        if self._active_tool is not None:
            self._active_tool.on_key_release(event)
