# SPDX-License-Identifier: MIT
"""ComponentGroup — named, nestable collection of component instances."""

from __future__ import annotations

import uuid
from dataclasses import dataclass, field
from typing import TYPE_CHECKING

from protoboard.geometry import Point

if TYPE_CHECKING:
    from protoboard.domain.components.instance import ComponentInstance
    from protoboard.domain.components.definition import ComponentDefinition
    from protoboard.domain.schematic.schematic import Schematic


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
    # Unified drag model (2026-06-02): when True, this group's internal
    # wires are treated as a HARD obstacle for foreign drags (a moved
    # pin landing on one of them blocks the commit instead of being
    # auto-resolved).  Default False preserves legacy soft-conflict
    # behaviour.  Toggled via PreferencesDialog default + per-group
    # Properties checkbox.
    lock_internal_wires: bool = False


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


def external_pins_for_components(
    component_ids: frozenset[uuid.UUID],
    schematic: "Schematic",
) -> frozenset[ExportedPin]:
    """Return pins on *component_ids* that connect to the world outside the set.

    A pin is "external" if its net either:
      * contains a pin on a component outside *component_ids*, or
      * is a global net (VCC/GND etc.) — by definition reaches the world.

    Pure function — no side effects, no Qt.  Used to seed exported_pins when
    creating/extending a group and by the "Export auto" context-menu entry.
    """
    result: set[ExportedPin] = set()
    for net in schematic.nets.values():
        member_pins = [p for p in net.pins if p.component_id in component_ids]
        if not member_pins:
            continue
        has_outside = any(p.component_id not in component_ids for p in net.pins)
        if has_outside or net.global_net:
            for p in member_pins:
                result.add(ExportedPin(component_id=p.component_id, pin_number=p.pin_number))
    return frozenset(result)


def internal_net_ids_for_components(
    component_ids: frozenset[uuid.UUID],
    schematic: "Schematic",
) -> frozenset[uuid.UUID]:
    """Return the IDs of nets that are fully internal to *component_ids*.

    A net is internal when:
      * every one of its pins sits on a component in *component_ids*, **and**
      * the net is not marked global (global nets reach the whole board
        and are routed at the project level, not per-group).

    Used by the "Autoroute group" action to restrict routing to the
    connections living inside a group (and, by extension, inside its
    sub-groups when *component_ids* is the recursive flattening).
    """
    result: set[uuid.UUID] = set()
    for net in schematic.nets.values():
        if net.global_net:
            continue
        if not net.pins:
            continue
        if all(p.component_id in component_ids for p in net.pins):
            result.add(net.id)
    return frozenset(result)


def all_pins_for_components(
    component_ids: frozenset[uuid.UUID],
    instances: dict[uuid.UUID, "ComponentInstance"],
    definitions: dict[str, "ComponentDefinition"],
) -> frozenset[ExportedPin]:
    """Return every pin of every component in *component_ids*.

    Used by the "Export all pins" context-menu entry.  Skips components or
    definitions that cannot be resolved (defensive — should not happen in a
    well-formed layout, but the menu handler is best-effort).
    """
    result: set[ExportedPin] = set()
    for cid in component_ids:
        inst = instances.get(cid)
        if inst is None:
            continue
        defn = definitions.get(inst.definition_ref)
        if defn is None:
            continue
        for pin_num in range(1, defn.footprint.pin_count() + 1):
            result.add(ExportedPin(component_id=cid, pin_number=pin_num))
    return frozenset(result)


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


def top_level_group_under(
    groups: dict[uuid.UUID, ComponentGroup],
    active_id: uuid.UUID,
    comp_id: uuid.UUID,
) -> uuid.UUID | None:
    """Return the "top-level" group containing *comp_id* under *active_id*.

    "Top-level under active" means: walking the group tree downward from
    *active_id*, the first child group on the path to *comp_id*.

    Examples (with active = A):
      - tree A[B[C[c1]]], comp_id=c1 → returns B (the direct child of A
        whose subtree contains c1).
      - tree A[c1, B[c2]], comp_id=c1 → returns None (c1 is a direct
        member of active itself, not nested in any child group).
      - tree A[c1], B[c2] (B sibling of A), comp_id=c2 → returns None
        (c2 lives outside active's subtree; promotion does not reach it).

    Used by Ctrl+G's selection-promotion rule: a selected component
    nested in a child group is treated as proxy for the whole top-level
    structure containing it under the current editing context.
    """
    active = groups.get(active_id)
    if active is None:
        return None
    for child_id in active.child_group_ids:
        if comp_id in all_component_ids(groups, child_id):
            return child_id
    return None


def promote_selection_to_top_level(
    groups: dict[uuid.UUID, ComponentGroup],
    active_id: uuid.UUID,
    selected_comp_ids: frozenset[uuid.UUID],
) -> tuple[frozenset[uuid.UUID], frozenset[uuid.UUID]]:
    """Split *selected_comp_ids* into (free components, top-level group IDs).

    For each selected component, find its "top-level under active" group
    (see :func:`top_level_group_under`).  If found, the component is
    consumed by that group — the group ID lands in the second return
    value, and every component the group recursively contains is
    removed from the first return value.

    Returns:
        (free_comp_ids, child_group_ids) — disjoint sets safe to pass
        directly as ``component_ids`` and ``child_group_ids`` of a
        newly created :class:`ComponentGroup`.

    Order-independent: the same input produces the same output
    regardless of ``selected_comp_ids`` iteration order.
    """
    promoted_groups: set[uuid.UUID] = set()
    consumed_components: set[uuid.UUID] = set()

    for comp_id in selected_comp_ids:
        if comp_id in consumed_components:
            continue
        top_gid = top_level_group_under(groups, active_id, comp_id)
        if top_gid is None:
            continue
        promoted_groups.add(top_gid)
        consumed_components |= all_component_ids(groups, top_gid)

    free_components = frozenset(selected_comp_ids - consumed_components)
    return free_components, frozenset(promoted_groups)


def parent_group_of(
    groups: dict[uuid.UUID, ComponentGroup],
    child_id: uuid.UUID,
) -> uuid.UUID | None:
    """Return the group that lists *child_id* in its ``child_group_ids``.

    Each group can have at most one parent (the tree invariant), so the
    first match wins.  Returns None if no group references *child_id*.
    """
    for gid, g in groups.items():
        if child_id in g.child_group_ids:
            return gid
    return None
