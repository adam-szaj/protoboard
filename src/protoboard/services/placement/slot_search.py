# SPDX-License-Identifier: MIT
"""Shared collision-aware placement-slot search primitives."""

from __future__ import annotations

import uuid

from protoboard.domain.components.definition import ComponentDefinition
from protoboard.domain.components.instance import ComponentInstance
from protoboard.domain.layout.pad_conflict import footprint_of
from protoboard.domain.layout.placement import ComponentPlacement, component_bbox
from protoboard.geometry import Point, Rect

ExistingPlacements = dict[
    uuid.UUID,
    tuple[ComponentPlacement, ComponentDefinition, ComponentInstance],
]


def has_collision(
    new_pl: ComponentPlacement,
    new_defn: ComponentDefinition,
    new_inst: ComponentInstance,
    existing: ExistingPlacements,
    extra_gap: float = 0.0,
) -> bool:
    """Return whether *new_pl* overlaps any existing component body.

    ``extra_gap`` inflates the candidate bounding box before testing, enforcing
    a minimum clearance independently of each component's body margin.
    """
    raw_new = component_bbox(
        footprint_of(new_defn, new_inst), new_pl, new_defn.component_class
    )
    new_bbox = Rect.of(
        x=raw_new.x - extra_gap,
        y=raw_new.y - extra_gap,
        lx=raw_new.lx + 2 * extra_gap,
        ly=raw_new.ly + 2 * extra_gap,
    )
    for other_pl, other_defn, other_inst in existing.values():
        other_bbox = component_bbox(
            footprint_of(other_defn, other_inst),
            other_pl,
            other_defn.component_class,
        )
        if new_bbox.intersects(other_bbox):
            return True
    return False


def find_free_slot(
    comp_id: uuid.UUID,
    defn: ComponentDefinition,
    inst: ComponentInstance,
    candidates: list[Point],
    rotations: list[int],
    side: str,
    existing: ExistingPlacements,
    extra_gap: float = 0.0,
) -> ComponentPlacement | None:
    """Return the first candidate/rotation pair without a body collision."""
    for pt in candidates:
        for rot in rotations:
            pl = ComponentPlacement(
                component_id=comp_id,
                position=pt,
                rotation=rot,
                side=side,  # type: ignore[arg-type]
            )
            if not has_collision(pl, defn, inst, existing, extra_gap=extra_gap):
                return pl
    return None


__all__ = ["ExistingPlacements", "find_free_slot", "has_collision"]
