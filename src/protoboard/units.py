# SPDX-License-Identifier: MIT
"""Unit types and conversions.

`RasterUnit` is the internal coordinate unit (1.0 = one hole pitch).
`Millimeter` is the physical unit. Conversion goes through a configurable
pitch (default 2.54 mm).
"""

from __future__ import annotations

import math
from typing import NewType

RasterUnit = NewType("RasterUnit", float)
Millimeter = NewType("Millimeter", float)

DEFAULT_PITCH_MM: float = 2.54


def _validate_pitch(pitch_mm: float) -> None:
    if not math.isfinite(pitch_mm) or pitch_mm <= 0.0:
        raise ValueError(f"pitch_mm must be a positive finite number; got {pitch_mm}")


def _validate_finite(value: float, name: str) -> None:
    if not math.isfinite(value):
        raise ValueError(f"{name} must be finite; got {value}")


def to_mm(value: RasterUnit, pitch_mm: float = DEFAULT_PITCH_MM) -> Millimeter:
    """Convert a raster-unit value to millimeters."""
    _validate_pitch(pitch_mm)
    _validate_finite(float(value), "value")
    return Millimeter(float(value) * pitch_mm)


def to_raster(value: Millimeter, pitch_mm: float = DEFAULT_PITCH_MM) -> RasterUnit:
    """Convert a millimeter value to raster units."""
    _validate_pitch(pitch_mm)
    _validate_finite(float(value), "value")
    return RasterUnit(float(value) / pitch_mm)
