# SPDX-License-Identifier: MIT
"""Seam between the board canvas and its interaction tools.

``AbstractBoardView`` declares the surface of the board canvas that tools and
extracted services are allowed to depend on — nothing outside this interface
is sanctioned (the old "plus what ``QGraphicsView`` provides" escape hatch is
gone with the Protocol swap).  ``BoardViewTool``
is the base class every canvas tool implements: the Qt input-event overrides
stay on the concrete ``BoardView`` (Qt requires real overrides) and are
*delegated* to the active tool through the ``on_*`` hooks declared here.
"""

from __future__ import annotations

import uuid
from abc import ABC, abstractmethod
from typing import TYPE_CHECKING, Protocol, override

from protoboard.services.debug_trace import traced

if TYPE_CHECKING:
    from collections.abc import Callable, Mapping

    from PySide6.QtWidgets import QGraphicsScene

    from protoboard.geometry import Point
    from protoboard.gui.ports import (
        HelpOverlayPort,
        InteractionContext,
        KeyInput,
        PointerInput,
        StatusEvent,
        ViewportPort,
        WheelInput,
    )
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


class AbstractBoardView(Protocol):
    """The board-canvas surface tools and extracted services may call.

    A structural :class:`typing.Protocol` (qt-ports §4): the concrete
    ``BoardView(QGraphicsView)`` satisfies it without inheriting it — the
    sanctioned surface finally *equals* the real surface, because the
    protocol simply does not have ``horizontalScrollBar()``.  The two
    deliberate Qt touchpoints are ``scene()`` (D1: tools' transient items
    draw into a real ``QGraphicsScene``; a headless double may hold one with
    no widget) and ``window()`` (dialog parenting only — D4 tracked debt).
    """

    # Composed services — the extracted BoardView collaborators (declared as
    # typed attributes so services can reach each other through this surface).
    highlight: HighlightRenderer
    paint: PaintPipeline
    camera: ViewCamera
    tools: ToolHost
    composer: SceneComposer
    # The camera *mechanism* (qt-ports §2.1) — ViewCamera and services speak
    # to the widget's transform/scroll/mapping only through this port.
    viewport_port: ViewportPort
    # The [?] shortcut-help surface (qt-ports §2.5).
    help_overlay: HelpOverlayPort

    # -- view state ----------------------------------------------------------

    @property
    def camera_side(self) -> str:
        """Which board face the camera looks at: ``"top"`` | ``"bottom"``."""

    @property
    def keymap(self) -> KeyMap:
        """The namespaced keybindings map tools resolve their actions in."""

    @property
    def session(self) -> EditorSession:
        """The editor session this canvas currently renders."""

    @property
    def theme(self) -> Theme:
        """The resolved theme (Preferences override or the default)."""

    @property
    def active_tool(self) -> BoardViewTool | None:
        """The tool currently receiving delegated input events."""

    @property
    def antialiasing(self) -> bool:
        """Whether render passes (scene + cached pixmaps) antialias."""

    def layer_state(self) -> LayerState:
        """The resolved per-layer visibility/alpha state for this paint."""

    def mirror_flags(self) -> tuple[bool, bool]:
        """(mirror_h, mirror_v) of the current view transform."""

    def in_transform_burst(self) -> bool:
        """True while a wheel-zoom or pan-scroll burst is in flight."""

    # -- scene-item registries (read-only; consumed by extracted services) ----

    def component_groups(self) -> Mapping[uuid.UUID, object]:
        """comp_id → its persistent ``QGraphicsItemGroup`` (or stand-in)."""

    def component_label_groups(self) -> Mapping[uuid.UUID, object]:
        """comp_id → its label ``QGraphicsItemGroup`` (refdes / pin names)."""

    def wire_groups(self) -> Mapping[uuid.UUID, object]:
        """wire_id → its persistent ``QGraphicsItemGroup`` (or stand-in)."""

    def invalidate_back_cache(self) -> None:
        """Drop the rasterised back-side pixmap; next paint rebuilds it."""

    def invalidate_bg_cache(self, *, keep_anchor: bool = False) -> None:
        """Mark the board pixmap stale (*keep_anchor* preserves the
        stale-blit anchor for pan/zoom/resize bursts)."""

    def mark_board_layer_dirty(self) -> None:
        """Force a board-layer rebuild on the next refresh()."""

    def rebuild_pad_cache(self) -> None:
        """Rebuild the pad-position list + spatial snap index from the board."""

    @property
    def layer_overrides(self) -> LayerOverrides:
        """User per-layer visibility overrides (Layers panel state)."""

    def apply_label_visibility(self, *, deferred: bool = False) -> None:
        """Re-evaluate label visibility for the current zoom.

        ``deferred=True`` debounces the O(scene items) sweep behind the
        zoom-refresh timer instead of running it synchronously.
        """

    @property
    def active_group_id(self) -> uuid.UUID:
        """The group whose context drives rendering (set by SelectTool)."""

    # -- geometry / hit helpers ----------------------------------------------

    def snap(
        self, scene_pt: Point, exclude_ids: frozenset[uuid.UUID] | None = None
    ) -> Point:
        """Snap *scene_pt* to the nearest board pad centre."""

    def is_locked_pin(self, scene_pt: Point, tol: float = 0.6) -> bool:
        """True when *scene_pt* hits a pin of a locked component."""

    def board_pad_positions(self) -> list[Point]:
        """All pad centres of the current board (cached)."""

    # -- scene / camera -------------------------------------------------------

    def refresh(self) -> None:
        """Rebuild the scene from the session."""

    def center_on_scene_point(self, scene_pt: Point) -> None:
        """Scroll so *scene_pt* lands in the viewport centre."""

    def set_active_group(self, group_id: uuid.UUID) -> None:
        """Set the group whose context the renderer highlights."""

    def trigger_tool_action(self, action_id: str) -> None:
        """Dispatch *action_id* to the active tool as a synthesised key press."""

    # -- highlight / overlay API ----------------------------------------------

    def set_highlighted_net(self, net_id: uuid.UUID | None) -> None:
        """Highlight *net_id* (dim everything else); ``None`` clears."""

    def set_clicked_free_pad(self, pos: Point | None) -> None:
        """Show the click-feedback ring on a free (net-less) pad."""

    def set_wire_drag_state(self, states: dict[uuid.UUID, str]) -> None:
        """Apply per-wire drag ghosting (``"drop" | "disconnect" | "normal"``)."""

    def set_selection_outline_override(self, color: str | None) -> None:
        """Override the selection-outline colour (drag-conflict signalling).

        *color* is hex ``#AARRGGBB`` — the format :class:`Theme` already
        uses; the QColor conversion happens Qt-side in the highlight renderer.
        """

    # -- notification channel (docs/qt-ports.md §2.3) --------------------------
    # The view surface *publishes* notifications; the consumer (MainWindow, or
    # a test double that merely records) decides what to do with them.  Tools
    # never touch ``window()``/``statusBar()`` directly.

    def notify_status(self, event: StatusEvent) -> None:
        """Publish a status notification (structural payload, see StatusEvent)."""

    def notify_cursor_moved(self, pos: Point) -> None:
        """Publish the cursor's raster (scene) position; fires per mouse move."""

    def notify_context(self, ctx: InteractionContext) -> None:
        """Publish an interaction context (RMB) for the consumer to render."""

    def escape_to_select(self) -> None:
        """Ask the application to leave the current tool for Select."""

    # -- sanctioned Qt touchpoints (see class docstring) -----------------------

    def scene(self) -> QGraphicsScene:
        """The graphics scene tools draw transient items into (D1)."""
        ...

    def window(self) -> object | None:
        """Top-level widget for *dialog parenting only* (D4 tracked debt)."""
        ...


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
    def on_key(self, inp: KeyInput) -> bool:
        """Handle a key press.  Return True when the key was consumed."""

    @abstractmethod
    def on_key_release(self, inp: KeyInput) -> None: ...

    @abstractmethod
    def key_claims(self) -> tuple[tuple[str, Callable[[], bool]], ...]:
        """``(action_id, state_gate)`` pairs driving the default ``wants_key``.

        The tool claims a key *right now* when any pair's action id matches the
        event through the KeyMap **and** its gate returns True (e.g. "the
        armed transform is live", "a wire-edit fragment is selected").  Purely
        declarative — no ``Qt.Key`` literals, rebinds honoured automatically.
        """

    @abstractmethod
    def wants_key(self, inp: KeyInput) -> bool:
        """True when the tool wants to handle *inp* itself this very moment.

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
    def _key_matches(self, inp: KeyInput, action_id: str) -> bool:
        """True when *inp* equals *action_id*'s currently-resolved shortcut.

        Resolves through the view's :class:`KeyMap`, so a tool dispatches and
        vetoes keys by *action id* — never by hard-coded ``Qt.Key`` literals.
        Returns False pre-activate / headless (no view).
        """
        view = self._view
        if view is None:
            return False
        return view.keymap.matches(inp.key, inp.native_mods, action_id)

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
    def on_key(self, inp: KeyInput) -> bool:
        return False

    @override
    @traced()
    def on_key_release(self, inp: KeyInput) -> None:
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
    def wants_key(self, inp: KeyInput) -> bool:
        """True when the tool wants to handle *inp* itself this very moment.

        Consulted on a Qt ``ShortcutOverride`` so a tool can veto a global
        application shortcut whose key it needs contextually right now (e.g. a
        wire-edit fragment claiming ``[W]`` / ``[A]`` / ``Delete`` from the
        Route / Library / Delete actions).  Vetoing makes Qt deliver the key as
        an ordinary ``KeyPress`` to ``on_key`` instead of firing the QAction.
        Derived from :meth:`key_claims` — a tool states *what* it claims and
        *when*; the matching itself is uniform (KeyMap, rebind-safe).
        """
        return any(
            gate() and self._key_matches(inp, action_id)
            for action_id, gate in self.key_claims()
        )

    @traced()
    def key_dispatch(self) -> tuple[tuple[str, Callable[[KeyInput], bool]], ...]:
        """Ordered ``(action_id, handler)`` table for :meth:`_dispatch_key`.

        A handler returns True when it consumed the key.  Default: empty —
        tools opt in per action.
        """
        return ()

    @traced()
    def _dispatch_key(self, inp: KeyInput) -> bool:
        """Run *inp* through :meth:`key_dispatch` (KeyMap-resolved, ordered).

        Mirrors an ``elif`` ladder: the first entry whose action id matches
        the key decides — its handler's verdict is final, later entries are
        not offered the key even when the handler declines.
        """
        for action_id, handler in self.key_dispatch():
            if self._key_matches(inp, action_id):
                return handler(inp)
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
