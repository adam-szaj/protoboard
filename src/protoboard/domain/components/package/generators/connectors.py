# SPDX-License-Identifier: MIT
"""Builtin generators for the connector kinds: pin-header and JST.

Both share the SIL / DIP prism geometry at the 0.1" connector row spacing;
only the housing colour differs (declared per generator).
"""

from __future__ import annotations

from protoboard.domain.components.footprints import DIL, SIL
from protoboard.domain.components.package.generators.base import (
    GeneratorContext,
    PackageGenerator,
    prism_from_footprint,
)
from protoboard.domain.components.package.loader import PackageDefError
from protoboard.domain.components.package.schema import parse_schema
from protoboard.domain.components.package.shapes import Package

# Connector row spacing in holes (0.1" pitch — pins on adjacent raster holes).
CONNECTOR_ROWS = 1.0


def _connector_body(pins: int, rows: int) -> Package:
    """A 1×N (single row) or 2×N (dual row) connector prism from *pins*."""
    if rows == 1:
        if pins < 2:
            raise PackageDefError(
                f"a connector kind cannot build a body for {pins} pins "
                f"(needs >= 2)")
        pkg = prism_from_footprint(SIL(n=pins))
    elif rows == 2:
        if pins < 2 or pins % 2 != 0:
            raise PackageDefError(
                f"a 2xN connector cannot build a body for {pins} pins "
                f"(needs an even count >= 2)")
        pkg = prism_from_footprint(DIL(n=pins, rows_spacing=CONNECTOR_ROWS))
    else:
        raise PackageDefError(f"connector 'rows' must be 1 or 2, got {rows}")
    if pkg is None:
        raise PackageDefError(
            f"a connector kind cannot build a body for {pins} pins")
    return pkg


class PinHeaderGenerator(PackageGenerator):
    """Gold-pin header, 1×N or 2×N (the ``rows`` parameter)."""

    generates = "pin_header"
    params = parse_schema({"rows": {"type": "int", "default": 1}})
    color = "#222222"  # near-black plastic strip

    def generate(self, ctx: GeneratorContext) -> GeneratorContext:
        rows = int(ctx.values.get("rows", 1))
        return ctx.with_package(_connector_body(ctx.pin_count, rows))

    def offered_family_ids(self, pin_count: int) -> list[str]:
        # A single-row header swaps to the bare sil-N shell.  (JST stays
        # offer-less: its polarised housing has no generated equivalent.)
        return [f"sil-{pin_count}"] if pin_count >= 2 else []


class JstGenerator(PackageGenerator):
    """JST connector — always a single row, light housing."""

    generates = "jst"
    color = "#dddddd"

    def generate(self, ctx: GeneratorContext) -> GeneratorContext:
        return ctx.with_package(_connector_body(ctx.pin_count, rows=1))
