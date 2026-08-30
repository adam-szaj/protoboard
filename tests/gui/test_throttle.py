# SPDX-License-Identifier: MIT
"""Unit tests for the shared ``ParkLatestThrottle`` helper.

Drive the coalescing logic directly (``_flush`` stands in for the single-shot
timeout) so no event loop is needed — the same park-latest behaviour the three
call sites (cursor label, hover, wheel-zoom) rely on.
"""

from __future__ import annotations

from protoboard.gui.throttle import ParkLatestThrottle, TimerWrap


def test_replace_latest_coalesces_to_most_recent() -> None:
    fired: list[int] = []
    t: ParkLatestThrottle[int] = ParkLatestThrottle(33, fired.append)
    t.note(1)
    t.note(2)
    t.note(3)
    t._flush()
    assert fired == [3]  # only the latest survives the burst


def test_flush_clears_parked_no_double_fire() -> None:
    fired: list[int] = []
    t: ParkLatestThrottle[int] = ParkLatestThrottle(33, fired.append)
    t.note(7)
    t._flush()
    t._flush()  # nothing parked → no second delivery
    assert fired == [7]


def test_combine_sum_accumulates_while_keeping_latest_pivot() -> None:
    # The wheel-zoom shape: accumulate the delta, keep the latest pivot.
    fired: list[tuple[int, str]] = []

    def combine(old: tuple[int, str], new: tuple[int, str]) -> tuple[int, str]:
        return (old[0] + new[0], new[1])

    t: ParkLatestThrottle[tuple[int, str]] = ParkLatestThrottle(
        16, fired.append, combine=combine
    )
    t.note((120, "a"))
    t.note((120, "b"))
    t.note((120, "c"))
    t._flush()
    assert fired == [(360, "c")]  # summed delta, latest pivot


def test_is_active_false_before_any_note() -> None:
    # Before arming, no delivery is pending. (A live timer's isActive after
    # note() needs a running event loop, exercised end-to-end elsewhere.)
    t: ParkLatestThrottle[int] = ParkLatestThrottle(33, lambda _v: None)
    assert t.is_active() is False


def test_set_interval_updates_timer() -> None:
    t: ParkLatestThrottle[int] = ParkLatestThrottle(33, lambda _v: None)
    t.set_interval(80)
    assert t._timer.interval() == 80


# --- TimerWrap -------------------------------------------------------------- #


def test_timer_wrap_defaults_single_shot_arm_if_idle() -> None:
    t = TimerWrap(50, lambda: None)
    assert t.is_active() is False
    assert t.interval() == 50


def test_timer_wrap_set_interval_and_stop() -> None:
    t = TimerWrap(50, lambda: None)
    t.set_interval(120)
    assert t.interval() == 120
    t.stop()  # idle stop is a no-op
    assert t.is_active() is False


def test_park_latest_delegates_to_timer_wrap() -> None:
    # The parked value survives a burst and one flush delivers it (the
    # TimerWrap underneath owns the arming; the flush is the delivery).
    fired: list[str] = []
    t: ParkLatestThrottle[str] = ParkLatestThrottle(33, fired.append)
    t.note("x")
    t.note("y")
    t._flush()
    assert fired == ["y"]
