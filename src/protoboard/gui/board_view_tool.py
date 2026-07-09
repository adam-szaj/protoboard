# SPDX-License-Identifier: MIT
"""Seam between the board canvas and its interaction tools.

``AbstractBoardView`` declares the surface of the board canvas that tools and
extracted services are allowed to depend on — nothing outside this interface
(plus what ``QGraphicsView`` itself provides) is sanctioned.  ``BoardViewTool``
is the base class every canvas tool implements: the Qt input-event overrides
stay on the concrete ``BoardView`` (Qt requires real overrides) and are
*delegated* to the active tool through the ``on_*`` hooks declared here.
"""

from __future__ import annotations

import uuid
from abc import ABC, ABCMeta, abstractmethod
from typing import TYPE_CHECKING, override

from PySide6.QtCore import QPointF
from PySide6.QtGui import QKeyEvent
from PySide6.QtWidgets import QGraphicsView

from protoboard.services.debug_trace import traced

if TYPE_CHECKING:
    from collections.abc import Callable, Mapping

    from protoboard.geometry import Point
    from protoboard.gui.ports import PointerInput, StatusEvent, WheelInput
    from protoboard.gui.highlight.renderer import HighlightRenderer
    from protoboard.gui.keymap import AbstractKeyMap, KeyMap
    from protoboard.gui.layer_state import LayerState
    from protoboard.gui.layer_state import LayerOverrides
    from protoboard.gui.paint_pipeline import PaintPipeline
    from protoboard.gui.scene_composer import SceneComposer
    from protoboard.gui.session import EditorSession
    from protoboard.gui.theme import Theme
    from protoboard.gui.tool_host import ToolHost
    from protoboard.gui.view_camera import ViewCamera


if TYPE_CHECKING:
    # Static checkers can't follow the dynamic base ``type(QGraphicsView)``
    # (pyright degrades the whole class hierarchy) — declare the plain-ABCMeta
    # equivalent for them and keep the real merged metaclass runtime-only.
    class QtABCMeta(ABCMeta):
        """Static stand-in for the runtime Shiboken+ABC metaclass."""
else:
    class QtABCMeta(type(QGraphicsView), ABCMeta):
        """Shiboken's Qt metaclass merged with :class:`abc.ABCMeta`.

        ``class X(QGraphicsView, ABC)`` raises a metaclass conflict because Qt
        widgets carry Shiboken's own metaclass; deriving from both resolves it.

        Note: Shiboken's instantiation path does **not** run the ABC
        ``__abstractmethods__`` check, so instantiating an abstract Qt class is
        not blocked at runtime — the contract is enforced statically
        (mypy / pyright) only.
        """


