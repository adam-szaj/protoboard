# SPDX-License-Identifier: MIT
"""Small concept classes for :mod:`protoboard.gui.tool_host`.

``HoverThrottle`` is the View-side throttle for the hover verb stream (docs
MVC step 1, §4 — "View owns throttling"): a fast pointer-move stream collapses
to at most one ``empty_action`` per interval so the Controller can hit-test
eagerly per tick without cost concerns.  It is one concept — the parked point
plus the coalescing timer — so it lives in one class (CLAUDE.md §6) rather than
as loose fields on ToolHost.
"""

from __future__ import annotations

from typing import TYPE_CHECKING

from PySide6.QtCore import QTimer

if TYPE_CHECKING:
    from collections.abc import Callable

    from protoboard.geometry import Point
    from protoboard.gui.ports import Mods


class HoverThrottle:
    """Coalesce a move stream to one ``(point, mods)`` fire per interval.

    ``note(point, mods)`` parks the latest sample and, if no fire is pending,
    arms a single-shot timer; on timeout the *most recent* parked sample is
    delivered to *sink*.  A burst of moves inside the interval therefore yields
    a single trailing fire — the classic park-latest + single-shot pattern the
    cursor-label throttle also uses.
    """

    def __init__(
        self,
        interval_ms: int,
        sink: Callable[[Point, Mods], None],
    ) -> None:
        self._sink = sink
        self._parked: tuple[Point, Mods] | None = None
        self._timer = QTimer()
        self._timer.setSingleShot(True)
        self._timer.setInterval(max(0, interval_ms))
        self._timer.timeout.connect(self._flush)

    def note(self, point: Point, mods: Mods) -> None:
        """Park *point*/*mods*; arm the timer if one is not already pending."""
        self._parked = (point, mods)
        if not self._timer.isActive():
            self._timer.start()

    def set_interval(self, interval_ms: int) -> None:
        """Update the coalescing interval (Preferences may change it live)."""
        self._timer.setInterval(max(0, interval_ms))

    def _flush(self) -> None:
        if self._parked is None:
            return
        point, mods = self._parked
        self._parked = None
        self._sink(point, mods)
