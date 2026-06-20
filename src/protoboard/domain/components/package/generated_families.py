# SPDX-License-Identifier: MIT
"""Generated package families for standard in-line bodies — no static catalogue.

Multi-pin in-line bodies follow rigid conventions, so their package families are
*generated* by name rather than authored as YAML.  A component selects one by a
``"{family_id}/std"`` address (the same ``package_variant`` mechanism the
two-pin catalogue uses); the geometry is built by the prism machinery in
:mod:`~protoboard.domain.components.package.from_footprint`.

Naming convention (per design 2026-06-20):

- ``dip-N``  — DIP-N, narrow body (0.3 in row pitch ≈ 3 holes).
- ``wdip-N`` — DIP-N, wide body (0.6 in row pitch ≈ 6 holes).
- ``sil-N``  — single in-line header / shell of N pins.

``N`` is the total pin count (even for DIP).  The families merge into the
session's package-family catalogue, so ``resolve_component_package`` and the
body-variant picker pick them up with no extra wiring.

Pure domain: no Qt, no I/O.
"""

from __future__ import annotations

from protoboard.domain.components.footprints import (
    DualRow,
    FootprintTemplate,
    SingleRow,
)
from protoboard.domain.components.package.family import (
    PackageFamily,
    PackageVariant,
)
from protoboard.domain.components.package.from_footprint import prism_for_footprint

# Row pitch (in holes) for the two DIP widths.  0.3 in / 0.6 in at a 0.1 in
# (one-hole) grid → 3 and 6 holes between the columns.
_DIP_NARROW_ROWS = 3
_DIP_WIDE_ROWS = 6

# The single, canonical variant id every generated family carries.  Width /
# pin count are encoded in the family id itself, so there is nothing left to
# vary within a family — one ``std`` variant keeps the address shape uniform
# with the two-pin catalogue (``"family/variant"``).
STD = "std"

# Pin-count ranges the picker offers.  DIP runs in even steps; SIL from 3 up
# (two-pin parts own n=2 via the TwoPin catalogue).
_DIP_COUNTS = range(4, 65, 2)
_SIL_COUNTS = range(3, 41)


def _family(
    family_id: str, label: str, footprint: FootprintTemplate,
) -> PackageFamily | None:
    pkg = prism_for_footprint(footprint)
    if pkg is None:
        return None
    return PackageFamily(
        family_id=family_id,
        label=label,
        variants=(PackageVariant(variant_id=STD, label=label, package=pkg),),
    )


def dip_family(pins: int, *, wide: bool) -> PackageFamily | None:
    """The generated ``dip-N`` / ``wdip-N`` family for an even *pins* count.

    Returns ``None`` for an odd or non-positive count (no DIP body exists).
    """
    if pins < 4 or pins % 2 != 0:
        return None
    rows = _DIP_WIDE_ROWS if wide else _DIP_NARROW_ROWS
    fp = DualRow(n_per_row=pins // 2, rows_spacing=float(rows))
    prefix = "wdip" if wide else "dip"
    width = "wide 0.6\"" if wide else "narrow 0.3\""
    return _family(f"{prefix}-{pins}", f"DIP-{pins} ({width})", fp)


def sil_family(pins: int) -> PackageFamily | None:
    """The generated ``sil-N`` header family for *pins* (>= 2)."""
    if pins < 2:
        return None
    return _family(f"sil-{pins}", f"SIL-{pins} header", SingleRow(n=pins))


def generate_inline_families() -> dict[str, PackageFamily]:
    """Every generated in-line family, keyed by ``family_id``.

    ``dip-N`` / ``wdip-N`` for even N in :data:`_DIP_COUNTS`, ``sil-N`` for N in
    :data:`_SIL_COUNTS`.  Intended to be merged into the loaded catalogue.
    """
    out: dict[str, PackageFamily] = {}
    for n in _DIP_COUNTS:
        for wide in (False, True):
            fam = dip_family(n, wide=wide)
            if fam is not None:
                out[fam.family_id] = fam
    for n in _SIL_COUNTS:
        fam = sil_family(n)
        if fam is not None:
            out[fam.family_id] = fam
    return out


def inline_family_ids_for_pins(pin_count: int, *, dual_row: bool) -> list[str]:
    """The generated family ids that fit a footprint of *pin_count* pins.

    ``dual_row`` selects the DIP families (``dip-N`` / ``wdip-N``); otherwise the
    SIL family (``sil-N``).  Used by the picker to offer only the bodies whose
    pin count matches the part — a DIP-16 sees ``dip-16`` / ``wdip-16``, never
    ``dip-8`` or a resistor body.
    """
    if dual_row:
        if pin_count < 4 or pin_count % 2 != 0:
            return []
        return [f"dip-{pin_count}", f"wdip-{pin_count}"]
    if pin_count < 2:
        return []
    return [f"sil-{pin_count}"]


def offered_body_families(
    footprint: object, families: dict[str, PackageFamily],
) -> list[PackageFamily]:
    """The package families a body-variant picker should offer for *footprint*.

    The catalogue (*families*) is the merged authored + generated set.  The
    footprint decides which slice is relevant — this is the one place that
    branches on footprint type so the two panels (properties / library) don't
    each grow their own copy:

    * ``TwoPin`` → every **two-pin** family (swap a resistor for a brick or a
      ceramic disc for a tantalum drop); the multi-pin DIP / SIL families are
      filtered out by pin count.
    * ``DualRow`` → only the pin-count-matching ``dip-N`` / ``wdip-N``.
    * ``SingleRow`` → only the pin-count-matching ``sil-N``.
    * anything else → nothing (no body choice).

    Returns the families in offer order; unknown ids are skipped.
    """
    # Imported here to avoid a domain import cycle through footprints at module
    # load (footprints imports nothing from this package, but keep it local).
    from protoboard.domain.components.footprints import (
        DualRow,
        SingleRow,
        TwoPin,
    )

    if isinstance(footprint, TwoPin):
        return [
            families[k]
            for k in sorted(families)
            if families[k].default_variant().package.pin_count() == 2
        ]
    if isinstance(footprint, DualRow):
        ids = inline_family_ids_for_pins(footprint.pin_count(), dual_row=True)
    elif isinstance(footprint, SingleRow):
        ids = inline_family_ids_for_pins(footprint.pin_count(), dual_row=False)
    else:
        return []
    return [families[i] for i in ids if i in families]


__all__ = [
    "STD",
    "dip_family",
    "generate_inline_families",
    "inline_family_ids_for_pins",
    "offered_body_families",
    "sil_family",
]
