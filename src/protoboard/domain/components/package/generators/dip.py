# SPDX-License-Identifier: MIT
"""Builtin generators for the dual-row IC kinds: DIP / wide-DIP / DIP-switch."""

from __future__ import annotations

from typing import ClassVar

from protoboard.domain.components.footprints import DIL
from protoboard.domain.components.package.generators.base import (
    RASTER_MM,
    GeneratorContext,
    PackageGenerator,
    prism_from_footprint,
    register_generator,
)
from protoboard.domain.components.package.loader import PackageDefError

# DIP row spacing in holes (0.3" narrow / 0.6" wide at a 0.1" grid).
DIP_NARROW_ROWS = 3.0
DIP_WIDE_ROWS = 6.0

# DIP body geometry (measured, JEDEC-ish), in mm.  A DIP body is *narrower*
# than the row spacing so the legs show on both sides, and runs a little past
# the end pins.  The moulded-body width is a linear fit to real parts of the
# row spacing R (in 2.54 mm units): ``width = 2.54·R − 1.35`` mm — a narrow
# 0.3" DIP (R=3) is ≈6.27 mm across, a wide 0.6" DIP (R=6) ≈13.89 mm.
# (Restored from the removed footprint→body synthesis, commit 4618740.)
_DIP_WIDTH_PITCH_MM = 2.54  # row-pitch coefficient in the body-width fit
_DIP_WIDTH_INSET_MM = 1.35  # constant inset in the body-width fit
_DIP_END_MARGIN_MM = 1.27   # body extent past the first/last pin (along axis)


def dip_body_dims_mm(
    pin_count: int, rows_spacing: float,
) -> tuple[float, float]:
    """DIP body ``(width across rows, length along pins)`` in mm.

    ``width = 2.54·R − 1.35`` (the measured fit above); ``length`` = the pin
    span plus an end margin past each end pin.  Shared by the DIP kind
    generators and the generated ``dip-N`` picker families so the two build
    paths cannot desync.
    """
    width = _DIP_WIDTH_PITCH_MM * rows_spacing - _DIP_WIDTH_INSET_MM
    pins_per_row = pin_count // 2
    length = (pins_per_row - 1) * RASTER_MM + 2.0 * _DIP_END_MARGIN_MM
    return width, length


@register_generator(kind=("dip_ic", "dip_switch"))
class DipGenerator(PackageGenerator):
    """Narrow (0.3") dual-row prism. ``dip_switch`` shares the geometry — the
    notch / plain distinction is a renderer concern (kind drives dispatch)."""

    rows_spacing: ClassVar[float] = DIP_NARROW_ROWS

    def generate(self, ctx: GeneratorContext) -> GeneratorContext:
        n = ctx.pin_count
        if n < 4 or n % 2 != 0:
            raise PackageDefError(
                f"a DIP kind cannot build a body for {n} pins "
                f"(needs an even count >= 4)")
        pkg = prism_from_footprint(
            DIL(n=n, rows_spacing=self.rows_spacing),
            dims_mm=dip_body_dims_mm(n, self.rows_spacing),
        )
        if pkg is None:
            raise PackageDefError(
                f"a DIP kind cannot build a body for {n} pins")
        return ctx.with_package(pkg)

    def offered_family_ids(self, pin_count: int) -> list[str]:
        # Both DIP widths of the matching pin count (narrow ↔ wide swap);
        # inherited by WideDipGenerator so either width offers the pair.
        if pin_count < 4 or pin_count % 2 != 0:
            return []
        return [f"dip-{pin_count}", f"wdip-{pin_count}"]


@register_generator(kind="wdip_ic")
class WideDipGenerator(DipGenerator):
    """Wide (0.6") dual-row prism — the classic wide DIP."""

    rows_spacing: ClassVar[float] = DIP_WIDE_ROWS
