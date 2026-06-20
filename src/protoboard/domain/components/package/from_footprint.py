# SPDX-License-Identifier: MIT
"""Synthesize a prism :class:`Package` from a multi-pin footprint.

The parametric renderer draws every body from a :class:`Package`.  Parts placed
from the catalogue carry a real package (resolved via
``resolve_component_package``), but plain in-line footprints — ``DualRow`` (DIP),
``SingleRow`` (SIL header) and multi-column dev-board modules (``QuadRow``) —
historically had no package and fell to hand-written SVG body generators.

This module closes that gap: it builds a prism package whose pin cloud matches
the footprint exactly, so the part renders through the same parametric
``PackageBody`` (DIP notch, SIL slots, or — for a dev-board module — a plain
bbox body) as a catalogued package.  It is a *construction boundary* — one
factory turning a footprint's pin cloud into package data — not behaviour that
belongs on the footprints themselves.

The body silhouette wraps the pin cloud with a uniform margin (the same look the
legacy SVG generators produced from a fixed 0.5-hole margin).  Pure domain: no
Qt, no I/O.
"""

from __future__ import annotations

from functools import lru_cache
from typing import Any

from protoboard.domain.components.footprints import (
    DualRow,
    FootprintTemplate,
    QuadRow,
    SingleRow,
)
from protoboard.domain.components.package.loader import PackageDefError, build_package
from protoboard.domain.components.package.shapes import Prism

# Footprint types that get a synthesized prism body.  Two-pin parts draw from
# the catalogue package (or a generic bbox); grids and bare templates keep their
# own path.  TO-92 / TO-220 subclass SingleRow but are dispatched to their own
# renderers upstream, before this factory is consulted.
_PRISM_FOOTPRINTS = (DualRow, SingleRow, QuadRow)

# Body margin around the pin cloud, in holes — matches the legacy SVG bodies'
# fixed 0.5-hole inset.  Used for SIL housings / dev-board bodies that hug their
# pin cloud.  The package domain's raster is 2.5 mm/hole.
_MARGIN_HOLES = 0.5
_RASTER_MM = 2.5

# DIP body geometry (JEDEC-ish), in mm.  A DIP body is *narrower* than the row
# spacing so the legs show on both sides, and runs a little past the end pins.
_DIP_BODY_INSET_MM = 1.27   # body edge inset from each pin row (≈0.05 in)
_DIP_END_MARGIN_MM = 1.27   # body extent past the first/last pin (along the axis)
_PIN_PITCH_MM = 2.5         # package-domain hole pitch


def _dual_row_body_dims(fp: DualRow) -> tuple[float, float]:
    """DIP body (width across rows, length along pins) in mm for *fp*.

    width  = row spacing − a body inset on each side (legs show outside the
             moulded body); length = pin span + an end margin past each end pin.
    """
    rows_mm = fp.rows_spacing * _PIN_PITCH_MM
    width_mm = rows_mm - 2.0 * _DIP_BODY_INSET_MM
    length_mm = (fp.n_per_row - 1) * fp.pitch * _PIN_PITCH_MM + 2.0 * _DIP_END_MARGIN_MM
    return width_mm, length_mm


def _step_offsets(fp: FootprintTemplate) -> list[list[float]]:
    """Pin→pin step vectors (holes), as a raw ``body_offset`` value.

    Derived from the footprint's own pin cloud as consecutive differences, so
    the synthesized package's pins coincide with the placed pads for any
    in-line layout without per-type geometry duplicated here.
    """
    pins = fp.pin_positions()
    return [
        [pins[i + 1].x - pins[i].x, pins[i + 1].y - pins[i].y]
        for i in range(len(pins) - 1)
    ]


def _prism_dict(fp: FootprintTemplate) -> dict[str, Any]:
    """A prism package definition whose pins follow *fp* (holes, pin 1 = 0).

    Geometry only: rigid downward leads.  A ``DualRow`` (DIP) is sized by explicit
    JEDEC dimensions and centred on the pin cloud (body between the rows, legs
    showing); a ``SingleRow`` (SIL) / ``QuadRow`` (dev-board) hugs its pin cloud
    with a uniform margin.  Colour/edge are left default so the renderer's
    caller-fill (component / theme colour) drives appearance.
    """
    dims: dict[str, Any]
    extra: dict[str, Any]
    if isinstance(fp, DualRow):
        width_mm, length_mm = _dual_row_body_dims(fp)
        dims = {"width": width_mm, "length": length_mm}
        extra = {}  # explicit dims → Prism.bbox centres on the pins
    else:
        dims = {}  # bbox derived from pins + margin (SIL housing / dev-board)
        extra = {"margin": _MARGIN_HOLES * _RASTER_MM}
    return {
        "shape": "prism",
        "dims": dims,
        "bend": 0,
        **extra,
        "leads": {
            "direction": [0, 0, -1],
            # Steps are in holes; unit: 2.5 (the package raster, mm/hole) scales
            # them to mm so board_offset: default reads them back as exact hole
            # coordinates (mm ÷ raster).
            "body_offset": {"unit": _RASTER_MM, "value": _step_offsets(fp)},
            "board_offset": "default",
        },
    }


@lru_cache(maxsize=256)
def prism_for_footprint(fp: FootprintTemplate) -> Prism | None:
    """A prism package matching *fp*, or ``None`` for unsupported footprints.

    Supports the in-line multi-pin families the SVG generators covered:
    ``DualRow`` (DIP), ``SingleRow`` (SIL) and ``QuadRow`` (dev-board module).
    Other footprints (grids, two-pin) return ``None`` and keep their own path.
    A single-pin (or empty) footprint has no body and also returns ``None``.

    Footprints are frozen value objects, so the result is memoised by footprint
    identity — the same DIP-16 (etc.) is built once, not on every render.
    """
    if not isinstance(fp, _PRISM_FOOTPRINTS) or fp.pin_count() < 2:
        return None
    try:
        pkg = build_package(_prism_dict(fp), f"synth/{type(fp).__name__}")
    except PackageDefError:
        return None
    assert isinstance(pkg, Prism)
    return pkg


__all__ = ["prism_for_footprint"]
