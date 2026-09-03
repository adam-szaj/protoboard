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

from dataclasses import replace
from typing import TYPE_CHECKING, cast

from PySide6.QtCore import QEvent, QPointF, Qt
from PySide6.QtGui import QKeyEvent, QMouseEvent, QWheelEvent

from protoboard.controller.ports import ActionOption, InteractionContext
from protoboard.controller.ports.viewport import CursorKind
from protoboard.geometry import Point, Vector
from protoboard.gui.board_view_tool import _Tool
from protoboard.gui.keymap import (
    VIEW_ACTIONS,
    VIEW_CANVAS_MENU,
    VIEW_PAN,
    VIEW_ZOOM_IN,
    VIEW_ZOOM_OUT,
    GlobalKeyMap,
    KeyMap,
    mouse_button_chord,
    wheel_chord,
)
from protoboard.gui.qt_inputs import key_input, mods_from_qt, pointer_input, wheel_input
from protoboard.gui.shortcuts_help import (
    ShortcutRow,
    app_shortcut_sections,
    category_label,
    tool_shortcut_rows,
)
from protoboard.instrumentation.debug_trace import traced
from protoboard.services.rendering.qt_geometry import from_qpointf, to_qpointf

if TYPE_CHECKING:
    from collections.abc import Callable

    from protoboard.controller.ports import (
        Mods,
        PointerInput,
        WheelInput,
    )
    from protoboard.controller.tools.base import ControllerTool
    from protoboard.gui.board_view_tool import AbstractBoardView


