# SPDX-License-Identifier: MIT
"""Board pads covered by top-mounted component bodies."""

from __future__ import annotations

import uuid
from collections.abc import Iterator

from protoboard.domain.board.board import Board
from protoboard.domain.board.boxes import PadAtom, StripAtom
from protoboard.domain.components.definition import ComponentDefinition
from protoboard.domain.components.instance import ComponentInstance
from protoboard.domain.components.resolution import iter_placed_components
from protoboard.domain.layout.layout import Layout
from protoboard.domain.layout.pad_conflict import footprint_of
from protoboard.domain.layout.placement import component_bbox
from protoboard.geometry import Point


def iter_under_component_pads(
    board: Board,
    layout: Layout,
    components: dict[str, ComponentDefinition],
    instances: dict[uuid.UUID, ComponentInstance],
) -> Iterator[Point]:
    """Yield board pad centres covered by top-mounted component bodies."""
    for component in iter_placed_components(layout.placements, instances, components):
        if component.placement.side != "top":
            continue
        bbox = component_bbox(
            footprint_of(component.definition, component.instance),
            component.placement,
        )
        for atom, pos in board.atoms():
            if isinstance(atom, (PadAtom, StripAtom)) and bbox.contains(pos):
                yield pos
