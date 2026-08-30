# SPDX-License-Identifier: MIT
"""Qt-free interaction-tool base (the gui ViewToolMixin adds KeyMap/Theme/on_key)."""

from __future__ import annotations

from collections.abc import Callable, Mapping
from typing import TYPE_CHECKING, Protocol

from protoboard.services.debug_trace import traced

if TYPE_CHECKING:
    import uuid

    from protoboard.controller.editing import EditState
    from protoboard.controller.ports import (
        ExecutorPort,
        HoverEvent,
        InteractionContext,
        KeyInput,
        Mods,
        PointerInput,
        StatusEvent,
        WheelInput,
    )
    from protoboard.geometry import Point


class KeyResolver(Protocol):
    """The one method the tool base needs from the view's KeyMap."""

    def matches(self, key: int, mods: int, action_id: str) -> bool: ...


class _ToolHostProto(Protocol):
    """The slice of the view's tool host that controller tools drive.

    ``ControllerTool`` swaps/queries the active tool through here (e.g. a
    SelectTool arming a sub-tool). Typed ``object`` for the tool itself: the
    controller passes ``self`` or another tool and never inspects the value's
    concrete gui type — that keeps this Qt-free (the gui ``ToolHost`` satisfies
    it structurally).
    """

    @property
    def active_tool(self) -> object | None: ...
    def set_tool(self, tool: object | None) -> None: ...
    def get_context_at(
        self, scene_pos: Point, view_pos: Point
    ) -> InteractionContext: ...


class _ViewProto(Protocol):
    """The view surface ``ControllerTool`` (and its subclasses) depend on.

    Deliberately not ``AbstractBoardView`` (declared in ``gui`` — importing it
    here would make ``controller`` depend on ``gui``, which
    ``tests/unit/test_controller_no_gui_import.py`` forbids). Instead it
    re-declares exactly the members the controller tools call, using only
    types available in the controller layer. ``AbstractBoardView`` (gui)
    satisfies it structurally; a parity test
    (``tests/unit/test_view_proto_parity.py``) guards against drift.
    """

    keymap: KeyResolver
    executor: ExecutorPort
    tools: _ToolHostProto

    @property
    def camera_side(self) -> str: ...

    def snap(
        self,
        scene_pt: Point,
        exclude_ids: frozenset[uuid.UUID] | None = None,
    ) -> Point: ...
    def is_locked_pin(self, scene_pt: Point, tol: float = 0.6) -> bool: ...
    def board_pad_positions(self) -> list[Point]: ...
    def refresh(self) -> None: ...
    def set_active_group(self, group_id: uuid.UUID) -> None: ...
    def mark_board_layer_dirty(self) -> None: ...
    def notify_status(self, event: StatusEvent) -> None: ...
    def notify_context(self, ctx: InteractionContext) -> None: ...
    def notify_controller_state(self, state: str) -> None: ...
    def window(self) -> object | None: ...


