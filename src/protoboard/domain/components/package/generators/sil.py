# SPDX-License-Identifier: MIT
"""Builtin generator for the single-row header kind (``sil``)."""

from __future__ import annotations

from protoboard.domain.components.footprints import SIL
from protoboard.domain.components.package.generators.base import (
    GeneratorContext,
    PackageGenerator,
    prism_from_footprint,
    register_generator,
)
from protoboard.domain.components.package.loader import PackageDefError


@register_generator(kind="sil")
class SilGenerator(PackageGenerator):
    """Single-in-line prism — one straight row of *pin_count* pins."""

    def generate(self, ctx: GeneratorContext) -> GeneratorContext:
        n = ctx.pin_count
        if n < 2:
            raise PackageDefError(
                f"kind 'sil' cannot build a body for {n} pins (needs >= 2)"
            )
        pkg = prism_from_footprint(SIL(n=n))
        if pkg is None:
            raise PackageDefError(f"kind 'sil' cannot build a body for {n} pins")
        return ctx.with_package(pkg)
