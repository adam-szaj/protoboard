# SPDX-License-Identifier: MIT
"""``ViewportPort`` — the board canvas' camera *mechanism* (qt-ports §2.1).

One port: mapping, transform, scroll and viewport rect are one physical
object and mathematically interdependent.  The split criterion (settled on
user feedback): *policy and decisions stay on the testable side; only
mechanism and data go behind the port.*  Semantic camera commands
(``zoom_by``, ``fit_board``, mirror toggles, view steps) live in
``ViewCamera`` — behind them sits exactly the math this port makes
headless-testable.

The Qt adapter is :class:`protoboard.gui.qt_viewport.QtViewport` — the only
QPointF/QTransform conversion site; the test fake implements the mapping
with exact :class:`~protoboard.gui.ports.viewport_details.CameraMath`
affine math.
"""

from __future__ import annotations

from dataclasses import dataclass
from enum import Enum
from typing import TYPE_CHECKING, Protocol

if TYPE_CHECKING:
    from protoboard.geometry import Point, Rect, Size, Transform2D, Vector


class CursorKind(Enum):
    """Only the cursor shapes the canvas actually uses."""

    ARROW = "arrow"
    CLOSED_HAND = "closed_hand"


@dataclass(frozen=True, slots=True)
class ScreenMetrics:
    """What the camera needs to know about the physical screen."""

    size_px: Size
    dpi: float


class ViewportPort(Protocol):
    """Dumb camera mechanism: get/set transform + scroll, map, frame, paint."""

    def map_view_to_scene(self, view_pt: Point) -> Point: ...

    def map_scene_to_view(self, scene_pt: Point) -> Point: ...

    def view_scale(self) -> float:
        """``|m11|`` — the zoom magnitude regardless of mirror signs."""
        ...

    def view_transform(self) -> Transform2D: ...

    def set_view_transform(self, t: Transform2D) -> None: ...

    def viewport_rect(self) -> Rect: ...

    def visible_scene_rect(self) -> Rect:
        """The scene-space rect currently visible (transform+scroll+size)."""
        ...

    def scroll_position(self) -> Vector: ...

    def set_scroll_position(self, v: Vector) -> None: ...

    def pan_by(self, view_delta: Vector) -> None:
        """Scroll the content by *view_delta* viewport pixels."""
        ...

    def center_on(self, scene_pt: Point) -> None: ...

    def fit_in_view(self, scene_rect: Rect) -> None:
        """Frame *scene_rect* (keep aspect ratio, preserve mirror signs)."""
        ...

    def request_repaint(self) -> None: ...

    def set_cursor(self, cursor: CursorKind) -> None: ...

    def screen_metrics(self) -> ScreenMetrics | None: ...