class AbstractBoardView(QGraphicsView, metaclass=QtABCMeta):
    """The board-canvas surface tools and extracted services may call.

    Everything a tool needs beyond plain :class:`QGraphicsView` behaviour
    (``scene()``, ``window()``, ``mapToScene`` …) must be declared here.
    """

    # Composed services — the extracted BoardView collaborators (declared as
    # typed attributes so services can reach each other through this surface).
    highlight: HighlightRenderer
    paint: PaintPipeline
    camera: ViewCamera
    tools: ToolHost
    composer: SceneComposer

    # -- view state ----------------------------------------------------------

    @property
    @abstractmethod
    def camera_side(self) -> str:
        """Which board face the camera looks at: ``"top"`` | ``"bottom"``."""

    @property
    @abstractmethod
    def keymap(self) -> KeyMap:
        """The namespaced keybindings map tools resolve their actions in."""

    @property
    @abstractmethod
    def session(self) -> EditorSession:
        """The editor session this canvas currently renders."""

    @property
    @abstractmethod
    def theme(self) -> Theme:
        """The resolved theme (Preferences override or the default)."""

    @property
    @abstractmethod
    def active_tool(self) -> BoardViewTool | None:
        """The tool currently receiving delegated input events."""

    @property
    @abstractmethod
    def antialiasing(self) -> bool:
        """Whether render passes (scene + cached pixmaps) antialias."""

    @abstractmethod
    def layer_state(self) -> LayerState:
        """The resolved per-layer visibility/alpha state for this paint."""

    @abstractmethod
    def mirror_flags(self) -> tuple[bool, bool]:
        """(mirror_h, mirror_v) of the current view transform."""

    @abstractmethod
    def in_transform_burst(self) -> bool:
        """True while a wheel-zoom or pan-scroll burst is in flight."""

    # -- scene-item registries (read-only; consumed by extracted services) ----

    @abstractmethod
    def component_groups(self) -> Mapping[uuid.UUID, object]:
        """comp_id → its persistent ``QGraphicsItemGroup`` (or stand-in)."""

    @abstractmethod
    def component_label_groups(self) -> Mapping[uuid.UUID, object]:
        """comp_id → its label ``QGraphicsItemGroup`` (refdes / pin names)."""

    @abstractmethod
    def wire_groups(self) -> Mapping[uuid.UUID, object]:
        """wire_id → its persistent ``QGraphicsItemGroup`` (or stand-in)."""

    @abstractmethod
    def invalidate_back_cache(self) -> None:
        """Drop the rasterised back-side pixmap; next paint rebuilds it."""

    @abstractmethod
    def invalidate_bg_cache(self, *, keep_anchor: bool = False) -> None:
        """Mark the board pixmap stale (*keep_anchor* preserves the
        stale-blit anchor for pan/zoom/resize bursts)."""

    @abstractmethod
    def mark_board_layer_dirty(self) -> None:
        """Force a board-layer rebuild on the next refresh()."""

    @abstractmethod
    def rebuild_pad_cache(self) -> None:
        """Rebuild the pad-position list + spatial snap index from the board."""

    @property
    @abstractmethod
    def layer_overrides(self) -> LayerOverrides:
        """User per-layer visibility overrides (Layers panel state)."""

    @abstractmethod
    def apply_label_visibility(self, *, deferred: bool = False) -> None:
        """Re-evaluate label visibility for the current zoom.

        ``deferred=True`` debounces the O(scene items) sweep behind the
        zoom-refresh timer instead of running it synchronously.
        """

    @property
    @abstractmethod
    def active_group_id(self) -> uuid.UUID:
        """The group whose context drives rendering (set by SelectTool)."""

    # -- geometry / hit helpers ----------------------------------------------

    @abstractmethod
    def snap(
        self, scene_pt: Point, exclude_ids: frozenset[uuid.UUID] | None = None
    ) -> Point:
        """Snap *scene_pt* to the nearest board pad centre."""

    @abstractmethod
    def is_locked_pin(self, scene_pt: Point, tol: float = 0.6) -> bool:
        """True when *scene_pt* hits a pin of a locked component."""

    @abstractmethod
    def board_pad_positions(self) -> list[Point]:
        """All pad centres of the current board (cached)."""

    # -- scene / camera -------------------------------------------------------

    @abstractmethod
    def refresh(self) -> None:
        """Rebuild the scene from the session."""

    @abstractmethod
    def center_on_scene_point(self, scene_pt: Point) -> None:
        """Scroll so *scene_pt* lands in the viewport centre."""

    @abstractmethod
    def set_active_group(self, group_id: uuid.UUID) -> None:
        """Set the group whose context the renderer highlights."""

    @abstractmethod
    def trigger_tool_action(self, action_id: str) -> None:
        """Dispatch *action_id* to the active tool as a synthesised key press."""

    # -- highlight / overlay API ----------------------------------------------

    @abstractmethod
    def set_highlighted_net(self, net_id: uuid.UUID | None) -> None:
        """Highlight *net_id* (dim everything else); ``None`` clears."""

    @abstractmethod
    def set_clicked_free_pad(self, pos: Point | None) -> None:
        """Show the click-feedback ring on a free (net-less) pad."""

    @abstractmethod
    def set_wire_drag_state(self, states: dict[uuid.UUID, str]) -> None:
        """Apply per-wire drag ghosting (``"drop" | "disconnect" | "normal"``)."""

    @abstractmethod
    def set_selection_outline_override(self, color: str | None) -> None:
        """Override the selection-outline colour (drag-conflict signalling).

        *color* is hex ``#AARRGGBB`` — the format :class:`Theme` already
        uses; the QColor conversion happens Qt-side in the highlight renderer.
        """

    # -- notification channel (docs/qt-ports.md §2.3) --------------------------
    # The view surface *publishes* notifications; the consumer (MainWindow, or
    # a test double that merely records) decides what to do with them.  Tools
    # never touch ``window()``/``statusBar()`` directly.

    @abstractmethod
    def notify_status(self, event: StatusEvent) -> None:
        """Publish a status notification (structural payload, see StatusEvent)."""

    @abstractmethod
    def notify_cursor_moved(self, pos: Point) -> None:
        """Publish the cursor's raster (scene) position; fires per mouse move."""

    @abstractmethod
    def escape_to_select(self) -> None:
        """Ask the application to leave the current tool for Select."""


