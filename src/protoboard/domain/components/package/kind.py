# SPDX-License-Identifier: MIT
"""Named, parameterized package *kinds*.

A ``kind`` is a field in a package definition (``package: {kind: dip_ic}``), not
a new entity. It sits *between* a generic solid (everything still to specify) and
a fully-specified footprint (nothing to specify): the look is known (a DIP-IC is a
dual-row prism with a notch; a DIP-switch is a plain dual-row prism) but the
geometry stays **parameterized** — chiefly by the owning component's pin count.

Each kind maps to a builder that, given the pin count, produces a concrete
:class:`Package` by reusing the existing footprint→prism machinery
(:func:`prism_for_footprint`). No geometry is duplicated here. Resolution is a
parse/build-boundary dispatch (a name→builder table), not domain type-branching.

The renderer may honour the kind for a richer body (the notch); when it does not,
it falls back to the generic solid + pin layout (today's ``silhouette_for``), so a
kind never blocks rendering.

Pure domain: no Qt, no I/O.
"""

from __future__ import annotations

import dataclasses
from collections.abc import Callable
from functools import lru_cache
from typing import Any

from protoboard.domain.components.footprints import DIL, SIL, Footprint
from protoboard.domain.components.package.loader import (
    PackageDefError,
    build_package,
)
from protoboard.domain.components.package.shapes import Package, Prism

# DIP row spacing in holes (0.3" narrow / 0.6" wide at a 0.1" grid).
_DIP_NARROW_ROWS = 3.0
_DIP_WIDE_ROWS = 6.0
# Connector row spacing in holes (0.1" pitch — pins on adjacent raster holes).
_CONNECTOR_ROWS = 1.0
# Package-domain raster (mm per hole) — scales hole-step offsets to mm so the
# prism's pins read back as exact hole coordinates (mm ÷ raster).
_RASTER_MM = 2.5
# Body margin around the pin cloud, in holes (legacy SVG bodies' 0.5-hole inset).
_MARGIN_HOLES = 0.5


def _prism_from_footprint(fp: Footprint) -> Package | None:
    """Build a plain prism :class:`Package` whose pins follow *fp*.

    Geometry only: the body bbox is derived from the pin cloud plus a uniform
    margin, leads point rigidly down. This is a minimal stub of the former
    ``prism_for_footprint`` — enough for the kind builders to produce a body;
    richer per-kind silhouettes (DIP notch, etc.) are a renderer concern (see
    docs/todo.md). Returns ``None`` for a footprint with no body (< 2 pins).
    """
    pins = fp.pin_positions()
    if len(pins) < 2:
        return None
    steps = [
        [pins[i + 1].x - pins[i].x, pins[i + 1].y - pins[i].y]
        for i in range(len(pins) - 1)
    ]
    spec = {
        "shape": "prism",
        "dims": {},  # bbox derived from pins + margin
        "bend": 0,
        "margin": _MARGIN_HOLES * _RASTER_MM,
        "leads": {
            "direction": [0, 0, -1],
            "body_offset": {"unit": _RASTER_MM, "value": steps},
            "board_offset": "default",
        },
    }
    try:
        return build_package(spec, "synth/kind")
    except PackageDefError:
        return None


# Marker stamped on the synthetic fallback package (``Prism.kind``) so consumers
# can tell a *declared* body apart from this generic bbox stand-in and keep their
# pre-invariant behaviour (authored ``bendable_pins``, DRC body-overlap skip).
GENERIC_PACKAGE_KIND = "_generic"


def is_generic_package(package: object) -> bool:
    """True when *package* is the synthetic generic bbox fallback (not declared).

    Single predicate so the ``kind == "_generic"`` tag-check is not duplicated
    across ``effective_bend_specs`` and the DRC engine.
    """
    return getattr(package, "kind", None) == GENERIC_PACKAGE_KIND


@lru_cache(maxsize=None)
def _synth_generic_by_pins(
    pins: tuple[tuple[float, float], ...], bend_holes: float
) -> Package | None:
    if len(pins) < 1:
        return None
    steps = [
        [pins[i + 1][0] - pins[i][0], pins[i + 1][1] - pins[i][1]]
        for i in range(len(pins) - 1)
    ]
    spec = {
        "shape": "prism",
        "dims": {},  # bbox derived from pins + margin
        "bend": bend_holes,  # > 0 → non-rigid (is_rigid_package stays False)
        "margin": _MARGIN_HOLES * _RASTER_MM,
        "leads": {
            "direction": [0, 0, -1],
            "body_offset": {"unit": _RASTER_MM, "value": steps},
            "board_offset": "default",
        },
    }
    try:
        pkg = build_package(spec, "synth/generic")
    except PackageDefError:
        return None
    if isinstance(pkg, Prism):
        pkg = dataclasses.replace(pkg, kind=GENERIC_PACKAGE_KIND)
    return pkg


