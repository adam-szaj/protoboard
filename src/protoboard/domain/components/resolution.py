# SPDX-License-Identifier: MIT
"""Resolve the records and package context of a placed component."""

from __future__ import annotations

import uuid
from collections.abc import Iterator
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
class PlacedComponent:
    """The records that identify a component currently present in a layout."""

    placement: ComponentPlacement
    instance: ComponentInstance
    definition: ComponentDefinition


def resolve_instance_definition(
    component_id: uuid.UUID,
    instances: dict[uuid.UUID, ComponentInstance],
    definitions: dict[str, ComponentDefinition],
) -> tuple[ComponentInstance, ComponentDefinition] | None:
    """Return an instance and its catalogue definition, or ``None``."""
    instance = instances.get(component_id)
    if instance is None:
        return None
    definition = definitions.get(instance.definition_ref)
    if definition is None:
        return None
    return instance, definition


def resolve_placed_component(
    component_id: uuid.UUID,
    placements: PlacementMap,
    instances: dict[uuid.UUID, ComponentInstance],
    definitions: dict[str, ComponentDefinition],
) -> PlacedComponent | None:
    """Return a placed component's structural records, or ``None`` if absent."""
    placement = placements.get(component_id)
    resolved = resolve_instance_definition(component_id, instances, definitions)
    if placement is None or resolved is None:
        return None
    instance, definition = resolved
    return PlacedComponent(
        placement=placement,
        instance=instance,
        definition=definition,
    )


def iter_placed_components(
    placements: PlacementMap,
    instances: dict[uuid.UUID, ComponentInstance],
    definitions: dict[str, ComponentDefinition],
) -> Iterator[PlacedComponent]:
    """Yield resolvable placed components in layout iteration order."""
    for component_id in placements:
        component = resolve_placed_component(
            component_id, placements, instances, definitions
        )
        if component is not None:
            yield component


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
    placed = resolve_placed_component(component_id, placements, instances, definitions)
    if placed is None:
        return None
    package_option = resolve_package_option(
        placed.definition,
        placed.instance.package_variant,
        placed.instance.families,
    )
    package = (
        placed.instance.resolved_package()
        if placed.instance.definition is not None
        else None
    )
    return ResolvedComponent(
        instance=placed.instance,
        definition=placed.definition,
        placement=placed.placement,
        package_option=package_option,
        package=package,
    )