class BoardViewTool(ABC):
    """Base class for board-view interaction tools.

    ``BoardView`` keeps the Qt event overrides and forwards input to the
    active tool through these hooks — mouse events arrive together with the
    mapped scene position, keys are vetoed/resolved through the view's
    :class:`~protoboard.gui.keymap.KeyMap`.
    """

    @abstractmethod
    def activate(self, session: EditorSession, view: AbstractBoardView) -> None:
        """Bind the tool to *session*/*view*; called when it becomes active."""

    @abstractmethod
    def deactivate(self) -> None:
        """Drop session/view bindings; called when another tool takes over."""

    @abstractmethod
    def on_press(self, inp: PointerInput) -> bool:
        """Handle mouse press. Return True if the event was consumed."""

    @abstractmethod
    def on_move(self, inp: PointerInput) -> None: ...

    @abstractmethod
    def on_release(self, inp: PointerInput) -> None: ...

    @abstractmethod
    def on_key(self, event: QKeyEvent) -> None: ...

    @abstractmethod
    def on_key_release(self, event: QKeyEvent) -> None: ...

    @abstractmethod
    def key_claims(self) -> tuple[tuple[str, Callable[[], bool]], ...]:
        """``(action_id, state_gate)`` pairs driving the default ``wants_key``.

        The tool claims a key *right now* when any pair's action id matches the
        event through the KeyMap **and** its gate returns True (e.g. "the
        armed transform is live", "a wire-edit fragment is selected").  Purely
        declarative — no ``Qt.Key`` literals, rebinds honoured automatically.
        """

    @abstractmethod
    def wants_key(self, event: QKeyEvent) -> bool:
        """True when the tool wants to handle *event* itself this very moment.

        Consulted on a Qt ``ShortcutOverride`` so a tool can veto a global
        application shortcut whose key it needs contextually right now.
        Default: derived from :meth:`key_claims`.
        """

    @abstractmethod
    def key_actions(self) -> dict[str, str | None]:
        """The keyboard actions this tool declares (namespaced keybindings)."""

    @abstractmethod
    def mouse_actions(self) -> dict[str, str | None]:
        """The mouse-chord actions this tool declares (``"WheelUp"``, ``"RMB"``,
        ``"Ctrl+LMB"``, …).  Registered into the tool's key-map layer alongside
        :meth:`key_actions`; a declaration shadows the base map's view gesture
        (zoom / pan / canvas menu) for the span of the activation."""

    @abstractmethod
    def mouse_dispatch(self) -> tuple[
        tuple[str, Callable[[PointerInput | WheelInput], bool]],
        ...,
    ]:
        """Ordered ``(action_id, handler)`` table for mouse-chord dispatch.

        Unlike the key table, dispatch walks *all* actions bound to the chord
        (leaf-first): a declining handler lets the gesture fall through to the
        base map's view action — that is the "unhandled returns to the
        BoardView" rule.  A handler gets the seam input DTO (``delta_y``
        accumulation stays with the consumer; a pointer DTO carries its own
        scene position).
        """

    @abstractmethod
    def make_key_map(self, parent: AbstractKeyMap) -> KeyMap:
        """Build this tool's key-map layer chained onto *parent*.

        Called by :class:`~protoboard.gui.tool_host.ToolHost` on activate; the
        layer is dropped on deactivate (ephemeral — nothing to unregister).
        The default implementation registers :meth:`key_actions` into a fresh
        layer sharing *parent*'s user-overrides store.
        """

    @abstractmethod
    def displayable_action_ids(self) -> list[str]:
        """Ordered action ids to surface in the ``[?]`` overlay / RMB menu.

        Default: the tool's own declared actions (:meth:`key_actions`).  A tool
        can override to add the inherited generic globals it also responds to.
        """

    @abstractmethod
    def on_scene_cleared(self) -> None:
        """Called after scene.clear() so tool can drop stale item references."""


