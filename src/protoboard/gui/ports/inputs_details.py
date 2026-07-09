# SPDX-License-Identifier: MIT
"""Concept classes behind the input DTOs (companion of ``inputs.py``)."""

from __future__ import annotations

# One mouse-wheel detent in Qt ``angleDelta`` units (15° × 8).
WHEEL_NOTCH = 120


class WheelNotchAccumulator:
    """Folds raw ``delta_y`` ticks into whole wheel notches.

    High-resolution wheels report sub-detent deltas; consumers that step per
    detent (armed rotate) feed every ``delta_y`` in and act on the returned
    whole-notch count.  A direction flip drops the opposite-sign residue —
    the historical behaviour of the inline accumulator this class replaces.
    """

    def __init__(self) -> None:
        self._accum = 0

    def add(self, delta_y: int) -> int:
        """Accumulate *delta_y*; return the signed number of whole notches."""
        if delta_y == 0:
            return 0
        if (delta_y > 0) != (self._accum >= 0):
            self._accum = 0
        self._accum += delta_y
        notches = 0
        while self._accum >= WHEEL_NOTCH:
            self._accum -= WHEEL_NOTCH
            notches += 1
        while self._accum <= -WHEEL_NOTCH:
            self._accum += WHEEL_NOTCH
            notches -= 1
        return notches
