# SPDX-License-Identifier: MIT
"""Pure spatial-index builders shared by live and preview editing state."""

from __future__ import annotations

import uuid
from collections import defaultdict
from typing import TYPE_CHECKING

from protoboard.domain.components.package.component_package import resolved_footprint
from protoboard.domain.components.resolution import iter_placed_components
from protoboard.domain.layout.placement import iter_footprint_pin_positions

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
    index: dict[tuple[int, int], list[uuid.UUID]] = defaultdict(list)
    for component in iter_placed_components(
        layout.placements,
        instances,
        components,
    ):
        component_id = component.placement.component_id
        placement = component.placement
        footprint = resolved_footprint(
            component.definition,
            component.instance,
            families,
        )
        seen_cells: set[tuple[int, int]] = set()
        for _pin_number, position in iter_footprint_pin_positions(footprint, placement):
            cell = (round(position.x), round(position.y))
            if cell not in seen_cells:
                seen_cells.add(cell)
                index[cell].append(component_id)
    return dict(index)
