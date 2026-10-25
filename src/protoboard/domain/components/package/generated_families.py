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

from typing import TYPE_CHECKING

from protoboard.domain.components.footprints import DIL, SIL, Footprint

if TYPE_CHECKING:
    from protoboard.domain.components.definition import (
        ComponentDefinition,
        PackageOption,
    )
from protoboard.domain.components.package.family import (
    PackageFamily,
    PackageVariant,
)
from protoboard.domain.components.package.generators import prism_from_footprint

# Row pitch (in holes) for the two DIP widths.  0.3 in / 0.6 in at a 0.1 in
# (one-hole) grid → 3 and 6 holes between the columns.
_DIP_NARROW_ROWS = 3
_DIP_WIDE_ROWS = 6

# The single, canonical variant id every generated family carries.  Width /
# pin count are encoded in the family id itself, so there is nothing left to
# vary within a family — one ``std`` variant keeps the address shape uniform
# with the two-pin catalogue (``"family/variant"``).
STD = "std"

# Pin-count ranges the catalogue generates. DIP runs in even steps; SIL from 3 up
# (two-pin parts own n=2 via the TwoPin catalogue).
_DIP_COUNTS = range(4, 65, 2)
_SIL_COUNTS = range(3, 41)


def _family(
    family_id: str,
    label: str,
    footprint: Footprint,
    kind: str | None = None,
    dims_mm: tuple[float, float] | None = None,
) -> PackageFamily | None:
    """Build a one-variant generated family; *kind* stamps the lineage.

    A generated body IS a declared look (``dip-14`` is a DIP by construction,
    not by guesswork), so the kind name goes onto the package ``lineage`` and
    the ordinary renderer dispatch picks the declared-look renderer
    (``DipIcRenderer`` / ``SilHeaderRenderer``) — the base prism renderer
    never column-guesses a silhouette for a declared package.
    """
    import dataclasses

    from protoboard.domain.components.package.shapes import Prism

    pkg = prism_from_footprint(footprint, dims_mm=dims_mm)
    if pkg is None:
        return None
    if kind is not None and isinstance(pkg, Prism):
        pkg = dataclasses.replace(pkg, lineage=("prism", kind))
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
    from protoboard.domain.components.package.generators.dip import (
        dip_body_dims_mm,
    )

    rows = _DIP_WIDE_ROWS if wide else _DIP_NARROW_ROWS
    fp = DIL(n=pins, rows_spacing=float(rows))
    prefix = "wdip" if wide else "dip"
    width = 'wide 0.6"' if wide else 'narrow 0.3"'
    return _family(
        f"{prefix}-{pins}",
        f"DIP-{pins} ({width})",
        fp,
        kind="wdip_ic" if wide else "dip_ic",
        dims_mm=dip_body_dims_mm(pins, float(rows)),
    )


def sil_family(pins: int) -> PackageFamily | None:
    """The generated ``sil-N`` header family for *pins* (>= 2)."""
    if pins < 2:
        return None
    return _family(f"sil-{pins}", f"SIL-{pins} header", SIL(n=pins), kind="sil")


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


def offered_package_options(
    defn: ComponentDefinition,
    families: dict[str, PackageFamily],
) -> list[PackageOption]:
    """Return concrete choices derived only from *defn.package_options*.

    A family reference expands to each of that family's variants. A concrete
    variant reference remains one leaf. Unknown references are not selectable.
    """
    import dataclasses

    from protoboard.domain.components.package.family import resolve_variant

    offered: list[PackageOption] = []
    for option in defn.package_options:
        family = families.get(option.ref)
        if family is not None:
            offered.extend(
                dataclasses.replace(
                    option,
                    name=f"{option.name}/{variant.variant_id}",
                    ref=family.address(variant.variant_id),
                )
                for variant in family.variants
            )
        elif resolve_variant(families, option.ref) is not None:
            offered.append(option)
    return offered


__all__ = [
    "STD",
    "dip_family",
    "generate_inline_families",
    "offered_package_options",
    "sil_family",
]
