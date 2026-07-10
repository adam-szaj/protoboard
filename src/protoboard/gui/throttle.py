# SPDX-License-Identifier: MIT
"""Shared QTimer wrappers — one mechanism for every deferred/coalesced call.

The GUI drives a dozen QTimers with only a few distinct disciplines; the
construction boilerplate (``setSingleShot`` / ``setInterval`` /
``timeout.connect``) plus the arming rule was copy-pasted at every site.  Two
small classes capture the whole space (CLAUDE.md §6 — one concept, one class):

* :class:`TimerWrap` — a timer + its arming discipline.  ``arm()`` either
  *arms-if-idle* (trailing-edge debounce: fire ~interval after the FIRST event
  of a burst) or *restarts* (settle detector: fire ~interval after the LAST
  event), per ``restart_on_arm``.  ``single_shot=False`` gives a periodic poll.
  ``is_active`` / ``stop`` / ``set_interval`` cover the sites that read burst
  state, tear down, or reconfigure the interval live.
* :class:`ParkLatestThrottle` — a :class:`TimerWrap` that also *carries a
  value*: ``note(value)`` folds the sample into the parked one (replace by
  default; a custom ``combine`` sums, e.g. wheel-zoom deltas) and arms the
  timer; the trailing fire delivers the parked sample.

Callback-local state (visibility gates, pending flags) stays in the
caller-supplied closure — it is not timer state.
"""

from __future__ import annotations

from typing import TYPE_CHECKING, Generic, TypeVar

from PySide6.QtCore import QTimer

if TYPE_CHECKING:
    from collections.abc import Callable

T = TypeVar("T")


class TimerWrap:
    """A QTimer plus its arming discipline.

    ``restart_on_arm=False`` (default) → *arm-if-idle*: ``arm()`` starts the
    timer only when it is not already pending, so a burst fires once, ~interval
    after its first event (trailing-edge debounce).  ``restart_on_arm=True`` →
    *restart*: ``arm()`` restarts the pending timer, so it fires ~interval after
    the burst's last event (settle detector).  ``single_shot=False`` makes it a
    periodic poll that ``arm()`` starts once.
    """

    def __init__(
        self,
        interval_ms: int,
        on_timeout: Callable[[], None],
        *,
        restart_on_arm: bool = False,
        single_shot: bool = True,
    ) -> None:
        self._restart_on_arm = restart_on_arm
        self._timer = QTimer()
        self._timer.setSingleShot(single_shot)
        self._timer.setInterval(max(0, interval_ms))
        self._timer.timeout.connect(on_timeout)

    def arm(self) -> None:
        """Start the timer per the arming discipline."""
        if self._restart_on_arm or not self._timer.isActive():
            self._timer.start()

    def stop(self) -> None:
        """Cancel a pending fire (no-op if idle)."""
        self._timer.stop()

    def is_active(self) -> bool:
        """True while a fire is pending (burst-state read-out)."""
        return self._timer.isActive()

    def set_interval(self, interval_ms: int) -> None:
        """Update the interval (Preferences may change it live)."""
        self._timer.setInterval(max(0, interval_ms))

    def interval(self) -> int:
        """The current interval in milliseconds."""
        return self._timer.interval()


def _replace(_parked: T, incoming: T) -> T:
    """Default combine: the latest sample wins."""
    return incoming


class ParkLatestThrottle(Generic[T]):
    """A :class:`TimerWrap` that parks the latest sample and delivers it once.

    ``note(value)`` folds *value* into the parked sample via *combine* and arms
    the timer; on timeout the parked sample is read, cleared, and passed to
    *sink*.  A burst of ``note`` calls inside one interval yields a single
    trailing delivery of the combined sample.
    """

    def __init__(
        self,
        interval_ms: int,
        sink: Callable[[T], None],
        *,
        combine: Callable[[T, T], T] = _replace,
    ) -> None:
        self._sink = sink
        self._combine = combine
        self._parked: T | None = None
        self._timer = TimerWrap(interval_ms, self._flush)

    def note(self, value: T) -> None:
        """Fold *value* into the parked sample; arm the timer if idle."""
        self._parked = (
            value if self._parked is None
            else self._combine(self._parked, value)
        )
        self._timer.arm()

    def is_active(self) -> bool:
        """True while a trailing delivery is pending."""
        return self._timer.is_active()

    def set_interval(self, interval_ms: int) -> None:
        """Update the coalescing interval (Preferences may change it live)."""
        self._timer.set_interval(interval_ms)

    def _flush(self) -> None:
        parked = self._parked
        self._parked = None
        if parked is None:
            return
        self._sink(parked)
