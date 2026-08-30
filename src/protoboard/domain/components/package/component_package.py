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
from protoboard.domain.components.package.shapes import Package

if TYPE_CHECKING:
    from protoboard.domain.components.definition import ComponentDefinition
    from protoboard.domain.components.footprints import Footprint
    from protoboard.domain.components.instance import ComponentInstance


def package_to_footprint(package: Package) -> Footprint:
    """Return the package through its directly implemented footprint API."""
    return package


def _resolve_variant_package(
    families: dict[str, PackageFamily],
    address: str | None,
) -> Package | None:
    if not address:
        return None
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
    chosen = _resolve_variant_package(
        families,
        instance.package_variant if instance is not None else None,
    )
    if chosen is not None:
        return chosen
    default_ref = defn.default_package_ref()
    if default_ref is None:
        raise ValueError(f"component {defn.name!r} has no package option")
    chosen = _resolve_variant_package(families, default_ref)
    if chosen is None:
        raise ValueError(f"unresolved default package {default_ref!r}")
    return chosen


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
    chosen = _resolve_variant_package(families, package_variant)
    return (
        package_to_footprint(chosen)
        if chosen is not None
        else defn.default_footprint(families)
    )


def resolved_footprint(
    defn: ComponentDefinition,
    instance: ComponentInstance | None,
    families: dict[str, PackageFamily] | None,
) -> Footprint:
    """The footprint a placed component actually uses — variant-aware.

    Thin wrapper over :meth:`ComponentInstance.resolved_footprint` for call
    sites that hold ``(defn, instance, families)`` separately. It does **not**
    silently fall back to the definition default for a missing/unbound instance:
    a placed component without a bound instance reaching a geometry path is the
    silent-wrong-body bug this model removes, so it raises
    :class:`~protoboard.errors.UnboundInstanceError`.

    The *families* argument is accepted for source-compatibility but is
    redundant once the instance is bound (it carries its own catalogue); when
    *instance* is unbound this raises regardless.

    For the legitimate *pre-placement* case — a picker/ghost that has no
    instance yet — call :func:`resolve_component_package` directly (or use
    ``defn.default_footprint()`` explicitly), so the no-instance path is visible at the
    call site rather than hidden behind a fallback here.
    """
    if instance is None:
        from protoboard.errors import UnboundInstanceError

        raise UnboundInstanceError(
            f"resolved_footprint called for {defn.name!r} with no instance; "
            "placed-component geometry must come from a bound instance "
            "(pre-placement callers must use resolve_component_package "
            "explicitly)"
        )
    return instance.resolved_footprint()
