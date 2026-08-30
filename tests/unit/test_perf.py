# SPDX-License-Identifier: MIT
"""Tests for the Stopwatch / Elapsed helpers."""

from __future__ import annotations

import time

from protoboard.perf import Elapsed, Stopwatch


class TestStopwatch:
    def test_constructed_and_started(self) -> None:
        sw = Stopwatch("phase")
        # Sleep a bit; elapsed must be positive and reflect at least the sleep.
        time.sleep(0.005)
        e = sw.elapsed()
        assert isinstance(e, Elapsed)
        assert e.label == "phase"
        assert e.ms >= 4.0  # 5 ms sleep; allow slack for short-sleep precision

    def test_multiple_elapsed_share_start(self) -> None:
        sw = Stopwatch()
        e1 = sw.elapsed()
        time.sleep(0.005)
        e2 = sw.elapsed()
        assert e1.start_time == e2.start_time
        assert e2.ms > e1.ms

    def test_reset_rebases_start(self) -> None:
        sw = Stopwatch("x")
        time.sleep(0.005)
        first_start = sw.start_time
        sw.reset()
        # New start is later than the original.
        assert sw.start_time > first_start
        e = sw.elapsed()
        # Elapsed since reset must be much smaller than the 5 ms sleep
        # that happened before reset (loose upper bound to dodge CI flakes).
        assert e.ms < 4.0

    def test_default_label_is_empty(self) -> None:
        sw = Stopwatch()
        e = sw.elapsed()
        assert e.label == ""

    def test_str_with_label(self) -> None:
        e = Elapsed(ms=12.34, label="build_bg", start_time=0.0)
        assert "build_bg" in str(e)
        assert "12.3" in str(e)

    def test_str_without_label(self) -> None:
        e = Elapsed(ms=12.34, label="", start_time=0.0)
        # No leading "label: " when label is empty.
        assert str(e).startswith("12.3")


class TestElapsed:
    def test_is_frozen(self) -> None:
        import dataclasses

        e = Elapsed(ms=1.0, label="a", start_time=0.0)
        try:
            e.ms = 2.0  # type: ignore[misc]
        except dataclasses.FrozenInstanceError:
            return
        raise AssertionError("Elapsed should be frozen")
