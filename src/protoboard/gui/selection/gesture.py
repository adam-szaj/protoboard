# SPDX-License-Identifier: MIT
"""``GestureController`` — the common lifecycle for a press-drag-release gesture.

SelectTool's left-button drag means two different things depending on *where*
the press landed (resolved by the hit-test):

* press on a selected / draggable entity → **drag** the selection,
* press on empty space → **rubber-band** select an area.

Both share the same *shape* — press arms a controller, moves drive it, release
commits or cancels it — but share no state and produce different outcomes (an
atomic move command vs. a selection mutation).  This protocol captures only that
shared shape, so the tool can route the three events uniformly into
``self._gesture`` without branching on which gesture it is.

The "did the gesture actually happen, or was it really a click?" threshold lives
**inside each controller** (drag: distance from the press point; band: rectangle
size) — they arm on different hit-test conditions, so each one owns its own
notion of "started".  :meth:`commit` returns ``False`` for a sub-threshold
gesture, which the tool treats as a plain click (a no-op for the gesture).

Pure protocol — no Qt.  Concrete controllers may hold Qt seams (overlays, the
view's ``snap``) injected at construction, but the protocol itself is Qt-free so
the tool can depend on it without importing either implementation.
"""

from __future__ import annotations

from typing import TYPE_CHECKING, Protocol, runtime_checkable

if TYPE_CHECKING:
    from protoboard.geometry import Point


@runtime_checkable
class GestureController(Protocol):
    """One in-flight press-drag-release gesture.

    Armed by the tool on press (by constructing a concrete controller), driven
    per mouse-move via :meth:`update`, and finished on release via
    :meth:`commit` or abandoned via :meth:`cancel`.
    """

    def update(self, scene_pt: Point) -> None:
        """Advance the gesture to the current cursor position (per move)."""
        ...

    def commit(self) -> bool:
        """Finish the gesture on release.

        Returns ``True`` if the gesture did something (a real drag / band) and
        ``False`` if it stayed below its own start threshold — i.e. the press
        and release were effectively a click.  A ``False`` return must leave the
        world exactly as it was before the gesture armed.
        """
        ...

    def cancel(self) -> None:
        """Abort the gesture, restoring any provisional state it applied."""
        ...
