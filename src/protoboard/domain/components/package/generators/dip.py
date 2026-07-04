# SPDX-License-Identifier: MIT
"""Builtin generators for the dual-row IC kinds: DIP / wide-DIP / DIP-switch."""

from __future__ import annotations

from typing import ClassVar

from protoboard.domain.components.footprints import DIL
from protoboard.domain.components.package.generators.base import (
    GeneratorContext,
    PackageGenerator,
    prism_from_footprint,
    register_generator,
)
from protoboard.domain.components.package.loader import PackageDefError

# DIP row spacing in holes (0.3" narrow / 0.6" wide at a 0.1" grid).
DIP_NARROW_ROWS = 3.0
DIP_WIDE_ROWS = 6.0


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
        pkg = prism_from_footprint(DIL(n=n, rows_spacing=self.rows_spacing))
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
