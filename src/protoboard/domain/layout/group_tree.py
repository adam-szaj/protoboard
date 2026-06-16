# SPDX-License-Identifier: MIT
"""GroupTree — behaviour over a group hierarchy.

:class:`ComponentGroup` is a frozen value object holding only its *own* direct
members; the recursive and cross-net queries about the group *tree* need the
whole ``groups`` dict (and sometimes the :class:`Schematic`). Rather than drag
the world onto the value object, ``GroupTree`` owns the groups dict in a private
field and exposes those queries as methods.

It is a thin *view* over a live dict (the same shape as ``DragSet`` /
``DragSnapshot`` holding references to live session dicts): ``frozen=True`` as a
value, never mutating the dict it wraps. Pure domain — no Qt — so it is testable
headlessly. The free functions in ``group.py`` delegate here.
"""

from __future__ import annotations

import uuid
from dataclasses import dataclass
from typing import TYPE_CHECKING

from protoboard.domain.layout.group import ComponentGroup, ExportedPin

if TYPE_CHECKING:
    from protoboard.domain.components.definition import ComponentDefinition
    from protoboard.domain.components.instance import ComponentInstance
    from protoboard.domain.schematic.schematic import Schematic


@dataclass(frozen=True)
class GroupTree:
    """Tree queries over a group hierarchy. Wraps a live ``groups`` dict by
    reference; never mutates it."""

    _groups: dict[uuid.UUID, ComponentGroup]

    # ------------------------------------------------------------------
    # Membership / hierarchy
    # ------------------------------------------------------------------

    def all_component_ids(self, group_id: uuid.UUID) -> frozenset[uuid.UUID]:
        """All component IDs reachable from *group_id* (recursive)."""
        return self._all_component_ids(group_id, frozenset())

    def _all_component_ids(
        self, group_id: uuid.UUID, visited: frozenset[uuid.UUID]
    ) -> frozenset[uuid.UUID]:
        if group_id in visited:
            return frozenset()
        visited = visited | {group_id}
        group = self._groups.get(group_id)
        if group is None:
            return frozenset()
        result: set[uuid.UUID] = set(group.component_ids)
        for child_id in group.child_group_ids:
            result |= self._all_component_ids(child_id, visited)
        return frozenset(result)

    def top_level_group_for(
        self, component_id: uuid.UUID
    ) -> uuid.UUID | None:
        """Top-level group that directly/indirectly contains *component_id*,
        or None if it is in no group."""
        parent: dict[uuid.UUID, uuid.UUID] = {}
        for gid, g in self._groups.items():
            for child_id in g.child_group_ids:
                parent[child_id] = gid

        containing: uuid.UUID | None = None
        for gid, g in self._groups.items():
            if component_id in g.component_ids:
                containing = gid
                break
        if containing is None:
            return None

        current = containing
        while current in parent:
            current = parent[current]
        return current

    def top_level_group_under(
        self, active_id: uuid.UUID, comp_id: uuid.UUID
    ) -> uuid.UUID | None:
        """Walking down from *active_id*, the first child group on the path to
        *comp_id* (None if *comp_id* is a direct member of active or outside its
        subtree)."""
        active = self._groups.get(active_id)
        if active is None:
            return None
        for child_id in active.child_group_ids:
            if comp_id in self.all_component_ids(child_id):
                return child_id
        return None

    def parent_group_of(self, child_id: uuid.UUID) -> uuid.UUID | None:
        """The group listing *child_id* in its ``child_group_ids`` (tree
        invariant → at most one), or None."""
        for gid, g in self._groups.items():
            if child_id in g.child_group_ids:
                return gid
        return None

    def would_create_cycle(
        self, parent_id: uuid.UUID, child_id: uuid.UUID
    ) -> bool:
        """True if adding *child_id* under *parent_id* would create a cycle."""
        if parent_id == child_id:
            return True
        visited: set[uuid.UUID] = set()
        stack = [child_id]
        while stack:
            current = stack.pop()
            if current in visited:
                continue
            visited.add(current)
            if current == parent_id:
                return True
            g = self._groups.get(current)
            if g is not None:
                stack.extend(g.child_group_ids)
        return False

    def promote_selection_to_top_level(
        self,
        active_id: uuid.UUID,
        selected_comp_ids: frozenset[uuid.UUID],
    ) -> tuple[frozenset[uuid.UUID], frozenset[uuid.UUID]]:
        """Split *selected_comp_ids* into (free components, top-level group IDs)
        under *active_id*. See :meth:`top_level_group_under`."""
        promoted_groups: set[uuid.UUID] = set()
        consumed_components: set[uuid.UUID] = set()

        for comp_id in selected_comp_ids:
            if comp_id in consumed_components:
                continue
            top_gid = self.top_level_group_under(active_id, comp_id)
            if top_gid is None:
                continue
            promoted_groups.add(top_gid)
            consumed_components |= self.all_component_ids(top_gid)

        free_components = frozenset(selected_comp_ids - consumed_components)
        return free_components, frozenset(promoted_groups)

    # ------------------------------------------------------------------
    # Export surface / validation
    # ------------------------------------------------------------------

    def exposable_surface(
        self,
        group_id: uuid.UUID,
        instances: dict[uuid.UUID, "ComponentInstance"],
        definitions: dict[str, "ComponentDefinition"],
    ) -> frozenset[ExportedPin]:
        """Pins that may legally appear in ``G.exported_pins``: every pin of
        every direct member ∪ each direct child's already-exported pins."""
        group = self._groups.get(group_id)
        if group is None:
            return frozenset()
        surface: set[ExportedPin] = set()
        for cid in group.component_ids:
            inst = instances.get(cid)
            if inst is None:
                continue
            defn = definitions.get(inst.definition_ref)
            if defn is None:
                continue
            for pin_num in range(1, defn.footprint.pin_count() + 1):
                surface.add(ExportedPin(component_id=cid, pin_number=pin_num))
        for child_id in group.child_group_ids:
            child = self._groups.get(child_id)
            if child is None:
                continue
            surface |= child.exported_pins
        return frozenset(surface)

    def validate_exported_pins(
        self, group_id: uuid.UUID
    ) -> frozenset[ExportedPin]:
        """Subset of ``G.exported_pins`` that violates encapsulation (component
        neither a direct member of G nor re-exported by a direct child)."""
        group = self._groups.get(group_id)
        if group is None:
            return frozenset()
        direct_members = group.component_ids
        child_exports: set[ExportedPin] = set()
        for child_id in group.child_group_ids:
            child = self._groups.get(child_id)
            if child is None:
                continue
            child_exports |= child.exported_pins
        invalid: set[ExportedPin] = set()
        for ep in group.exported_pins:
            if ep.component_id in direct_members:
                continue
            if ep in child_exports:
                continue
            invalid.add(ep)
        return frozenset(invalid)

    def external_pins_for_group(
        self, group_id: uuid.UUID, schematic: "Schematic"
    ) -> frozenset[ExportedPin]:
        """Minimal re-export set for *group_id*: for every net reaching outside
        G's recursive members (or global), at least one pin from G's exposable
        surface."""
        group = self._groups.get(group_id)
        if group is None:
            return frozenset()

        direct_members = group.component_ids
        recursive_members = self.all_component_ids(group_id)

        child_reexports: set[ExportedPin] = set()
        for child_id in group.child_group_ids:
            child = self._groups.get(child_id)
            if child is None:
                continue
            child_reexports |= child.exported_pins

        result: set[ExportedPin] = set()
        for net in schematic.nets.values():
            if not net.pins:
                continue
            has_inside = any(
                p.component_id in recursive_members for p in net.pins
            )
            if not has_inside:
                continue
            has_outside = any(
                p.component_id not in recursive_members for p in net.pins
            )
            if not (has_outside or net.global_net):
                continue
            for p in net.pins:
                if p.component_id in direct_members:
                    result.add(
                        ExportedPin(
                            component_id=p.component_id,
                            pin_number=p.pin_number,
                        )
                    )
            net_pin_set = {(p.component_id, p.pin_number) for p in net.pins}
            for ep in child_reexports:
                if (ep.component_id, ep.pin_number) in net_pin_set:
                    result.add(ep)
        return frozenset(result)

    # ------------------------------------------------------------------
    # Net projection (component-set queries — no group tree needed, but they
    # live here so all group/schematic projection logic shares one home)
    # ------------------------------------------------------------------

    def internal_net_ids_for_components(
        self,
        component_ids: frozenset[uuid.UUID],
        schematic: "Schematic",
    ) -> frozenset[uuid.UUID]:
        """IDs of nets fully internal to *component_ids* (all pins inside, not
        global)."""
        result: set[uuid.UUID] = set()
        for net in schematic.nets.values():
            if net.global_net:
                continue
            if not net.pins:
                continue
            if all(p.component_id in component_ids for p in net.pins):
                result.add(net.id)
        return frozenset(result)

    def schematic_projection_for_group(
        self,
        component_ids: frozenset[uuid.UUID],
        schematic: "Schematic",
    ) -> "Schematic":
        """Projection of *schematic* keeping only pins on *component_ids*; nets
        with fewer than two surviving pins are dropped."""
        from protoboard.domain.schematic.net import Net
        from protoboard.domain.schematic.schematic import Schematic

        projected_nets: dict[uuid.UUID, Net] = {}
        for net in schematic.nets.values():
            internal_pins = {
                p for p in net.pins if p.component_id in component_ids
            }
            if len(internal_pins) < 2:
                continue
            projected_nets[net.id] = Net(
                id=net.id,
                name=net.name,
                pins=internal_pins,
                color=net.color,
                global_net=net.global_net,
            )
        return Schematic(nets=projected_nets)


__all__ = ["GroupTree"]
