# SPDX-License-Identifier: MIT
"""ComponentGroup — named, nestable collection of component instances."""

from __future__ import annotations

import uuid
from dataclasses import dataclass, field

from protoboard.geometry import Point


@dataclass(frozen=True)
class ExportedPin:
    """A pin exposed by a group as an external connection point."""

    component_id: uuid.UUID
    pin_number: int


@dataclass(frozen=True)
class ComponentGroup:
    """A named group of component instances and/or child groups.

    Groups form a tree (no cycles allowed).  ``component_ids`` holds only the
    *direct* members; child groups are referenced by ``child_group_ids``.
    To collect all component IDs recursively use
    ``groups_all_component_ids(groups, group_id)``.

    ``exported_pins`` declares which pins are visible as connection points from
    the parent group's editing context.
    """

    id: uuid.UUID
    name: str
    component_ids: frozenset[uuid.UUID] = field(default_factory=frozenset)
    child_group_ids: frozenset[uuid.UUID] = field(default_factory=frozenset)
    exported_pins: frozenset[ExportedPin] = field(default_factory=frozenset)
    rotation: int = 0
    origin: Point | None = None


def all_component_ids(
    groups: dict[uuid.UUID, ComponentGroup],
    group_id: uuid.UUID,
    _visited: frozenset[uuid.UUID] | None = None,
) -> frozenset[uuid.UUID]:
    """Return all component IDs reachable from *group_id* (recursive)."""
    visited = _visited or frozenset()
    if group_id in visited:
        return frozenset()
    visited = visited | {group_id}
    group = groups.get(group_id)
    if group is None:
        return frozenset()
    result: set[uuid.UUID] = set(group.component_ids)
    for child_id in group.child_group_ids:
        result |= all_component_ids(groups, child_id, visited)
    return frozenset(result)


def top_level_group_for(
    groups: dict[uuid.UUID, ComponentGroup],
    component_id: uuid.UUID,
) -> uuid.UUID | None:
    """Return the top-level group ID that directly or indirectly contains
    *component_id*, or None if the component is not in any group."""
    # Build parent map: group_id -> parent_group_id
    parent: dict[uuid.UUID, uuid.UUID] = {}
    for gid, g in groups.items():
        for child_id in g.child_group_ids:
            parent[child_id] = gid

    # Find any group that contains component_id (directly).
    containing: uuid.UUID | None = None
    for gid, g in groups.items():
        if component_id in g.component_ids:
            containing = gid
            break
    if containing is None:
        return None

    # Walk up to the root.
    current = containing
    while current in parent:
        current = parent[current]
    return current


def would_create_cycle(
    groups: dict[uuid.UUID, ComponentGroup],
    parent_id: uuid.UUID,
    child_id: uuid.UUID,
) -> bool:
    """Return True if adding *child_id* as a child of *parent_id* would create
    a cycle in the group hierarchy."""
    if parent_id == child_id:
        return True
    # Walk descendants of child_id; if parent_id is found, it's a cycle.
    visited: set[uuid.UUID] = set()
    stack = [child_id]
    while stack:
        current = stack.pop()
        if current in visited:
            continue
        visited.add(current)
        if current == parent_id:
            return True
        g = groups.get(current)
        if g is not None:
            stack.extend(g.child_group_ids)
    return False
