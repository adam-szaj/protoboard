# SPDX-License-Identifier: MIT
"""Pure spatial-index builders shared by live and preview editing state."""

from __future__ import annotations

import uuid
from collections import defaultdict
from typing import TYPE_CHECKING

from protoboard.domain.layout.placement import pin_world_position

if TYPE_CHECKING:
    from protoboard.domain.components.definition import ComponentDefinition
    from protoboard.domain.components.instance import ComponentInstance
    from protoboard.domain.components.package import PackageFamily
    from protoboard.domain.layout.layout import Layout


def build_component_spatial_index(
    layout: Layout,
    instances: dict[uuid.UUID, ComponentInstance],
    components: dict[str, ComponentDefinition],
    families: dict[str, PackageFamily] | None = None,
) -> dict[tuple[int, int], list[uuid.UUID]]:
    """Build a grid-cell to component-id index from current placements."""
    from protoboard.domain.components.package import resolved_footprint

    index: dict[tuple[int, int], list[uuid.UUID]] = defaultdict(list)
    for component_id, placement in layout.placements.items():
        instance = instances.get(component_id)
        if instance is None:
            continue
        definition = components.get(instance.definition_ref)
        if definition is None:
            continue
        footprint = resolved_footprint(definition, instance, families)
        seen_cells: set[tuple[int, int]] = set()
        for pin_index in range(footprint.pin_count()):
            position = pin_world_position(footprint, placement, pin_index + 1)
            cell = (round(position.x), round(position.y))
            if cell not in seen_cells:
                seen_cells.add(cell)
                index[cell].append(component_id)
    return dict(index)