class ControllerTool:
    """Do-nothing Qt-free tool base. The gui mixin adds make_key_map/_theme/on_key."""

    def __init__(self) -> None:
        # Bound on activate(); set to None up-front so that pre-activate code
        # paths (e.g. MainWindow applies user preferences to a freshly built
        # SelectTool) can ``if self._session is not None`` without AttributeError.
        # Type is narrowed to the real EditState after activate(); existing
        # callers all access these only via the event-handler paths that run
        # *after* activate().
        self._session: EditState = None  # type: ignore[assignment]
        self._view: _ViewProto | None = None

    def __json_repr__(self) -> str:
        return str(type(self))

    @traced()
    def activate(self, session: EditState, view: _ViewProto) -> None:
        self._session = session
        self._view = view

    @traced()
    def deactivate(self) -> None:
        self._session = None  # type: ignore[assignment]
        self._view = None

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

    def make_key_map(self, parent: object) -> object:
        """Build this tool's key-map layer chained onto *parent*.

        Qt-free default: headless has no KeyMap layer to build — return
        *parent* unchanged. ``ViewToolMixin`` overrides this to build the
        real View key-map layer.
        """
        return parent

    def _theme(self) -> object | None:
        """The resolved theme.

        Qt-free default: headless has no theme. Only Qt-rendering paths read
        it; ``ViewToolMixin`` overrides this to resolve the real View theme.
        """
        return None

    def can_exit(self) -> bool:
        """May ``[Esc]`` exit this tool now? Tools with a pending operation
        override to return False so the first Esc cancels the op (stay active)
        and only a later Esc exits. Default: always exitable."""
        return True

    @traced()
    def on_move(self, inp: PointerInput) -> None:
        pass

    @traced()
    def on_release(self, inp: PointerInput) -> None:
        pass

    # -- verb surface (docs MVC step 1) --------------------------------------
    #
    # The View translates Qt input (+ KeyMap chord resolution + throttling)
    # into these semantic verbs and hands them to the active tool.  The default
    # adapters delegate to the ``on_*`` hooks so introducing the vocabulary
    # changes no behaviour on its own (parity before swap, CLAUDE.md §8).

    @traced()
    def default_action(self, point: Point, mods: Mods) -> bool:
        """Primary action (LMB) — delegates to :meth:`on_press`."""
        return bool(self.on_press(self._synth_pointer(point, mods, primary=True)))  # type: ignore[attr-defined]

    @traced()
    def secondary_action(self, point: Point, mods: Mods) -> bool:
        """Context action (RMB).  Default: no item context (bare canvas)."""
        return False

    @traced()
    def empty_action(self, point: Point, mods: Mods) -> HoverEvent:
        """Hover/move — delegates to :meth:`on_move`, reports nothing hit."""
        from protoboard.controller.ports import HoverEvent

        self.on_move(self._synth_pointer(point, mods, primary=False))
        return HoverEvent(point=point)

    @property
    def state(self) -> str:
        """The current interaction state.  Default: idle, no sub-state."""
        return "idle"

    def _synth_pointer(
        self, point: Point, mods: Mods, *, primary: bool
    ) -> PointerInput:
        """Build a scene==view ``PointerInput`` for a verb→hook delegation.

        A verb carries only ``(point, mods)`` (the boundary vocabulary); the
        legacy ``on_*`` hooks still want a DTO, so we synthesise one whose
        ``button`` is the primary button for a click and ``0`` for a move.
        """
        from protoboard.controller.ports import LEFT_BUTTON, PointerInput

        return PointerInput(
            view_pos=point,
            scene_pos=point,
            button=LEFT_BUTTON if primary else 0,
            native_mods=0,
            mods=mods,
        )

    @traced()
    def key_claims(self) -> tuple[tuple[str, Callable[[], bool]], ...]:
        """``(action_id, state_gate)`` pairs driving the default ``wants_key``.

        Default: no claims.
        """
        return ()

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

    @traced()
    def mouse_actions(self) -> dict[str, str | None]:
        """The mouse-chord actions this tool declares.  Default: none."""
        return {}

    @traced()
    def mouse_dispatch(self) -> tuple[
        tuple[str, Callable[[PointerInput | WheelInput], bool]],
        ...,
    ]:
        """Ordered ``(action_id, handler)`` mouse table.  Default: empty."""
        return ()

    @traced()
    def key_actions(self) -> dict[str, str | None]:
        """The keyboard actions this tool declares (namespaced keybindings).

        Maps ``"<category>.action.<name>" -> shortcut | None``: a literal
        shortcut overrides for this tool, ``None`` inherits the matching generic
        ``global.action.<name>`` default.  Registered into the view's key-map
        on :meth:`activate`, and consulted (dynamically, by current binding)
        for the shortcut veto.  Default: none.
        """
        return {}

    @traced()
    def displayable_action_ids(self) -> list[str]:
        """Ordered action ids to surface in the ``[?]`` overlay / RMB menu.

        Default: the tool's own declared actions (:meth:`key_actions`).  A tool
        can override to add the inherited generic globals it also responds to.
        """
        return list(self.key_actions().keys())

    @traced()
    def command_table(self) -> dict[str, Callable[[], object]]:
        """``action_id -> zero-arg command`` — the data the Controller facade
        (:meth:`~protoboard.controller.editing.controller.EditController.
        tool_commands`) exposes and :meth:`~protoboard.controller.editing.
        controller.EditController.dispatch_tool_command` runs.  Default: no
        commands.  A tool overrides this to expose the same operations its
        ``key_dispatch()`` table (or dispatch branches) already name — the
        View resolves a key to an action id via the KeyMap and drives it
        through the controller instead of calling the tool directly.
        """
        return {}

    @traced()
    def on_scene_cleared(self) -> None:
        """Called after scene.clear() so tool can drop stale item references."""
        pass

    @traced()
    def perform_context_action(
        self, action_id: str, data: Mapping[str, object]
    ) -> None:
        """Execute a parameterised context action.  Default: no-op."""
        pass