def synth_generic_package(
    fp: Footprint, *, bend_holes: float = 1.0
) -> Package | None:
    """A NON-rigid generic bbox package whose pins follow *fp*.

    The fallback body for a library part that declares no real package, so
    ``ComponentDefinition.package`` can be an invariant (always set). Geometry is
    the pin-cloud bbox + 0.5-hole margin (matching the legacy ``component_bbox``
    body footprint); ``bend > 0`` keeps ``is_rigid_package`` False so the part's
    bendable leads survive. Tagged ``kind="_generic"`` so bend-spec / DRC
    consumers can tell it from a declared body and keep today's behaviour.
    Returns ``None`` for a footprint with no pins. Cached by pin-cloud (the ~187
    library parts collapse to a handful of distinct clouds)."""
    pins = tuple((p.x, p.y) for p in fp.pin_positions())
    return _synth_generic_by_pins(pins, bend_holes)


def _dip(pins: int, *, wide: bool) -> Package | None:
    if pins < 4 or pins % 2 != 0:
        return None
    rows = _DIP_WIDE_ROWS if wide else _DIP_NARROW_ROWS
    return _prism_from_footprint(DIL(n=pins, rows_spacing=rows))


def _sil(pins: int) -> Package | None:
    if pins < 2:
        return None
    return _prism_from_footprint(SIL(n=pins))


def _connector(pins: int, *, rows: int) -> Package | None:
    """A 1×N (single row) or 2×N (dual row) connector body from *pins*.

    The geometry is the existing SIL / DIP prism at the 0.1" connector row
    spacing — pin-headers and JST share it; only the fill colour differs (set by
    :func:`build_kind_package` from ``_KIND_COLOR``).
    """
    if rows == 1:
        return _sil(pins)
    if rows == 2:
        if pins < 2 or pins % 2 != 0:
            return None
        return _prism_from_footprint(
            DIL(n=pins, rows_spacing=_CONNECTOR_ROWS))
    return None


# kind name → builder(pin_count, **params) → Package | None. ``params`` carries
# extra package fields (e.g. a connector's ``rows``); a builder ignores keys it
# does not use. ``dip_switch`` shares DIP-narrow geometry (the notch / plain
# distinction is a render concern — see Phase B).
_KIND_BUILDERS: dict[str, Callable[..., Package | None]] = {
    "dip_ic": lambda n, **_p: _dip(n, wide=False),
    "wdip_ic": lambda n, **_p: _dip(n, wide=True),
    "dip_switch": lambda n, **_p: _dip(n, wide=False),
    "sil": lambda n, **_p: _sil(n),
    # Connectors: pin-header is 1×N or 2×N (``rows``); JST is always single-row.
    "pin_header": lambda n, rows=1, **_p: _connector(n, rows=int(rows)),
    "jst": lambda n, **_p: _connector(n, rows=1),
}

# Per-kind fill colour (#rrggbb), stamped onto the built body. None → the
# renderer's caller/theme colour drives appearance (as for the DIP/SIL kinds).
_KIND_COLOR: dict[str, str] = {
    "pin_header": "#222222",  # gold-pin header — near-black plastic strip
    "jst": "#dddddd",  # JST connector — light housing
}


def is_kind(name: object) -> bool:
    """True when *name* is a recognised package kind."""
    return isinstance(name, str) and name in _KIND_BUILDERS


def build_kind_package(kind: str,
                       pin_count: int,
                       params: dict[str, Any] | None = None) -> Package:
    """Build the :class:`Package` for *kind* parameterized by *pin_count*.

    *params* carries extra package fields a kind may need (e.g. a connector's
    ``rows``); builders ignore keys they do not use. The declared *kind* (and any
    per-kind colour) is stamped onto the built body (a :class:`Prism`) so the
    renderer can draw the declared look instead of guessing from pin columns.
    Raises :class:`PackageDefError` for an unknown kind or a pin count the kind
    cannot realise (e.g. an odd count for a DIP or 2×N connector).
    """
    builder = _KIND_BUILDERS.get(kind)
    if builder is None:
        raise PackageDefError(f"unknown package kind {kind!r}")
    pkg = builder(pin_count, **(params or {}))
    if pkg is None:
        raise PackageDefError(
            f"kind {kind!r} cannot build a body for {pin_count} pins")
    if isinstance(pkg, Prism):
        changes: dict[str, Any] = {"kind": kind}
        if kind in _KIND_COLOR:
            changes["color"] = _KIND_COLOR[kind]
        pkg = dataclasses.replace(pkg, **changes)
    return pkg


__all__ = ["build_kind_package", "is_kind"]
