# SPDX-License-Identifier: MIT
"""``RoutePreview`` — the structural seam for RouteTool's transient overlay.

The Qt-free ``Protocol`` RouteTool depends on.  The Qt implementation
(``QtRoutePreview``, owning the ``QGraphicsItem`` mechanism) lives in
:mod:`protoboard.gui.tools.route_preview` — see the qt-ports §3 pattern
described there.
"""

from __future__ import annotations

from typing import TYPE_CHECKING, Protocol

if TYPE_CHECKING:
    from protoboard.geometry import Point
    from protoboard.services.routing.astar import RoutingResult


class RoutePreview(Protocol):
    """What RouteTool shows while a wire is being pulled."""

    def add_segment(self, a: Point, b: Point) -> None:
        """Append one committed (clicked) segment to the preview polyline."""
        ...

    def clear_segments(self) -> None: ...

    def show_live(self, a: Point, b: Point) -> None:
        """Show the dashed last-point→cursor segment (replaces the previous)."""
        ...

    def clear_live(self) -> None: ...

    def show_hints(self, results: list[RoutingResult]) -> None:
        """Render the top-K routing hint paths (replaces previous hints)."""
        ...

    def clear_hints(self) -> None: ...

    def show_dim(self) -> None:
        """Dim the whole scene below the routed items (route-active mode)."""
        ...

    def clear_dim(self) -> None: ...

    def on_scene_cleared(self) -> None:
        """Drop stale item refs after ``scene.clear()`` (C++ objects gone)."""
        ...
