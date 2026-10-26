# SPDX-License-Identifier: MIT
"""Two-pin radial prism whose pitch and body size come from an option name."""

from __future__ import annotations

import math
import re
from typing import Any

from protoboard.domain.components.package.generators.base import (
    RASTER_MM,
    GeneratorContext,
    PackageGenerator,
    register_generator,
)
from protoboard.domain.components.package.loader import PackageDefError
from protoboard.domain.components.package.schema import AUTO, parse_schema

_NUMBER = r"(?:\d+(?:\.\d*)?|\.\d+)"
_NAME = re.compile(
    rf"(?P<raster>{_NUMBER})mm@(?P<lx>{_NUMBER})x"
    rf"(?P<ly>{_NUMBER})x(?P<lz>{_NUMBER})"
)


@register_generator(kind="generic-prism")
class GenericPrismGenerator(PackageGenerator):
    """Build a rigid radial prism from ``pitch_mm@lx x ly x lz``."""

    code_params = parse_schema(
        {
            key: {"type": "float", "required": True}
            for key in ("raster", "lx", "ly", "lz")
        }
    )

    def option_values(self, name: str) -> dict[str, Any]:
        match = _NAME.fullmatch(name)
        if match is None:
            raise PackageDefError(
                f"generic-prism option name {name!r} must be "
                "'<raster>mm@<lx>x<ly>x<lz>'"
            )
        values = {key: float(value) for key, value in match.groupdict().items()}
        if any(value <= 0 for value in values.values()):
            raise PackageDefError(f"generic-prism option {name!r} needs positive sizes")
        holes = values["raster"] / RASTER_MM
        if not math.isclose(holes, round(holes), abs_tol=1e-9):
            raise PackageDefError(
                f"generic-prism option {name!r} raster must be a multiple "
                f"of {RASTER_MM:g} mm"
            )
        return values

    def option_pin_map(self, pin_count: int) -> dict[int, tuple[int, ...]]:
        if pin_count != 2:
            raise PackageDefError("generic-prism needs exactly two MetaPins")
        return {1: (1,), 2: (2,)}

    def resolve_auto(self, ctx: GeneratorContext) -> GeneratorContext:
        if ctx.pin_count != 2:
            raise PackageDefError("generic-prism needs exactly two pins")
        values = dict(ctx.values)
        dims = dict(values["dims"])
        for key in ("lx", "ly", "lz"):
            if dims.get(key) == AUTO:
                dims[key] = values[key]
        leads = dict(values["leads"])
        if leads.get("body_offset") == AUTO:
            leads["body_offset"] = [values["raster"], 0.0]
        values["dims"] = dims
        values["leads"] = leads
        return ctx.with_values(values)

    def generate(self, ctx: GeneratorContext) -> GeneratorContext:
        raise PackageDefError("generic-prism requires its package template")
