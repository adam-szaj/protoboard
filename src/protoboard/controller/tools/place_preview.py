# SPDX-License-Identifier: MIT
"""``PlacePreview`` — the structural seam for PlaceTool's ghost + reach overlay.

The Qt-free ``Protocol`` PlaceTool depends on.  The Qt implementation
(``QtPlacePreview``, owning the ``QGraphicsItem`` mechanism) lives in
:mod:`protoboard.gui.tools.place_preview` — see the qt-ports §3 pattern
described there.
"""

from __future__ import annotations

from typing import TYPE_CHECKING, Protocol

if TYPE_CHECKING:
    from protoboard.controller.view_options.theme import Theme
    from protoboard.domain.components.definition import ComponentDefinition
    from protoboard.geometry import Point, Vector


class PlacePreview(Protocol):
    """What PlaceTool shows while a component is being placed."""

    def show_ghost(
        self,
        defn: ComponentDefinition,
        *,
        rotation: int,
        pin_offsets: dict[int, Vector],
        package_variant: str | None,
        theme: Theme,
    ) -> None:
        """(Re)build the ghost body for the current transform (at 0,0)."""
        ...

    def move_ghost(self, pos: Point) -> None: ...

    def clear_ghost(self) -> None: ...

    @property
    def has_ghost(self) -> bool: ...

    def show_reach_dots(self, points: list[Point]) -> None:
        """Mark the pads the active drag pin may land on (replaces previous)."""
        ...

    def show_reach_line(self, a: Point, b: Point) -> None:
        """Dashed feedback line reach-origin→cursor (replaces the previous)."""
        ...

    def clear_reach_line(self) -> None:
        """Drop the feedback line only (the reach dots stay)."""
        ...

    def clear_reach(self) -> None:
        """Drop the reach dots and the feedback line."""
        ...

    def on_scene_cleared(self) -> None:
        """Drop stale item refs after ``scene.clear()`` (C++ objects gone)."""
        ...
