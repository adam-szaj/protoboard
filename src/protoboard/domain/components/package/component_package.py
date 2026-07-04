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
