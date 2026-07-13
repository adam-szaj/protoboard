# SPDX-License-Identifier: MIT
"""``HeadlessBoardView`` — the AbstractBoardView test double (qt-ports §4).

No realized QWidget, no event loop, no qtbot: a real ``QGraphicsScene`` (D1)
carries the tools' transient items, the viewport is the exact
:class:`CameraMath` affine model, and every notification is recorded for
structural assertions.  Satisfies the ``AbstractBoardView`` Protocol
structurally — pinned by ``_conformance`` at the bottom.
"""

from __future__ import annotations

import uuid
from typing import TYPE_CHECKING, cast

from protoboard.geometry import Point, Rect, Size, Transform2D, Vector
from protoboard.gui.layer_state import LayerOverrides, LayerState
from protoboard.controller.ports import (
    BlockingJob,
    ExecutorPort,
    HelpOverlayPort,
    HoverEvent,
    InteractionContext,
    StatusEvent,
    ViewportPort,
)
from protoboard.controller.ports.viewport import CursorKind, ScreenMetrics
from protoboard.controller.ports.viewport_details import CameraMath
from protoboard.controller.theme import DEFAULT_THEME, Theme
from protoboard.gui.tool_host import ToolHost

if TYPE_CHECKING:
    from PySide6.QtWidgets import QGraphicsScene

    from protoboard.gui.board_view_tool import (
        AbstractBoardView,
        BoardViewTool,
    )
    from protoboard.gui.highlight.renderer import HighlightRenderer
    from protoboard.gui.keymap import KeyMap
    from protoboard.gui.paint_pipeline import PaintPipeline
    from protoboard.gui.scene_composer import SceneComposer
    from protoboard.gui.session import EditorSession
    from protoboard.gui.view_camera import ViewCamera


class FakeViewport:
    """ViewportPort implemented by the pure CameraMath model."""

    def __init__(self) -> None:
        self.math = CameraMath(
            transform=Transform2D(60.0, 0.0, 0.0, 60.0),
            scroll=Vector(0.0, 0.0),
            viewport_size=Size(640.0, 480.0),
        )
        self.cursor: CursorKind = CursorKind.ARROW
        self.repaint_requests = 0

    def map_view_to_scene(self, view_pt: Point) -> Point:
        return self.math.map_view_to_scene(view_pt)

    def map_scene_to_view(self, scene_pt: Point) -> Point:
        return self.math.map_scene_to_view(scene_pt)

    def view_scale(self) -> float:
        return self.math.view_scale()

    def view_transform(self) -> Transform2D:
        return self.math.transform

    def set_view_transform(self, t: Transform2D) -> None:
        self.math.transform = t

    def viewport_rect(self) -> Rect:
        size = self.math.viewport_size
        return Rect(0.0, 0.0, size.width, size.height)

    def visible_scene_rect(self) -> Rect:
        return self.math.visible_scene_rect()

    def scroll_position(self) -> Vector:
        return self.math.scroll

    def set_scroll_position(self, v: Vector) -> None:
        self.math.scroll = v

    def pan_by(self, view_delta: Vector) -> None:
        self.math.pan_by(view_delta)

    def center_on(self, scene_pt: Point) -> None:
        self.math.center_on(scene_pt)

    def fit_in_view(self, scene_rect: Rect) -> None:
        self.math.fit_in_view(scene_rect)

    def request_repaint(self) -> None:
        self.repaint_requests += 1

    def set_cursor(self, cursor: CursorKind) -> None:
        self.cursor = cursor

    def screen_metrics(self) -> ScreenMetrics | None:
        return None  # headless — ViewCamera falls back to 96 dpi


class FakeHelpOverlay:
    """HelpOverlayPort recorder."""

    def __init__(self) -> None:
        self.html: str | None = None
        self._visible = False

    def show_help(self, html: str) -> None:
        self.html = html
        self._visible = True

    def hide_help(self) -> None:
        self._visible = False

    @property
    def help_visible(self) -> bool:
        return self._visible


