# SPDX-License-Identifier: MIT
"""Small wall-time measurement utility.

Two pieces:

* :class:`Stopwatch` — starts on construction, ``elapsed()`` returns an
  :class:`Elapsed` snapshot of the current interval, ``reset()`` rebases
  the start moment so the next ``elapsed()`` is relative to the reset.
* :class:`Elapsed` — frozen value object carrying ``ms`` (float),
  ``label`` (free-form tag, e.g. ``"build_bg_cache"``) and ``start_time``
  (the ``time.monotonic()`` value at the start of the measured interval,
  useful when ordering events from independent stopwatches).

Used by perf-instrumented code paths (paint pipeline, refresh loop,
autoroute snapshot) to avoid the repeated ``_t0 = time.monotonic()``
boilerplate and to surface a label-aware result.  ``time.monotonic`` is
deliberate — never wall-clock, never CPU time, never ``perf_counter``;
the same clock the rest of the codebase already uses for paint/refresh
timings so the numbers are comparable.

The class is intentionally tiny.  Resist the temptation to turn it into
a profiler — multi-phase / histogram / percentile metrics belong in a
separate utility.

Follow-up: making this awaitable (so an async task can ``await sw`` and
wake when a deadline elapses) is plausible but out of scope here.  The
sync API is small enough that an async one can wrap it without disruption.
"""

from __future__ import annotations

import time
from dataclasses import dataclass


@dataclass(frozen=True, slots=True)
class Elapsed:
    """Snapshot of one stopwatch interval.

    ``ms`` is the duration since the stopwatch's start (or last reset),
    ``label`` is the same string carried by the Stopwatch, and
    ``start_time`` is the ``time.monotonic()`` value at interval start.
    """

    ms: float
    label: str
    start_time: float

    def __str__(self) -> str:  # log-friendly
        return f"{self.label}: {self.ms:.1f} ms" if self.label else f"{self.ms:.1f} ms"


class Stopwatch:
    """Single-shot interval timer.

    Constructed-and-started — no separate ``start()`` step::

        sw = Stopwatch("build_bg")
        ... work ...
        result = sw.elapsed()
        # result.ms, result.label == "build_bg"

    Calling ``elapsed()`` multiple times keeps the same start moment.
    Use ``reset()`` to rebase the start; the next ``elapsed()`` then
    measures from that point.
    """

    __slots__ = ("_label", "_start")

    def __init__(self, label: str = "") -> None:
        self._label = label
        self._start = time.monotonic()

    @property
    def label(self) -> str:
        return self._label

    @property
    def start_time(self) -> float:
        return self._start

    def elapsed(self) -> Elapsed:
        """Return an :class:`Elapsed` snapshot relative to the current start."""
        now = time.monotonic()
        return Elapsed(
            ms=(now - self._start) * 1000.0,
            label=self._label,
            start_time=self._start,
        )

    def reset(self) -> None:
        """Rebase the start moment to *now*."""
        self._start = time.monotonic()


__all__ = ["Stopwatch", "Elapsed"]
