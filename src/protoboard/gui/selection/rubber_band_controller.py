# SPDX-License-Identifier: MIT
"""``RubberBandController`` — area-select gesture as a headless controller.

The other half of SelectTool's left-button drag (the part that does *not* grab a
selected entity): a press on empty space drags a rectangle, and on release every
component the rectangle catches becomes the selection.  Two catch modes,
matching the legacy tool and every CAD convention:

* **left-to-right** (release.x ≥ press.x) — only components *fully inside* the
  band,
* **right-to-left** — every component the band *touches*.

Like :class:`DragController` it implements the
:class:`~protoboard.gui.selection.mouse_input.MouseInputController` protocol and owns
its own start threshold (a minimum rectangle size — below it the press/release
is a click, not a band).  The geometry is pure; the only Qt seams are the live
rectangle overlay and the bbox lookup, injected as callables so the controller
is headless-testable.

Phase 3 lifts this verbatim out of ``SelectTool``'s inline band code so the
tool routes both gestures through one uniform ``update`` / ``commit`` /
``cancel`` path.
"""

from __future__ import annotations

import uuid
from typing import TYPE_CHECKING

from protoboard.geometry import Point
from protoboard.gui.selection.mouse_input import Intent, Mods

if TYPE_CHECKING:
    from collections.abc import Callable

    from protoboard.geometry import Rect

__all__ = ["BandHit", "RubberBandController"]

#: Below this rectangle size (raster units, either dimension) the gesture is a
#: click, not a band (legacy ``_BAND_MIN_SIZE``).
_BAND_MIN_SIZE = 0.1


if TYPE_CHECKING:
    BboxFn = Callable[[uuid.UUID], "Rect | None"]
    """``comp_id -> bbox`` — wraps ``session.component_bbox``."""
    DrawFn = Callable[[Point, Point, bool], None]
    """``(corner_a, corner_b, ltr) -> None`` — update the on-screen rectangle."""
    ClearFn = Callable[[], None]
    """Remove the on-screen rectangle."""
    SelectFn = Callable[[list[uuid.UUID]], None]
    """Commit the caught component ids as the new selection."""


class BandHit:
    """Result of a finished band — the caught component ids and the mode."""

    def __init__(self, comp_ids: list[uuid.UUID], *, ltr: bool) -> None:
        self.comp_ids = comp_ids
        self.ltr = ltr


class RubberBandController:
    """Area-select gesture over a set of component bounding boxes.

    Armed on press (construction) at the press point; :meth:`update` redraws the
    rectangle to the cursor; :meth:`commit` resolves the caught components and
    hands them to the ``select`` seam (returning ``True``) unless the rectangle
    never grew past the click threshold (``False``).
    """

    def __init__(
        self,
        *,
        press_pt: Point,
        comp_ids: list[uuid.UUID],
        bbox: BboxFn,
        draw: DrawFn | None = None,
        clear: ClearFn | None = None,
        select: SelectFn | None = None,
    ) -> None:
        self._press = press_pt
        self._comp_ids = comp_ids
        self._bbox = bbox
        self._draw = draw
        self._clear = clear
        self._select = select
        self._cur = press_pt
        self._last_hit: BandHit | None = None
        self._finished = False
        self._was_click = False

    @property
    def last_hit(self) -> BandHit | None:
        """The components caught by the most recent :meth:`commit` (or None)."""
        return self._last_hit

    # -- MouseInputController ------------------------------------------------

    def on_press(self, scene_pt: Point, mods: Mods) -> bool:
        """The band is armed on construction; further presses aren't part of it."""
        return False

    def on_click(self, scene_pt: Point, mods: Mods) -> bool:
        """The band is press-drag-release, not click-driven."""
        return False

    def on_double_click(self, scene_pt: Point, mods: Mods) -> bool:
        return False

    def on_move(self, scene_pt: Point, mods: Mods) -> None:
        self._cur = scene_pt
        if self._draw is not None:
            ltr = scene_pt.x >= self._press.x
            self._draw(self._press, scene_pt, ltr)

    def on_release(self, scene_pt: Point, mods: Mods) -> None:
        self._finished = True
        if self._clear is not None:
            self._clear()
        rx0, ry0 = min(self._press.x, self._cur.x), min(self._press.y, self._cur.y)
        rx1, ry1 = max(self._press.x, self._cur.x), max(self._press.y, self._cur.y)
        if (rx1 - rx0) < _BAND_MIN_SIZE and (ry1 - ry0) < _BAND_MIN_SIZE:
            self._was_click = True
            return  # a click, not a band
        ltr = self._cur.x >= self._press.x
        caught = self._catch(rx0, ry0, rx1, ry1, ltr)
        self._last_hit = BandHit(caught, ltr=ltr)
        if self._select is not None:
            self._select(caught)

    def on_event(self, intent: Intent) -> bool:
        """CANCEL aborts the band; CONFIRM is not used (release finalizes it)."""
        if self._finished:
            return False
        if intent is Intent.CANCEL:
            self.cancel()
            return True
        return False

    def cancel(self) -> None:
        self._finished = True
        if self._clear is not None:
            self._clear()
        self._last_hit = None

    @property
    def finished(self) -> bool:
        return self._finished

    @property
    def was_click(self) -> bool:
        """True when release fired below the band's minimum size (a plain click)."""
        return self._was_click

    # -- geometry ------------------------------------------------------------

    def _catch(
        self, rx0: float, ry0: float, rx1: float, ry1: float, ltr: bool
    ) -> list[uuid.UUID]:
        """Component ids the band catches — fully-inside (LTR) or touching (RTL)."""
        out: list[uuid.UUID] = []
        for cid in self._comp_ids:
            bb = self._bbox(cid)
            if bb is None:
                continue
            bx0, by0 = bb.x, bb.y
            bx1, by1 = bb.x + bb.width, bb.y + bb.height
            inside = bx0 >= rx0 and by0 >= ry0 and bx1 <= rx1 and by1 <= ry1
            intersects = bx0 < rx1 and bx1 > rx0 and by0 < ry1 and by1 > ry0
            if inside if ltr else intersects:
                out.append(cid)
        return out
