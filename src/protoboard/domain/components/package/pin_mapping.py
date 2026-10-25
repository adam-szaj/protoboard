# SPDX-License-Identifier: MIT
"""Translate logical MetaPins through a component's active package option."""

from __future__ import annotations

from collections.abc import Mapping
from typing import TYPE_CHECKING

if TYPE_CHECKING:
    from protoboard.domain.components.definition import (
        ComponentDefinition,
        PackageOption,
    )
    from protoboard.domain.components.instance import ComponentInstance
    from protoboard.domain.components.package.family import PackageFamily


def resolve_package_option(
    defn: ComponentDefinition,
    package_variant: str | None,
    families: Mapping[str, PackageFamily] | None = None,
) -> PackageOption:
    """Select a declared option, including variants of a declared family.

    Shared by geometry, MetaPin mapping, rendering and pre-placement consumers.
    A concrete option permits only its own address; a family option permits the
    family's existing variants. ``None`` selects the definition default.
    """
    if package_variant is None:
        return defn.default_package_option()
    for option in defn.package_options:
        if option.ref == package_variant:
            return option
        family = families.get(option.ref) if families is not None else None
        if family is not None and any(
            family.address(variant.variant_id) == package_variant
            for variant in family.variants
        ):
            return option
    raise ValueError(
        f"component {defn.name!r} has no package option for {package_variant!r}"
    )


def package_pins_for_meta_pin(
    defn: ComponentDefinition,
    instance: ComponentInstance | None,
    meta_pin_index: int,
) -> tuple[int, ...]:
    """Return physical pins carrying a 1-based MetaPin in the active option."""
    defn.meta_pin(meta_pin_index)
    option = resolve_package_option(
        defn,
        instance.package_variant if instance is not None else None,
        instance.families if instance is not None else None,
    )
    return option.pin_map.get(meta_pin_index, ())


def meta_pin_for_package_pin(
    defn: ComponentDefinition,
    instance: ComponentInstance | None,
    package_pin_number: int,
) -> int | None:
    """Return the MetaPin carried by a physical pin, or None when unmapped."""
    if package_pin_number < 1:
        raise ValueError("package pin number must be positive")
    for meta_pin_index, package_pins in resolve_package_option(
        defn,
        instance.package_variant if instance is not None else None,
        instance.families if instance is not None else None,
    ).pin_map.items():
        if package_pin_number in package_pins:
            return meta_pin_index
    return None
