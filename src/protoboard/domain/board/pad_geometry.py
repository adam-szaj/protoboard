# SPDX-License-Identifier: MIT
"""Shared pad-coordinate quantisation.

Pad centers are conventionally at ``(col+0.5, row+0.5)`` in raster units.
Floating-point noise (legacy saves with tiny offsets, rotation rounding,
drag math) can place positions a few thousandths off.  Without
quantisation, downstream indexes (ConnectivityGraph, PadIndex) would treat
``(3.5, 3.5)`` and ``(3.5, 3.503)`` as distinct pads and a wire connecting
them would not register electrically.

This module is the single source of truth for the tolerance and the
quantisation helpers.  PadIndex and ConnectivityGraph both import from
here so they always agree on whether two near-identical floats are the
same pad.
"""

from __future__ import annotations

from protoboard.geometry import Point
from protoboard.types import NodeKey  # (qx, qy, side)

# Pads within ±_SNAP_TOL are considered the same pad.  5e-3 (= ±0.005)
# raster units is comfortably below any realistic prototyping-board pitch
# (typical pad spacing ≥ 0.5 raster units = 1.27 mm) while absorbing
# legacy fractional offsets like "3.503".
SNAP_TOL: float = 5e-3


def q(v: float, tol: float = SNAP_TOL) -> float:
    """Quantise *v* to the nearest multiple of ``2*tol`` (bucket width).

    With ``tol=5e-3`` this rounds to two decimal places, giving ±0.005
    tolerance.
    """
    bucket = 2 * tol
    return round(v / bucket) * bucket


def qkey(point: Point, side: str) -> NodeKey:
    """Build a quantised ``NodeKey`` from a Point + side string."""
    return (q(point.x), q(point.y), side)


def qpair(point: Point) -> tuple[float, float]:
    """Quantised ``(qx, qy)`` pair, no side — for side-agnostic pad lookup."""
    return (q(point.x), q(point.y))


__all__ = ["SNAP_TOL", "NodeKey", "q", "qkey", "qpair"]