class FakeExecutor:
    """ExecutorPort that runs the job synchronously on the calling thread.

    No worker, no dialog, no event loop: ``run_blocking`` invokes the job
    inline (progress callback is a no-op) and logs it in ``jobs``.  Set
    ``cancel_next`` to simulate the user cancelling the next job (the job
    fn is NOT run — matching a dialog cancelled before the worker finishes).
    A raising job propagates (loud test failure) instead of the production
    log-and-return-None.
    """

    def __init__(self) -> None:
        self.jobs: list[BlockingJob] = []
        self.cancel_next = False

    def run_blocking(self, job: BlockingJob) -> object | None:
        self.jobs.append(job)
        if self.cancel_next:
            self.cancel_next = False
            return None
        if job.with_progress:
            return job.fn(lambda current, total: None)
        return job.fn()


class _HighlightRecorder:
    """Records the highlight calls tools/services make through the seam."""

    def __init__(self) -> None:
        self.calls: list[tuple[str, object]] = []

    def __getattr__(self, name: str) -> object:
        def _record(*args: object, **kwargs: object) -> None:
            self.calls.append((name, args))
        return _record


class NotificationLog:
    """All seam notifications, recorded for structural assertions (§2.6)."""

    def __init__(self) -> None:
        self.status: list[StatusEvent] = []
        self.cursor: list[Point] = []
        self.contexts: list[InteractionContext] = []
        self.hovers: list[HoverEvent] = []
        self.controller_states: list[str] = []
        self.escapes: int = 0


