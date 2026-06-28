# SPDX-License-Identifier: MIT
"""Resolve a placed component's concrete :class:`Package` from the catalogue.

A component can reference a package three ways, in precedence order:

1. ``ComponentInstance.package_variant`` — a per-instance ``"family/variant"``
   override (the user picked a specific body for this part).
2. ``ComponentDefinition.package_family_ref`` — the library default ``"family/
   variant"`` for parts placed from this definition.
3. ``ComponentDefinition.package`` — an inline package built directly from the
   component's own ``package:`` block (geometry that is not a catalogue body,
   e.g. a DIP).

A ``"family/variant"`` address that does not resolve in *families* falls
through to the next rung, so a stale reference degrades to the inline package
or ``None`` rather than raising. ``None`` means the component has no package and
the legacy footprint/renderer path applies.

Pure domain: the caller supplies the catalogue (``families``); this module does
no I/O.
"""

from __future__ import annotations

from typing import TYPE_CHECKING

from protoboard.domain.components.package.family import (
    PackageFamily,
    resolve_variant,
)
from protoboard.domain.components.package.shapes import Package

if TYPE_CHECKING:
    from protoboard.domain.components.definition import ComponentDefinition
    from protoboard.domain.components.footprints import Footprint
    from protoboard.domain.components.instance import ComponentInstance


def resolve_component_package(
    defn: "ComponentDefinition",
    instance: "ComponentInstance | None",
    families: dict[str, PackageFamily],
) -> Package | None:
    """The concrete package for a placed component, by precedence.

    instance.package_variant → defn.package_family_ref → defn.package → None.
    An address that does not resolve in *families* is skipped (falls through).
    """
    if instance is not None and instance.package_variant:
        variant = resolve_variant(families, instance.package_variant)
        if variant is not None:
            return variant.package
    if defn.package_family_ref:
        variant = resolve_variant(families, defn.package_family_ref)
        if variant is not None:
            return variant.package
    return defn.package


def resolved_footprint(
    defn: "ComponentDefinition",
    instance: "ComponentInstance | None",
    families: dict[str, PackageFamily] | None,
) -> "Footprint":
    """The footprint of the component's *resolved* package variant.

    ``ComponentDefinition.footprint`` is the pin cloud of the *default* variant.
    When a component carries a chosen ``instance.package_variant`` (or a
    ``package_family_ref``), its real pin spacing lives in that variant's
    package, not the default footprint — so any placement-time geometry (load
    snapping, render, bbox, place/route) that reads ``defn.footprint`` silently
    uses the wrong variant. This resolves the variant via
    :func:`resolve_component_package` and adapts it to a ``Footprint``; it falls
    back to ``defn.footprint`` when no package resolves (legacy parts, or no
    instance/catalogue available). Single source of truth for "the footprint
    this placed component actually uses".
    """
    pkg = resolve_component_package(defn, instance, families or {})
    if pkg is not None:
        from protoboard.domain.components.package.adapter import (
            PackageFootprint,
        )
        return PackageFootprint(pkg)
    return defn.footprint
