# SPDX-License-Identifier: MIT
"""Component model — footprints, definitions, instances."""

from protoboard.domain.components.resolution import (
    PlacedComponent,
    ResolvedComponent,
    iter_placed_components,
    resolve_component,
    resolve_instance_definition,
    resolve_placed_component,
)

__all__ = [
    "PlacedComponent",
    "ResolvedComponent",
    "iter_placed_components",
    "resolve_component",
    "resolve_instance_definition",
    "resolve_placed_component",
]
