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
from protoboard.domain.components.package.schema import AUTO

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
_DIP_END_MARGIN_MM = 1.27  # body extent past the first/last pin (along axis)


def dip_body_dims_mm(
    pin_count: int,
    rows_spacing: float,
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


def dil_steps_mm(
    pin_count: int,
    rows_spacing: float,
) -> list[list[float]]:
    """Pin→pin ``leads.body_offset`` step vectors of a DIL, in mm (bare list).

    The DIL footprint's hole-grid deltas scaled by the raster — exactly the
    steps the legacy ``prism_from_footprint`` path derives, in the bare-list
    form the leads schema declares (the ``{unit, value}`` wrapper is a YAML
    sugar the generator never needs).
    """
    pins = DIL(n=pin_count, rows_spacing=rows_spacing).pin_positions()
    return [
        [(b.x - a.x) * RASTER_MM, (b.y - a.y) * RASTER_MM]
        for a, b in zip(pins, pins[1:])
    ]


def _check_dip_pin_count(n: int) -> None:
    if n < 4 or n % 2 != 0:
        raise PackageDefError(
            f"a DIP kind cannot build a body for {n} pins "
            f"(needs an even count >= 4)"
        )


@register_generator(kind=("dip_ic", "dip_switch"))
class DipGenerator(PackageGenerator):
    """Narrow (0.3") dual-row prism. ``dip_switch`` shares the geometry — the
    notch / plain distinction is a renderer concern (kind drives dispatch)."""

    rows_spacing: ClassVar[float] = DIP_NARROW_ROWS

    def resolve_auto(self, ctx: GeneratorContext) -> GeneratorContext:
        """Fill the DIP node's ``auto`` slots: body dims + lead steps."""
        values = dict(ctx.values)
        dims = dict(values.get("dims") or {})
        leads = dict(values.get("leads") or {})
        needs_dims = AUTO in (dims.get("ly"), dims.get("lx"))
        needs_steps = leads.get("body_offset") == AUTO
        if not (needs_dims or needs_steps):
            return ctx
        _check_dip_pin_count(ctx.pin_count)
        width, length = dip_body_dims_mm(ctx.pin_count, self.rows_spacing)
        if dims.get("ly") == AUTO:
            dims["ly"] = width  # across the pin rows
        if dims.get("lx") == AUTO:
            dims["lx"] = length  # along the pin rows
        if needs_steps:
            leads["body_offset"] = dil_steps_mm(ctx.pin_count, self.rows_spacing)
        values["dims"] = dims
        values["leads"] = leads
        return ctx.with_values(values)

    def generate(self, ctx: GeneratorContext) -> GeneratorContext:
        # Node-less fallback (a registry without the kinds.yaml node bound);
        # geometry-identical to the declarative path via the shared helpers.
        n = ctx.pin_count
        _check_dip_pin_count(n)
        pkg = prism_from_footprint(
            DIL(n=n, rows_spacing=self.rows_spacing),
            dims_mm=dip_body_dims_mm(n, self.rows_spacing),
        )
        if pkg is None:
            raise PackageDefError(f"a DIP kind cannot build a body for {n} pins")
        return ctx.with_package(pkg)


@register_generator(kind="wdip_ic")
class WideDipGenerator(DipGenerator):
    """Wide (0.6") dual-row prism — the classic wide DIP."""

    rows_spacing: ClassVar[float] = DIP_WIDE_ROWS
