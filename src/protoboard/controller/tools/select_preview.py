# SPDX-License-Identifier: MIT
"""``SelectPreview`` — the structural seam for SelectTool's transient overlays.

The Qt-free ``Protocol`` SelectTool depends on, covering the five overlay
concepts SelectTool draws (armed-rotate pivot markers, the rubber band, the
re-drag landing overlay, the wire-edit fragment overlay, the reroute waypoint
preview).  The Qt implementation (``QtSelectPreview``, owning the
``QGraphicsItem`` mechanism) lives in :mod:`protoboard.gui.tools.select_preview`
— see the qt-ports §3 pattern described there.
"""

from __future__ import annotations

from typing import TYPE_CHECKING, Protocol

if TYPE_CHECKING:
    from collections.abc import Collection

    from protoboard.geometry import Point


class SelectPreview(Protocol):
    """The transient overlays SelectTool shows (semantic surface)."""

    # armed-rotate pivot markers
    def show_pivot(self, pos: Point, *, shared: bool) -> None:
        """Add one ⊕ pivot marker (shared = bigger/brighter)."""
        ...

    def clear_pivots(self) -> None: ...

    # rubber band
    def show_band(self, a: Point, b: Point, ltr: bool) -> None:
        """Show/update the rubber band (ltr picks the solid-vs-dash style)."""
        ...

    def clear_band(self) -> None: ...

    # re-drag landing overlay
    def show_redrag_pad(self, pt: Point) -> None:
        """Add one green landing-pad dot."""
        ...

    def show_reach_disc(self, centre: Point, radius: float) -> None:
        """Outline the grabbed pin's reach disc."""
        ...

    def show_redrag_live(self, pos: Point, ok: bool) -> None:
        """Validity tint under the cursor: green ring, or red ring + X cross
        (the cross is the non-colour accessibility cue).  Replaces the
        previous marker."""
        ...

    def clear_redrag_live(self) -> None: ...

    def clear_redrag(self) -> None:
        """Drop the pads, the reach disc and the live marker."""
        ...

    # wire-edit fragment overlay
    def show_wire_edit_wire(
        self,
        path: list[Point],
        marked: Collection[int],
        span: list[Point] | None = None,
    ) -> None:
        """Add one wire's cyan segments + vertex dots (*marked* indices red),
        plus a bright thick polyline over *span* (the picked range's points)."""
        ...

    def clear_wire_edit(self) -> None: ...

    # reroute waypoint preview
    def show_reroute_path(self, points: list[Point]) -> None:
        """Dashed orange polyline through *points* (replaces the previous,
        including the live segment)."""
        ...

    def show_reroute_live(self, a: Point, b: Point) -> None:
        """Live anchor→cursor segment (replaces the previous)."""
        ...

    def clear_reroute_live(self) -> None: ...

    def clear_reroute(self) -> None: ...

    def on_scene_cleared(self) -> None:
        """Drop stale item refs after ``scene.clear()`` (C++ objects gone)."""
        ...
