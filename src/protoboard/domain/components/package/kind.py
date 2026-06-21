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

from protoboard.domain.components.footprints import DualRow, SingleRow
from protoboard.domain.components.package.from_footprint import prism_for_footprint
from protoboard.domain.components.package.loader import PackageDefError
from protoboard.domain.components.package.shapes import Package, Prism

# DIP row spacing in holes (0.3" narrow / 0.6" wide at a 0.1" grid).
_DIP_NARROW_ROWS = 3.0
_DIP_WIDE_ROWS = 6.0


def _dip(pins: int, *, wide: bool) -> Package | None:
    if pins < 4 or pins % 2 != 0:
        return None
    rows = _DIP_WIDE_ROWS if wide else _DIP_NARROW_ROWS
    return prism_for_footprint(DualRow(n_per_row=pins // 2, rows_spacing=rows))


def _sil(pins: int) -> Package | None:
    if pins < 2:
        return None
    return prism_for_footprint(SingleRow(n=pins))


# kind name → (pin_count → Package | None). The builder returns the parameterized
# geometry for the kind; ``dip_switch`` shares DIP-narrow geometry (the notch /
# plain distinction is a render concern — see Phase B).
_KIND_BUILDERS: dict[str, Callable[[int], Package | None]] = {
    "dip_ic": lambda n: _dip(n, wide=False),
    "wdip_ic": lambda n: _dip(n, wide=True),
    "dip_switch": lambda n: _dip(n, wide=False),
    "sil": _sil,
}


def is_kind(name: object) -> bool:
    """True when *name* is a recognised package kind."""
    return isinstance(name, str) and name in _KIND_BUILDERS


def build_kind_package(kind: str, pin_count: int) -> Package:
    """Build the :class:`Package` for *kind* parameterized by *pin_count*.

    The declared *kind* is stamped onto the built body (a :class:`Prism`) so the
    renderer can draw the declared look instead of guessing from pin columns.
    Raises :class:`PackageDefError` for an unknown kind or a pin count the kind
    cannot realise (e.g. an odd count for a DIP).
    """
    builder = _KIND_BUILDERS.get(kind)
    if builder is None:
        raise PackageDefError(f"unknown package kind {kind!r}")
    pkg = builder(pin_count)
    if pkg is None:
        raise PackageDefError(
            f"kind {kind!r} cannot build a body for {pin_count} pins"
        )
    if isinstance(pkg, Prism):
        pkg = dataclasses.replace(pkg, kind=kind)
    return pkg


__all__ = ["build_kind_package", "is_kind"]
