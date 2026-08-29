# SPDX-License-Identifier: MIT
"""Translate logical MetaPins through a component's active package option."""

from __future__ import annotations

from typing import TYPE_CHECKING

if TYPE_CHECKING:
    from protoboard.domain.components.definition import (
        ComponentDefinition,
        PackageOption,
    )
    from protoboard.domain.components.instance import ComponentInstance


def resolve_package_option(
    defn: "ComponentDefinition",
    instance: "ComponentInstance | None",
) -> "PackageOption":
    """Return the option selected by the instance, or the definition default."""
    address = instance.package_variant if instance is not None else None
    if address is None:
        return defn.package_options[0]
    for option in defn.package_options:
        if option.ref == address:
            return option
    raise ValueError(
        f"component {defn.name!r} has no package option for {address!r}"
    )


def package_pins_for_meta_pin(
    defn: "ComponentDefinition",
    instance: "ComponentInstance | None",
    meta_pin_index: int,
) -> tuple[int, ...]:
    """Return physical pins carrying a 1-based MetaPin in the active option."""
    defn.meta_pin(meta_pin_index)
    return resolve_package_option(defn, instance).pin_map.get(meta_pin_index, ())


def meta_pin_for_package_pin(
    defn: "ComponentDefinition",
    instance: "ComponentInstance | None",
    package_pin_number: int,
) -> int | None:
    """Return the MetaPin carried by a physical pin, or None when unmapped."""
    if package_pin_number < 1:
        raise ValueError("package pin number must be positive")
    for meta_pin_index, package_pins in resolve_package_option(
        defn, instance
    ).pin_map.items():
        if package_pin_number in package_pins:
            return meta_pin_index
    return None