class _Tool(BoardViewTool):
    """Do-nothing default implementation every concrete tool subclasses."""

    def __init__(self) -> None:
        # Bound on activate(); set to None up-front so that pre-activate code
        # paths (e.g. MainWindow applies user preferences to a freshly built
        # SelectTool) can ``if self._session is not None`` without AttributeError.
        # Type is narrowed to the real EditorSession after activate(); existing
        # callers all access these only via the event-handler paths that run
        # *after* activate().
        self._session: EditorSession = None  # type: ignore[assignment]
        self._view: AbstractBoardView = None  # type: ignore[assignment]

    def __json_repr__(self) -> str:
        return str(type(self))

    @override
    @traced()
    def activate(self, session: EditorSession, view: AbstractBoardView) -> None:
        self._session = session
        self._view = view

    @override
    @traced()
    def deactivate(self) -> None:
        self._session = None  # type: ignore[assignment]
        self._view = None  # type: ignore[assignment]

    @traced()
    def _key_matches(self, event: QKeyEvent, action_id: str) -> bool:
        """True when *event* equals *action_id*'s currently-resolved shortcut.

        Resolves through the view's :class:`KeyMap`, so a tool dispatches and
        vetoes keys by *action id* — never by hard-coded ``Qt.Key`` literals.
        Returns False pre-activate / headless (no view).
        """
        view = self._view
        if view is None:
            return False
        return view.keymap.matches(event.key(), event.modifiers(), action_id)

    def _theme(self) -> Theme:
        from protoboard.gui.theme import DEFAULT_THEME, Theme
        t = getattr(self._view, "_theme_override", None)
        return t if isinstance(t, Theme) else DEFAULT_THEME

    @override
    @traced()
    def on_press(self, inp: PointerInput) -> bool:
        """Handle mouse press. Return True if the event was consumed."""
        return False

    @override
    @traced()
    def on_move(self, inp: PointerInput) -> None:
        pass

    @override
    @traced()
    def on_release(self, inp: PointerInput) -> None:
        pass

    @override
    @traced()
    def on_key(self, event: QKeyEvent) -> None:
        pass

    @override
    @traced()
    def on_key_release(self, event: QKeyEvent) -> None:
        pass

    @override
    @traced()
    def key_claims(self) -> tuple[tuple[str, Callable[[], bool]], ...]:
        """``(action_id, state_gate)`` pairs driving the default ``wants_key``.

        Default: no claims.
        """
        return ()

    @override
    @traced()
    def wants_key(self, event: QKeyEvent) -> bool:
        """True when the tool wants to handle *event* itself this very moment.

        Consulted on a Qt ``ShortcutOverride`` so a tool can veto a global
        application shortcut whose key it needs contextually right now (e.g. a
        wire-edit fragment claiming ``[W]`` / ``[A]`` / ``Delete`` from the
        Route / Library / Delete actions).  Vetoing makes Qt deliver the key as
        an ordinary ``KeyPress`` to ``on_key`` instead of firing the QAction.
        Derived from :meth:`key_claims` — a tool states *what* it claims and
        *when*; the matching itself is uniform (KeyMap, rebind-safe).
        """
        return any(
            gate() and self._key_matches(event, action_id)
            for action_id, gate in self.key_claims()
        )

    @traced()
    def key_dispatch(self) -> tuple[tuple[str, Callable[[QKeyEvent], bool]], ...]:
        """Ordered ``(action_id, handler)`` table for :meth:`_dispatch_key`.

        A handler returns True when it consumed the key (the event is then
        accepted).  Default: empty — tools opt in per action.
        """
        return ()

    @traced()
    def _dispatch_key(self, event: QKeyEvent) -> bool:
        """Run *event* through :meth:`key_dispatch` (KeyMap-resolved, ordered).

        Mirrors an ``elif`` ladder: the first entry whose action id matches
        the event decides — its handler's verdict is final, later entries are
        not offered the key even when the handler declines.
        """
        for action_id, handler in self.key_dispatch():
            if self._key_matches(event, action_id):
                consumed = handler(event)
                if consumed:
                    event.accept()
                return consumed
        return False

    @override
    @traced()
    def mouse_actions(self) -> dict[str, str | None]:
        """The mouse-chord actions this tool declares.  Default: none."""
        return {}

    @override
    @traced()
    def mouse_dispatch(self) -> tuple[
        tuple[str, Callable[[PointerInput | WheelInput], bool]],
        ...,
    ]:
        """Ordered ``(action_id, handler)`` mouse table.  Default: empty."""
        return ()

    @override
    @traced()
    def key_actions(self) -> dict[str, str | None]:
        """The keyboard actions this tool declares (namespaced keybindings).

        Maps ``"<category>.action.<name>" -> shortcut | None``: a literal
        shortcut overrides for this tool, ``None`` inherits the matching generic
        ``global.action.<name>`` default.  Registered into the view's
        :class:`~protoboard.gui.keymap.KeyMap` on :meth:`activate`, and consulted
        (dynamically, by current binding) for the shortcut veto.  Default: none.
        """
        return {}

    @override
    @traced()
    def make_key_map(self, parent: AbstractKeyMap) -> KeyMap:
        """Build this tool's key-map layer chained onto *parent*.

        Registers :meth:`key_actions` + :meth:`mouse_actions` into a fresh
        layer sharing *parent*'s user-overrides store.  Best-effort per entry:
        a declaration colliding with a ``final`` action's trigger is logged
        and skipped rather than crashing the tool switch.
        """
        from protoboard.gui.keymap import FinalShortcutError, KeyMap
        km = KeyMap.layer_over(parent)
        for action_id, shortcut in {**self.key_actions(),
                                    **self.mouse_actions()}.items():
            try:
                km.register_tool_actions({action_id: shortcut})
            except FinalShortcutError as exc:
                import structlog
                structlog.get_logger(__name__).warning(
                    "keymap.tool_action_rejected", error=str(exc)
                )
        return km

    @override
    @traced()
    def displayable_action_ids(self) -> list[str]:
        """Ordered action ids to surface in the ``[?]`` overlay / RMB menu.

        Default: the tool's own declared actions (:meth:`key_actions`).  A tool
        can override to add the inherited generic globals it also responds to.
        """
        return list(self.key_actions().keys())

    @override
    @traced()
    def on_scene_cleared(self) -> None:
        """Called after scene.clear() so tool can drop stale item references."""
        pass
