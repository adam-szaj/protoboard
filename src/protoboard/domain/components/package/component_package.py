# SPDX-License-Identifier: MIT
"""Resolve a placed component's concrete :class:`Package` from the catalogue.

A component resolves its package along one axis:

1. ``ComponentInstance.package_variant`` — a per-instance ``"family/variant"``
   override (the user picked a specific body for this part).
2. ``ComponentDefinition.default_package_ref()`` — the definition's default
   package choice, normalised from the first declared option.
An explicitly selected ``"family/variant"`` address must resolve in
*families*; otherwise resolution raises ``ValueError``.

Pure domain: the caller supplies the catalogue (``families``); this module does
no I/O.
"""

from __future__ import annotations

from typing import TYPE_CHECKING

from protoboard.domain.components.package.family import (
    PackageFamily,
    resolve_variant,
)
from protoboard.domain.components.package.pin_mapping import resolve_package_option
from protoboard.domain.components.package.shapes import Package

if TYPE_CHECKING:
    from protoboard.domain.components.definition import ComponentDefinition
    from protoboard.domain.components.footprints import Footprint
    from protoboard.domain.components.instance import ComponentInstance


def _resolve_variant_package(
    defn: ComponentDefinition,
    families: dict[str, PackageFamily],
    address: str | None,
) -> Package:
    option = resolve_package_option(defn, address, families)
    if address is None:
        address = option.ref
    variant = resolve_variant(families, address)
    if variant is None:
        raise ValueError(f"unresolved package variant {address!r}")
    return variant.package


def resolve_component_package(
    defn: ComponentDefinition,
    instance: ComponentInstance | None,
    families: dict[str, PackageFamily],
) -> Package:
    """The concrete package for a placed component, by precedence.

    instance.package_variant → defn.default_package_ref().
    An explicitly selected address that does not resolve raises ``ValueError``.
    """
    address = instance.package_variant if instance is not None else None
    return _resolve_variant_package(defn, families, address)


def resolve_package_variant_footprint(
    defn: ComponentDefinition,
    package_variant: str | None,
    families: dict[str, PackageFamily],
) -> Footprint:
    """Resolve the footprint for a pre-placement package choice.

    This is the ghost/picker path: there is no bound instance yet, but the UI
    may still need the geometry of a user-picked ``package_variant``. An
    explicitly selected variant must resolve.
    """
    return _resolve_variant_package(defn, families, package_variant)
