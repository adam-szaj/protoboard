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
from typing import TYPE_CHECKING, Protocol, override, runtime_checkable

from protoboard.controller.tools.base import ControllerTool
from protoboard.services.debug_trace import traced

if TYPE_CHECKING:
    from collections.abc import Callable, Mapping

    from PySide6.QtWidgets import QGraphicsScene

    from protoboard.geometry import Point
    from protoboard.controller.ports import (
        ActionOption,
        ExecutorPort,
        HelpOverlayPort,
        HoverEvent,
        InteractionContext,
        KeyInput,
        Mods,
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
    from protoboard.controller.theme import Theme
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
    # Blocking background jobs (worker + modal progress) behind the seam.
    executor: ExecutorPort

    # -- view state ----------------------------------------------------------

    @property
    def camera_side(self) -> str:
        """Which board face the camera looks at: ``"top"`` | ``"bottom"``."""
        ...

    @property
    def keymap(self) -> KeyMap:
        """The namespaced keybindings map tools resolve their actions in."""
        ...

    @property
    def session(self) -> EditorSession:
        """The editor session this canvas currently renders."""
        ...

    @property
    def theme(self) -> Theme:
        """The resolved theme (Preferences override or the default)."""
        ...

    @property
    def active_tool(self) -> _Tool | None:
        """The tool currently receiving delegated input events.

        Typed as the structural :class:`_Tool` (the View's tool surface), not
        the ``BoardViewTool`` ABC: concrete tools compose ``(_XTool,
        ViewToolMixin)`` and satisfy ``_Tool`` by shape, not by descending
        from a single base.  ``BoardView.active_tool`` returns ``_Tool | None``
        for the same reason, so the seam types agree.
        """

    @property
    def antialiasing(self) -> bool:
        """Whether render passes (scene + cached pixmaps) antialias."""
        ...

    def layer_state(self) -> LayerState:
        """The resolved per-layer visibility/alpha state for this paint."""
        ...

    def mirror_flags(self) -> tuple[bool, bool]:
        """(mirror_h, mirror_v) of the current view transform."""
        ...

    def in_transform_burst(self) -> bool:
        """True while a wheel-zoom or pan-scroll burst is in flight."""
        ...

    # -- scene-item registries (read-only; consumed by extracted services) ----

    def component_groups(self) -> Mapping[uuid.UUID, object]:
        """comp_id → its persistent ``QGraphicsItemGroup`` (or stand-in)."""
        ...

    def component_label_groups(self) -> Mapping[uuid.UUID, object]:
        """comp_id → its label ``QGraphicsItemGroup`` (refdes / pin names)."""
        ...

    def wire_groups(self) -> Mapping[uuid.UUID, object]:
        """wire_id → its persistent ``QGraphicsItemGroup`` (or stand-in)."""
        ...

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
        ...

    def apply_label_visibility(self, *, deferred: bool = False) -> None:
        """Re-evaluate label visibility for the current zoom.

        ``deferred=True`` debounces the O(scene items) sweep behind the
        zoom-refresh timer instead of running it synchronously.
        """

    @property
    def active_group_id(self) -> uuid.UUID:
        """The group whose context drives rendering (set by SelectTool)."""
        ...

    # -- geometry / hit helpers ----------------------------------------------

    def snap(
        self, scene_pt: Point, exclude_ids: frozenset[uuid.UUID] | None = None
    ) -> Point:
        """Snap *scene_pt* to the nearest board pad centre."""
        ...

    def is_locked_pin(self, scene_pt: Point, tol: float = 0.6) -> bool:
        """True when *scene_pt* hits a pin of a locked component."""
        ...

    def board_pad_positions(self) -> list[Point]:
        """All pad centres of the current board (cached)."""
        ...

    # -- scene / camera -------------------------------------------------------

    def refresh(self) -> None:
        """Rebuild the scene from the session."""

    def center_on_scene_point(self, scene_pt: Point) -> None:
        """Scroll so *scene_pt* lands in the viewport centre."""

    def set_active_group(self, group_id: uuid.UUID) -> None:
        """Set the group whose context the renderer highlights."""

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

    def notify_hover(self, event: HoverEvent) -> None:
        """Publish a throttled hover reply (what is under the cursor)."""

    def notify_controller_state(self, state: str) -> None:
        """Publish an interaction-state change (e.g. ``"move.float.rotate"``)."""

    def perform_context_action(self, option: ActionOption) -> None:
        """Execute a chosen context option (qt-ports §2.4, variant 1).

        Plain options run the active tool's command for ``option.action_id``
        through the Controller facade (``EditController.dispatch_tool_
        command``); parameterised ones reach the active tool's
        ``perform_context_action(action_id, data)``.
        """

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

    @abstractmethod
    def perform_context_action(
        self, action_id: str, data: Mapping[str, object]
    ) -> None:
        """Execute a parameterised context-menu action this tool published.

        *data* is the ``ActionOption.data`` payload the tool itself built in
        its context query — the dispatch counterpart of the old QMenu
        data-tuple ladder.  Default: no-op.
        """


class ViewToolMixin:
    """Qt/View bindings a concrete GUI tool composes over :class:`ControllerTool`.

    Holds exactly the members that need the real ``BoardView``: the key-map
    layer builder (``gui.keymap``), the theme reader (``gui.theme``), and the
    input-translation bodies (``on_press``/``on_key``/``on_key_release``).
    """

    if TYPE_CHECKING:
        # Composed onto ControllerTool in every concrete tool (_Tool); declare
        # the borrowed members here so mypy checks this mixin's own bodies
        # against the composed shape without inheriting from ControllerTool.
        _view: AbstractBoardView

        def key_actions(self) -> dict[str, str | None]: ...
        def mouse_actions(self) -> dict[str, str | None]: ...

    @override
    @traced()
    def on_press(self, inp: PointerInput) -> bool:
        """Handle mouse press. Return True if the event was consumed."""
        return False

    @override
    @traced()
    def on_key(self, inp: KeyInput) -> bool:
        return False

    @override
    @traced()
    def on_key_release(self, inp: KeyInput) -> None:
        pass

    def _theme(self) -> Theme:
        from protoboard.controller.theme import DEFAULT_THEME, Theme
        t = getattr(self._view, "_theme_override", None)
        return t if isinstance(t, Theme) else DEFAULT_THEME

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


@runtime_checkable
class _Tool(Protocol):
    """Structural surface the View accepts as "a tool".

    The View (``set_tool``/``active_tool``/``permanent_tools``), ToolHost and
    the Preferences keybinding tabs annotate against *this* — never the
    concrete class.  It is a :class:`~typing.Protocol` so the real tools, which
    compose ``(_XTool, ViewToolMixin)`` in a deliberate order that does *not*
    nominally descend from a single concrete base (see the composition-order
    note in ``gui/tools/gui_tools.py``), still satisfy it by *shape*.  A plain
    ``class _Tool(...)`` forced nominal assignability and produced ~150 spurious
    ``set_tool(SelectTool())`` argument-type errors.

    ``make_key_map`` is declared LOOSE (``parent: object -> object``) to match
    what pyright *actually resolves* for a concrete tool.  Although every tool
    re-points ``make_key_map = ViewToolMixin.make_key_map`` at runtime, pyright
    ignores a class-body re-point of an inherited ABC method and keeps
    ``ControllerTool``'s ``object -> object`` as the static type (the concrete
    tools list ``_XTool`` — hence ``ControllerTool`` — before ``ViewToolMixin``
    in their MRO; see the composition-order note in ``gui/tools/gui_tools.py``).
    Declaring the refined ``AbstractKeyMap -> KeyMap`` here would make every
    concrete tool fail the structural check.

    ``@runtime_checkable`` keeps ``isinstance(tool, _Tool)`` working (the
    Preferences dialog relies on a structural guard).  It checks method
    *presence* only, which is all the guard needs.
    """

    def activate(self, session: EditorSession, view: AbstractBoardView) -> None: ...
    def deactivate(self) -> None: ...
    def on_press(self, inp: PointerInput) -> bool: ...
    def on_move(self, inp: PointerInput) -> None: ...
    def on_release(self, inp: PointerInput) -> None: ...
    def on_key(self, inp: KeyInput) -> bool: ...
    def on_key_release(self, inp: KeyInput) -> None: ...
    def key_claims(self) -> tuple[tuple[str, Callable[[], bool]], ...]: ...
    def wants_key(self, inp: KeyInput) -> bool: ...
    def key_actions(self) -> dict[str, str | None]: ...
    def mouse_actions(self) -> dict[str, str | None]: ...
    def mouse_dispatch(self) -> tuple[
        tuple[str, Callable[[PointerInput | WheelInput], bool]],
        ...,
    ]: ...
    def make_key_map(self, parent: object) -> object: ...
    def displayable_action_ids(self) -> list[str]: ...
    def on_scene_cleared(self) -> None: ...
    def perform_context_action(
        self, action_id: str, data: Mapping[str, object]
    ) -> None: ...


class _DefaultTool(ControllerTool, ViewToolMixin, BoardViewTool):
    """Do-nothing default implementation test doubles subclass.

    Concrete GUI tools do *not* descend from this — they compose
    ``(_XTool, ViewToolMixin)`` (see ``gui/tools/gui_tools.py``).  This base
    exists only so the handful of test tools that want a ready-made "does
    nothing" tool can subclass and instantiate one; production annotations use
    the :class:`_Tool` Protocol above.

    The base order mirrors the concrete tools' ``(_XTool, ViewToolMixin)`` —
    ``ControllerTool`` (the base ``_XTool``s inherit) first, ``ViewToolMixin``
    second — so this stand-in resolves the same *static* member signatures a
    real tool does (notably the loose ``make_key_map``), and therefore
    satisfies the :class:`_Tool` Protocol the same way.  The real
    ``ViewToolMixin`` implementations are re-pointed below exactly as
    ``gui/tools/gui_tools.py`` does, so runtime behaviour is unchanged.
    """

    make_key_map = ViewToolMixin.make_key_map
    _theme = ViewToolMixin._theme
    on_press = ViewToolMixin.on_press
    on_key = ViewToolMixin.on_key
    on_key_release = ViewToolMixin.on_key_release

    def __init__(self) -> None:
        # Bound on activate(); set to None up-front so that pre-activate code
        # paths (e.g. MainWindow applies user preferences to a freshly built
        # SelectTool) can ``if self._session is not None`` without AttributeError.
        # Type is narrowed to the real EditorSession after activate(); existing
        # callers all access these only via the event-handler paths that run
        # *after* activate().
        ControllerTool.__init__(self)
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