class ToolHost:
    """Tool lifecycle + input dispatch for the board canvas."""

    def __init__(self, view: AbstractBoardView) -> None:
        self._view = view
        # Tools always notified on scene_cleared, active or not (MainWindow
        # parks the long-lived SelectTool here).
        self.permanent_tools: list[_Tool] = []
        # Namespaced keybindings.  The permanent base map is owned here so a
        # tool always has a KeyMap to consult for the contextual shortcut
        # veto, even headless (BoardView without a MainWindow); MainWindow
        # loads user overrides into its store.  The active tool's own layer
        # (tool.make_key_map) is pushed on activate and popped on
        # deactivate — ``keymap`` always returns the current head.
        self._base_keymap = KeyMap(GlobalKeyMap.with_globals())
        # The canvas's own mouse gestures (zoom / pan / canvas menu) are real
        # actions on the base map, so a tool layer can shadow them while
        # active (e.g. an armed rotate claiming the wheel).
        self._base_keymap.declare(VIEW_ACTIONS)
        # Seed the shipped tool-shortcut defaults (armed-transform / place-tool
        # / route-tool action ids the tools themselves no longer declare — see
        # BUILTIN_DEFAULT_KEYBINDINGS_FILE) so every KeyMap resolves them from
        # construction, headless or not.  MainWindow re-runs this after with
        # the user's on-disk `defaults:` layered on top (idempotent).
        self._base_keymap.load_defaults()
        self._tool_keymap: KeyMap | None = None
        # Live KeyBindings provider — injected by MainWindow so the [?]
        # overlay's global section reflects real, currently-bound shortcuts
        # without the host reaching for ``window()``.  None ⇒ shipped defaults.
        self._keybindings_provider: Callable[[], object | None] = lambda: None
        # Mouse-pan state (middle button, or Ctrl+LMB the tool declined).
        self._panning = False
        self._ctrl_panning = False
        self._pan_start = QPointF()
        # View-side throttle for the hover verb stream (docs MVC step 1 §4):
        # a fast move stream collapses to one empty_action per interval, whose
        # reply is published via notify_hover.  33 ms mirrors the rubber-band /
        # drag-refresh cadence.
        from protoboard.gui.throttle import ParkLatestThrottle

        self._hover_throttle: ParkLatestThrottle[tuple[Point, Mods]] = (
            ParkLatestThrottle(33, self._fire_hover)
        )
        # KeyMap-layer swap on active-tool change (Controller facade, Task 7):
        # ``EditController.set_active_tool`` fires ``notify_active_tool_
        # changed``, which the GUI session turns into this signal.
        view.session.active_tool_changed.connect(self._on_active_tool_changed)

    # -- tool lifecycle ----------------------------------------------------------

    @property
    def _active_tool(self) -> _Tool | None:
        """The tool the EditController currently owns (Controller facade).

        A read-only property backed by ``edit_controller.active_tool`` —
        ownership lives on the Controller (Task 7), so every call site here
        (input dispatch, scene-cleared fan-out, …) reads the same source of
        truth a headless driver would.
        """
        return cast("_Tool | None", self._view.session.edit_controller.active_tool)

    @property
    def active_tool(self) -> _Tool | None:
        """The tool currently receiving delegated input events."""
        return self._active_tool

    @property
    def keymap(self) -> KeyMap:
        """The current head of the key-map chain (tool layer, else base)."""
        return self._tool_keymap or self._base_keymap

    @property
    def base_keymap(self) -> KeyMap:
        """The permanent base map (user-overrides store; below tool layers).

        For consumers that must outlive the active tool's ephemeral layer —
        the Preferences dialog builds display layers of its own on top.
        """
        return self._base_keymap

    @traced()
    def set_tool(self, tool: _Tool | None) -> None:
        """Swap the active tool (deactivate old, activate + register new).

        Ownership is the Controller's (:meth:`EditController.set_active_tool`);
        that call fires ``notify_active_tool_changed``, which
        :meth:`_on_active_tool_changed` (subscribed once, in ``__init__``)
        consumes to rebuild the KeyMap layer for the new tool.
        """
        view = self._view
        # While autoroute is running, mutating tools (Place / Route / Cut)
        # are not allowed — fall back to the SelectTool instance (which is
        # purely read-only).  SelectTool itself, or None, is always allowed.
        if tool is not None and view.session.autoroute_active:
            from protoboard.gui.tools.gui_tools import SelectTool as _ST

            if not isinstance(tool, _ST):
                tool = None
        if self._active_tool is not None:
            self._active_tool.deactivate()
        view.highlight.set_highlighted_net_silent(None)
        # Tool change may affect board_alpha (e.g. ghost mode) — rebuild board layer.
        view.mark_board_layer_dirty()
        if tool is not None:
            tool.activate(view.session, view)
        # Every concrete _Tool is a ControllerTool at runtime (composed as
        # ``(_XTool, ViewToolMixin)``); the controller-side API types its param
        # as ControllerTool and cannot import the gui-side _Tool protocol.
        view.session.edit_controller.set_active_tool(
            cast("ControllerTool | None", tool)
        )
        view.refresh()

    @traced()
    def _on_active_tool_changed(self) -> None:
        """Rebuild the tool KeyMap layer for the (now) active tool.

        Subscribed to ``EditorSession.active_tool_changed`` in ``__init__``;
        a headless session has no such signal to fire, so this only runs in
        the GUI.
        """
        tool = self._active_tool
        self._tool_keymap = (
            tool.make_key_map(self._base_keymap) if tool is not None else None
        )

    @traced()
    def detach_active_tool(self) -> None:
        """Deactivate + drop the active tool without a refresh (session swap)."""
        if self._active_tool is not None:
            self._active_tool.deactivate()
            self._view.session.edit_controller.set_active_tool(None)
            self._tool_keymap = None  # pop its layer

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
    def perform_context_action(self, option: ActionOption) -> None:
        """Execute a chosen context option (qt-ports §2.4, variant 1).

        A parameterised option (non-empty ``data``) goes to the active
        tool's ``perform_context_action``; a plain one runs the active
        tool's command for ``option.action_id`` through the Controller
        facade (:meth:`EditController.dispatch_tool_command`) — no
        synthesised key.
        """
        tool = self._active_tool
        if option.data and tool is not None:
            tool.perform_context_action(option.action_id, option.data)
            return
        self._view.session.edit_controller.dispatch_tool_command(option.action_id)

    @traced()
    def get_context_at(self, scene_pos: Point, view_pos: Point) -> InteractionContext:
        """The actions executable at *scene_pos* right now (qt-ports §2.4).

        A pure query of state — equally usable by the RMB publication, a
        test (which simulates the choice with ``dispatch_tool_command``) or an
        interactive CLI.  The generic canvas context carries the active
        tool's own actions with their live shortcuts; the consumer adds its
        view chrome (center-here / cancel) itself.
        """
        options: list[ActionOption] = []
        tool = self._active_tool
        if tool is not None:
            action_ids = tool.displayable_action_ids()
            if action_ids:
                group = category_label(action_ids[0]) + " actions"
                options = [
                    ActionOption(
                        action_id=row.action_id,
                        label=row.label,
                        shortcut_hint=row.shortcut,
                        group=group,
                    )
                    for row in tool_shortcut_rows(self.keymap, action_ids)
                ]
        return InteractionContext(
            scene_pos=scene_pos,
            view_pos=view_pos,
            hit=None,
            selection=(),
            actions=tuple(options),
            kind="canvas",
        )

    # -- [?] shortcut-help overlay -----------------------------------------

    @traced()
    def _shortcuts_overlay_visible(self) -> bool:
        return self._view.help_overlay.help_visible

    @traced()
    def _toggle_shortcuts_overlay(self) -> None:
        if self._shortcuts_overlay_visible():
            self._hide_shortcuts_overlay()
        else:
            self._show_shortcuts_overlay()

    @traced()
    def _hide_shortcuts_overlay(self) -> None:
        self._view.help_overlay.hide_help()

    def set_keybindings_provider(self, provider: Callable[[], object | None]) -> None:
        """Inject the live ``KeyBindings`` source (MainWindow owns it)."""
        self._keybindings_provider = provider

    def _app_keybindings(self) -> object | None:
        """The live ``KeyBindings`` table (with user overrides), or None.

        None ⇒ ``app_shortcut_sections`` falls back to the shipped defaults.
        """
        return self._keybindings_provider()

    @traced()
    def _show_shortcuts_overlay(self) -> None:
        """Float a panel listing the globally + tool-relevant shortcuts.

        Reads the live :class:`KeyMap`, so the panel always reflects the user's
        current bindings.  Global rows first, then the active tool's actions.
        """
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
                sections.append(
                    (
                        category_label(tool_ids[0]),
                        list(tool_shortcut_rows(self.keymap, tool_ids)),
                    )
                )

        self._view.help_overlay.show_help(self._render_shortcuts_html(sections))

    @staticmethod
    def _render_shortcuts_html(sections: list[tuple[str, list[object]]]) -> str:
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

    def _dispatch_mouse_chord(
        self,
        chord: str,
        event: QMouseEvent | QWheelEvent,
        scene_pt: QPointF | None,
    ) -> bool:
        """Offer a mouse chord to every action bound to it, leaf-first.

        Tool-layer handlers (``tool.mouse_dispatch``) shadow the base map's
        view gestures; a declining handler lets the chord fall through to the
        next bound action — "unhandled returns to the BoardView".  Tool
        handlers receive the seam DTO; the base map's own view gestures
        (pan / menu / camera zoom) keep the raw Qt event.
        """
        tool = self._active_tool
        handlers = dict(tool.mouse_dispatch()) if tool is not None else {}
        inp: PointerInput | WheelInput | None = None
        if handlers:
            if isinstance(event, QMouseEvent) and scene_pt is not None:
                inp = pointer_input(event, from_qpointf(scene_pt))
            elif isinstance(event, QWheelEvent):
                inp = wheel_input(event)
        for action_id in self.keymap.mouse_lookup(chord):
            handler = handlers.get(action_id)
            if handler is not None and inp is not None and handler(inp):
                return True
            if self._view_mouse_action(action_id, event, scene_pt):
                return True
        return False

    def _view_mouse_action(
        self,
        action_id: str,
        event: QMouseEvent | QWheelEvent,
        scene_pt: QPointF | None,
    ) -> bool:
        """Run one of the base map's view gestures; True when it consumed."""
        view = self._view
        if action_id == VIEW_PAN and isinstance(event, QMouseEvent):
            ctrl_variant = event.button() == Qt.MouseButton.LeftButton
            if ctrl_variant and scene_pt is not None:
                # Ctrl+LMB keeps the tool's first refusal (component drag);
                # a consuming tool suppresses the pan.
                tool = self._active_tool
                if tool is not None and tool.on_press(
                    pointer_input(event, from_qpointf(scene_pt))
                ):
                    return True
            self._ctrl_panning = ctrl_variant
            self._panning = True
            self._pan_start = event.position()
            view.viewport_port.set_cursor(CursorKind.CLOSED_HAND)
            return True
        if action_id == VIEW_CANVAS_MENU and isinstance(event, QMouseEvent):
            assert scene_pt is not None
            pos = event.position()
            view.notify_context(
                self.get_context_at(
                    from_qpointf(scene_pt),
                    Point(pos.x(), pos.y()),
                )
            )
            return True
        if action_id in (VIEW_ZOOM_IN, VIEW_ZOOM_OUT) and isinstance(
            event, QWheelEvent
        ):
            view.camera.on_wheel(wheel_input(event))
            return True
        return False

    def handle_mouse_press(self, event: QMouseEvent) -> bool:
        view = self._view
        scene_pt = to_qpointf(
            view.viewport_port.map_view_to_scene(from_qpointf(event.position()))
        )
        chord = mouse_button_chord(event.button(), event.modifiers())
        if chord is not None and self._dispatch_mouse_chord(chord, event, scene_pt):
            return True
        # Not a bound gesture — the press belongs to the tool's input stream.
        if self._active_tool is not None:
            point = from_qpointf(scene_pt)
            if event.button() == Qt.MouseButton.LeftButton:
                # The primary click is the default_action verb (docs MVC step 1).
                self._active_tool.default_action(point, mods_from_qt(event.modifiers()))
            else:
                # Other buttons stay on the raw hook (no verb maps to them yet).
                self._active_tool.on_press(pointer_input(event, point))
            return True
        return False  # fall through to QGraphicsView

    def handle_wheel(self, event: QWheelEvent) -> None:
        """Route a wheel tick through the KeyMap; unbound chords keep zooming.

        The bound action only *selects* the handler — the handler receives the
        raw event, so sub-detent accumulation (armed rotate, smooth zoom)
        works exactly as before.
        """
        chord = wheel_chord(event.angleDelta().y(), event.modifiers())
        if chord is not None and self._dispatch_mouse_chord(chord, event, None):
            event.accept()
            return
        # Legacy fallback: modified / horizontal wheels keep their historic
        # camera behaviour even though no action is bound to them.
        self._view.camera.on_wheel(wheel_input(event))

    def handle_mouse_move(self, event: QMouseEvent) -> bool:
        view = self._view
        if self._panning:
            delta = event.position() - self._pan_start
            self._pan_start = event.position()
            view.viewport_port.pan_by(Vector(-delta.x(), -delta.y()))
        scene_pos = view.viewport_port.map_view_to_scene(from_qpointf(event.position()))
        view.notify_cursor_moved(scene_pos)
        if not self._panning and self._active_tool is not None:
            inp = pointer_input(event, scene_pos)
            self._active_tool.on_move(inp)
            # Throttled hover verb alongside the live on_move: the Controller's
            # enriched reply (what is under the cursor) is published for the
            # highlight consumer without firing per raw move.
            self._hover_throttle.note((scene_pos, inp.mods))
        elif not self._panning:
            return False  # fall through to QGraphicsView
        return True

    def _fire_hover(self, parked: tuple[Point, Mods]) -> None:
        """Deliver a throttled hover to the active tool and publish the reply."""
        tool = self._active_tool
        if tool is None:
            return
        point, mods = parked
        ev = replace(tool.empty_action(point, mods), mods=mods)
        self._view.notify_hover(ev)

    def handle_mouse_release(self, event: QMouseEvent) -> bool:
        view = self._view
        if event.button() == Qt.MouseButton.MiddleButton and self._panning:
            self._panning = False
            view.viewport_port.set_cursor(CursorKind.ARROW)
        elif event.button() == Qt.MouseButton.LeftButton and self._ctrl_panning:
            self._panning = False
            self._ctrl_panning = False
            view.viewport_port.set_cursor(CursorKind.ARROW)
        elif self._active_tool is not None:
            scene_pos = view.viewport_port.map_view_to_scene(
                from_qpointf(event.position())
            )
            self._active_tool.on_release(pointer_input(event, scene_pos))
        else:
            return False  # fall through to QGraphicsView
        return True

    def handle_double_click(self, event: QMouseEvent) -> None:
        view = self._view
        left = Qt.MouseButton.LeftButton
        if event.button() == left and self._active_tool is not None:
            scene_pos = view.viewport_port.map_view_to_scene(
                from_qpointf(event.position())
            )
            handler = getattr(self._active_tool, "on_double_press", None)
            if handler is not None:
                handler(scene_pos)

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
            and tool.wants_key(key_input(event))
        )

    def handle_key_press(self, event: QKeyEvent) -> None:
        view = self._view
        if event.isAutoRepeat():
            event.ignore()
            return
        key = event.key()
        inp = key_input(event)
        # ``?`` toggles the shortcut-help overlay (resolved through the KeyMap,
        # so a rebind moves it).  Esc closes it before anything else.
        if self.keymap.matches(key, inp.native_mods, "global.action.show_shortcuts"):
            self._toggle_shortcuts_overlay()
            event.accept()
            return
        if key == Qt.Key.Key_Escape:
            if self._shortcuts_overlay_visible():
                self._hide_shortcuts_overlay()
                event.accept()
                return
            consumed = False
            tool = self._active_tool
            # Two-stage Esc: evaluate can_exit() BEFORE dispatching the key —
            # the tool's on_key cancels a pending operation (clearing the very
            # state can_exit() reads), so checking afterwards would always see
            # "nothing pending" and exit on every Esc. A tool with a pending op
            # reports can_exit()=False here, so the cancel lands and the tool
            # stays active; only once nothing is pending does the (idle)
            # default can_exit()=True let this Esc exit — unchanged
            # single-press exit for idle tools.
            can_exit = tool is None or tool.can_exit()
            if tool is not None:
                consumed = tool.on_key(inp)
            if can_exit:
                view.escape_to_select()
            if consumed:
                event.accept()
            return
        # An armed tool (float-rotate / keyboard-move) claims arrows for
        # stepping — give it first crack BEFORE the arrow-scroll fallback, else
        # the view scrolls instead.  Gated on wants_key (the tool decides by
        # action id), so an idle tool still scrolls on arrows as before.
        tool = self._active_tool
        if tool is not None and tool.wants_key(inp):
            # The tool claimed this key (via wants_key / key_claims), so it OWNS
            # the dispatch: hand it to on_key once and stop.  Returning only on a
            # truthy on_key would fall through to the unconditional second
            # on_key below, double-dispatching every op whose handler is
            # ``-> None`` (e.g. wire-edit flip) — one physical [F] fired twice,
            # net-zero.  A claimed key is consumed whether or not on_key reports
            # a truthy "consumed" value.
            tool.on_key(inp)
            event.accept()
            return

        # Arrow keys — scroll by one raster unit (only without modifier).
        if not event.modifiers() and key in (
            Qt.Key.Key_Left,
            Qt.Key.Key_Right,
            Qt.Key.Key_Up,
            Qt.Key.Key_Down,
        ):
            # Convert 1 raster unit to viewport pixels using current transform.
            t = view.viewport_port.view_transform()
            px = float(max(int(t.a), 1))
            py = float(max(int(t.d), 1))
            step = {
                Qt.Key.Key_Left: Vector(-px, 0.0),
                Qt.Key.Key_Right: Vector(px, 0.0),
                Qt.Key.Key_Up: Vector(0.0, -py),
                Qt.Key.Key_Down: Vector(0.0, py),
            }[Qt.Key(key)]
            view.viewport_port.pan_by(step)
            return
        if self._active_tool is not None and self._active_tool.on_key(inp):
            # Consumed exclusively by the tool — accept so the key neither
            # reaches Qt's fall-through nor double-dispatches a global action.
            event.accept()
        # An unconsumed key stays ignored: Qt propagates it to the parent
        # widgets natively (global WindowShortcut QActions were already
        # offered the key on ShortcutOverride).  The old explicit
        # ``window().keyPressEvent(event)`` forward was a no-op — MainWindow's
        # keyPressEvent is a plain passthrough — and is gone.

    def handle_key_release(self, event: QKeyEvent) -> None:
        if self._active_tool is not None:
            self._active_tool.on_key_release(key_input(event))
