# SPDX-License-Identifier: MIT
"""Builtin generator for the explicit generic-bbox kind (``generic``).

The DECLARED spelling of "just a plain bbox body over my pin cloud": the same
synthetic ``_generic`` package the loader used to fabricate for bare
``footprint:``-only parts, now requested explicitly::

    pins: {count: 2}
    package: {kind: generic}

The body keeps every ``_generic`` semantic: non-rigid (authored
``bendable_pins`` stay in force — ``effective_bend_specs`` defers to them),
excluded from the DRC body mask and from the body-variant picker, rendered as
the plain pin-cloud bbox. Pins default to a single 1-hole-pitch row.
"""

from __future__ import annotations

from protoboard.domain.components.footprints import SIL
from protoboard.domain.components.package.generators.base import (
    GeneratorContext,
    PackageGenerator,
)
from protoboard.domain.components.package.loader import PackageDefError


class GenericGenerator(PackageGenerator):
    """Generic bbox body — a declared ``_generic`` over a single pin row."""

    generates = "generic"

    def generate(self, ctx: GeneratorContext) -> GeneratorContext:
        # Local import: kind.py imports this package's registry at module
        # load, so the reverse import must stay call-time.
        from protoboard.domain.components.package.kind import (
            synth_generic_package,
        )

        n = ctx.pin_count
        if n < 1:
            raise PackageDefError(
                f"kind 'generic' cannot build a body for {n} pins")
        pkg = synth_generic_package(SIL(n=n))
        if pkg is None:
            raise PackageDefError(
                f"kind 'generic' cannot build a body for {n} pins")
        return ctx.with_package(pkg)

    def offered_family_ids(self, pin_count: int) -> list[str]:
        # A generic body is deliberately absent from the body picker.
        return []
