# SPDX-License-Identifier: MIT
"""Resolve the records and package context of a placed component."""

from __future__ import annotations

import uuid
from dataclasses import dataclass
from typing import TYPE_CHECKING

from protoboard.domain.components.package.pin_mapping import resolve_package_option

if TYPE_CHECKING:
    from protoboard.domain.components.definition import (
        ComponentDefinition,
        PackageOption,
    )
    from protoboard.domain.components.instance import ComponentInstance
    from protoboard.domain.components.package.shapes import Package
    from protoboard.domain.layout.placement import ComponentPlacement, PlacementMap


@dataclass(frozen=True, slots=True)
class ResolvedComponent:
    instance: ComponentInstance
    definition: ComponentDefinition
    placement: ComponentPlacement
    package_option: PackageOption
    package: Package | None


def resolve_component(
    component_id: uuid.UUID,
    placements: PlacementMap,
    instances: dict[uuid.UUID, ComponentInstance],
    definitions: dict[str, ComponentDefinition],
) -> ResolvedComponent | None:
    """Return a placed component's complete context, or ``None`` if absent."""
    placement = placements.get(component_id)
    instance = instances.get(component_id)
    if placement is None or instance is None:
        return None
    definition = definitions.get(instance.definition_ref)
    if definition is None:
        return None
    package_option = resolve_package_option(definition, instance)
    package = instance.resolved_package() if instance.definition is not None else None
    return ResolvedComponent(
        instance=instance,
        definition=definition,
        placement=placement,
        package_option=package_option,
        package=package,
    )