class HeadlessBoardView:
    """Dict-backed AbstractBoardView double (no widget, no event loop)."""

    def __init__(self, session: EditorSession) -> None:
        from PySide6.QtWidgets import QGraphicsScene

        self._session = session
        self._scene = QGraphicsScene()
        self._theme: Theme = DEFAULT_THEME
        self._active_group_id: uuid.UUID
        from protoboard.domain.layout.layout import ROOT_GROUP_ID
        self._active_group_id = ROOT_GROUP_ID
        self._layer_overrides = LayerOverrides()
        self._pad_positions: list[Point] = []
        self.notifications = NotificationLog()
        self.refresh_count = 0
        self.dirty_flags: list[str] = []
        self.wire_drag_states: list[dict[uuid.UUID, str]] = []
        self.outline_overrides: list[str | None] = []
        self.clicked_free_pads: list[Point | None] = []

        # Annotated with the port protocols — Protocol attributes are
        # invariant, so the concrete fake types would fail conformance.
        self.viewport_port: ViewportPort = FakeViewport()
        self.help_overlay: HelpOverlayPort = FakeHelpOverlay()
        self.executor: ExecutorPort = FakeExecutor()
        self.highlight = cast("HighlightRenderer", _HighlightRecorder())
        self.paint = cast("PaintPipeline", _HighlightRecorder())
        self.composer = cast("SceneComposer", _HighlightRecorder())
        self.tools = ToolHost(cast("AbstractBoardView", self))
        from protoboard.gui.view_camera import ViewCamera as _VC
        self.camera: ViewCamera = _VC(cast("AbstractBoardView", self))

    # -- view state ----------------------------------------------------------

    @property
    def camera_side(self) -> str:
        return self.camera.camera_side

    @property
    def keymap(self) -> KeyMap:
        return self.tools.keymap

    @property
    def session(self) -> EditorSession:
        return self._session

    @property
    def theme(self) -> Theme:
        return self._theme

    @property
    def active_tool(self) -> BoardViewTool | None:
        return self.tools.active_tool

    @property
    def antialiasing(self) -> bool:
        return True

    def layer_state(self) -> LayerState:
        return LayerState()

    def mirror_flags(self) -> tuple[bool, bool]:
        return self.camera.mirror_flags()

    def in_transform_burst(self) -> bool:
        return self.camera.in_transform_burst()

    # -- scene-item registries -------------------------------------------------

    def component_groups(self) -> dict[uuid.UUID, object]:
        return {}

    def component_label_groups(self) -> dict[uuid.UUID, object]:
        return {}

    def wire_groups(self) -> dict[uuid.UUID, object]:
        return {}

    def invalidate_back_cache(self) -> None:
        self.dirty_flags.append("back_cache")

    def invalidate_bg_cache(self, *, keep_anchor: bool = False) -> None:
        self.dirty_flags.append("bg_cache")

    def mark_board_layer_dirty(self) -> None:
        self.dirty_flags.append("board_layer")

    def rebuild_pad_cache(self) -> None:
        self._pad_positions = [
            pos for _atom, pos in self._session.board.atoms()
        ]

    @property
    def layer_overrides(self) -> LayerOverrides:
        return self._layer_overrides

    def apply_label_visibility(self, *, deferred: bool = False) -> None:
        pass

    @property
    def active_group_id(self) -> uuid.UUID:
        return self._active_group_id

    # -- geometry / hit helpers -------------------------------------------------

    def snap(
        self, scene_pt: Point, exclude_ids: frozenset[uuid.UUID] | None = None
    ) -> Point:
        import math

        if not self._pad_positions:
            self.rebuild_pad_cache()
        best, best_d = None, math.inf
        for p in self._pad_positions:
            d = (p.x - scene_pt.x) ** 2 + (p.y - scene_pt.y) ** 2
            if d < best_d:
                best, best_d = p, d
        if best is not None:
            return best
        return Point(
            math.floor(scene_pt.x) + 0.5, math.floor(scene_pt.y) + 0.5
        )

    def is_locked_pin(self, scene_pt: Point, tol: float = 0.6) -> bool:
        return False

    def board_pad_positions(self) -> list[Point]:
        if not self._pad_positions:
            self.rebuild_pad_cache()
        return self._pad_positions

    # -- scene / camera ----------------------------------------------------------

    def refresh(self) -> None:
        self.refresh_count += 1

    def center_on_scene_point(self, scene_pt: Point) -> None:
        self.viewport_port.center_on(scene_pt)

    def set_active_group(self, group_id: uuid.UUID) -> None:
        self._active_group_id = group_id

    def perform_context_action(self, option: object) -> None:
        from protoboard.controller.ports import ActionOption

        assert isinstance(option, ActionOption)
        self.tools.perform_context_action(option)

    # -- highlight / overlay API ---------------------------------------------------

    def set_highlighted_net(self, net_id: uuid.UUID | None) -> None:
        pass

    def set_clicked_free_pad(self, pos: Point | None) -> None:
        self.clicked_free_pads.append(pos)

    def set_wire_drag_state(self, states: dict[uuid.UUID, str]) -> None:
        self.wire_drag_states.append(states)

    def set_selection_outline_override(self, color: str | None) -> None:
        self.outline_overrides.append(color)

    # -- notification channel ------------------------------------------------------

    def notify_status(self, event: StatusEvent) -> None:
        self.notifications.status.append(event)

    def notify_cursor_moved(self, pos: Point) -> None:
        self.notifications.cursor.append(pos)

    def notify_context(self, ctx: InteractionContext) -> None:
        self.notifications.contexts.append(ctx)

    def notify_hover(self, event: HoverEvent) -> None:
        self.notifications.hovers.append(event)

    def notify_controller_state(self, state: str) -> None:
        self.notifications.controller_states.append(state)

    def escape_to_select(self) -> None:
        self.notifications.escapes += 1

    # -- sanctioned Qt touchpoints ---------------------------------------------------

    def scene(self) -> QGraphicsScene:
        return self._scene

    def window(self) -> object | None:
        return None  # headless — dialogs get a None parent


if TYPE_CHECKING:
    def _conformance(view: HeadlessBoardView) -> AbstractBoardView:
        """Static-only: the double must satisfy the Protocol."""
        return view
