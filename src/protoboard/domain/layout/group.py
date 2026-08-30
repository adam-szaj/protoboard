# SPDX-License-Identifier: MIT
"""ComponentGroup — named, nestable collection of component instances."""

from __future__ import annotations

import dataclasses
import uuid
from dataclasses import dataclass, field
from typing import TYPE_CHECKING

from protoboard.domain.schematic.net import PinRef
from protoboard.geometry import Transform2D

if TYPE_CHECKING:
    from protoboard.domain.components.definition import ComponentDefinition
    from protoboard.domain.components.instance import ComponentInstance
    from protoboard.domain.schematic.schematic import Schematic


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
    exported_pins: frozenset[PinRef] = field(default_factory=frozenset)
    # The group's own affine transform in its parent's frame (scene-graph
    # model) — the SINGLE source of truth for the group pose. A member's world
    # position composes the ancestor group transforms with the member-local
    # placement (see PlacementCollection / GroupTree.world_transform).
    # Identity default → composes to a no-op, so a group with the default
    # transform reads exactly as an ungrouped component.  The legacy stored
    # ``rotation`` / ``origin`` pair is gone: ``rotation`` is the derived view
    # below, the bbox top-left is computed on demand
    # (``GroupTree.group_origin``), and the rotate pivot derives from the
    # transform (``RotateGroupCommand``).  Old files still load — the v2→v3
    # persistence migration folds a stored pair into ``transform``.
    transform: Transform2D = field(default_factory=Transform2D.identity)
    # Unified drag model (2026-06-02): when True, this group's internal
    # wires are treated as a HARD obstacle for foreign drags (a moved
    # pin landing on one of them blocks the commit instead of being
    # auto-resolved).  Default False preserves legacy soft-conflict
    # behaviour.  Toggled via PreferencesDialog default + per-group
    # Properties checkbox.
    lock_internal_wires: bool = False

    @property
    def rotation(self) -> int:
        """Derived view: the transform's CW rotation in degrees (0/90/180/270).

        Meaningful for ``det > 0`` transforms (rotation + translation — the
        only kind group rotate/move produce); a flipped group (``det < 0``)
        reports the quantiser's identity fallback. Kept under the legacy field
        name so readers survive the stored-pair removal.
        """
        return (self.transform.rotation_steps() * 90) % 360

    def replace(self, **changes: object) -> ComponentGroup:
        """Return a copy of this group with the given fields replaced.

        Thin wrapper over :func:`dataclasses.replace` so callers (commands,
        GroupTree) don't field-list the dataclass by hand — which previously
        dropped ``lock_internal_wires`` on every copy.
        """
        return dataclasses.replace(self, **changes)  # type: ignore[arg-type]


# ---------------------------------------------------------------------------
# Group-tree queries.
#
# The behaviour lives on :class:`GroupTree` (``group_tree.py``); these
# module-level functions are thin compatibility shims so the ~12 caller modules
# keep importing the familiar names. New code should prefer
# ``GroupTree(groups).<method>`` (or ``EditorSession.group_tree()``).
# ``GroupTree`` is imported lazily to avoid a circular import
# (``group_tree`` imports ``ComponentGroup`` from here).
# ---------------------------------------------------------------------------


def all_component_ids(
    groups: dict[uuid.UUID, ComponentGroup],
    group_id: uuid.UUID,
    _visited: frozenset[uuid.UUID] | None = None,
) -> frozenset[uuid.UUID]:
    """Return all component IDs reachable from *group_id* (recursive)."""
    from protoboard.domain.layout.group_tree import GroupTree

    return GroupTree(groups).all_component_ids(group_id)


def exposable_surface(
    groups: dict[uuid.UUID, ComponentGroup],
    group_id: uuid.UUID,
    instances: dict[uuid.UUID, ComponentInstance],
    definitions: dict[str, ComponentDefinition],
) -> frozenset[PinRef]:
    """Pins that may legally appear in ``G.exported_pins``.

    Shim for :meth:`GroupTree.exposable_surface`.
    """
    from protoboard.domain.layout.group_tree import GroupTree

    return GroupTree(groups).exposable_surface(group_id, instances, definitions)


def validate_exported_pins(
    groups: dict[uuid.UUID, ComponentGroup],
    group_id: uuid.UUID,
) -> frozenset[PinRef]:
    """Subset of ``G.exported_pins`` that violates encapsulation.

    Shim for :meth:`GroupTree.validate_exported_pins`.
    """
    from protoboard.domain.layout.group_tree import GroupTree

    return GroupTree(groups).validate_exported_pins(group_id)


def top_level_group_for(
    groups: dict[uuid.UUID, ComponentGroup],
    component_id: uuid.UUID,
) -> uuid.UUID | None:
    """Top-level group containing *component_id*, or None.

    Shim for :meth:`GroupTree.top_level_group_for`.
    """
    from protoboard.domain.layout.group_tree import GroupTree

    return GroupTree(groups).top_level_group_for(component_id)


def external_pins_for_group(
    groups: dict[uuid.UUID, ComponentGroup],
    group_id: uuid.UUID,
    schematic: Schematic,
) -> frozenset[PinRef]:
    """Minimal re-export set for *group_id* satisfying encapsulation.

    Shim for :meth:`GroupTree.external_pins_for_group`.
    """
    from protoboard.domain.layout.group_tree import GroupTree

    return GroupTree(groups).external_pins_for_group(group_id, schematic)


def internal_net_ids_for_components(
    component_ids: frozenset[uuid.UUID],
    schematic: Schematic,
) -> frozenset[uuid.UUID]:
    """IDs of nets fully internal to *component_ids*.

    Shim for :meth:`GroupTree.internal_net_ids_for_components` (the empty-tree
    instance suffices — this query needs no group hierarchy).
    """
    from protoboard.domain.layout.group_tree import GroupTree

    return GroupTree({}).internal_net_ids_for_components(component_ids, schematic)


def schematic_projection_for_group(
    component_ids: frozenset[uuid.UUID],
    schematic: Schematic,
) -> Schematic:
    """Projection of *schematic* keeping only pins on *component_ids*.

    Shim for :meth:`GroupTree.schematic_projection_for_group`.
    """
    from protoboard.domain.layout.group_tree import GroupTree

    return GroupTree({}).schematic_projection_for_group(component_ids, schematic)


def would_create_cycle(
    groups: dict[uuid.UUID, ComponentGroup],
    parent_id: uuid.UUID,
    child_id: uuid.UUID,
) -> bool:
    """True if adding *child_id* under *parent_id* would create a cycle.

    Shim for :meth:`GroupTree.would_create_cycle`.
    """
    from protoboard.domain.layout.group_tree import GroupTree

    return GroupTree(groups).would_create_cycle(parent_id, child_id)


def top_level_group_under(
    groups: dict[uuid.UUID, ComponentGroup],
    active_id: uuid.UUID,
    comp_id: uuid.UUID,
) -> uuid.UUID | None:
    """First child group under *active_id* on the path to *comp_id*.

    Shim for :meth:`GroupTree.top_level_group_under`.
    """
    from protoboard.domain.layout.group_tree import GroupTree

    return GroupTree(groups).top_level_group_under(active_id, comp_id)


def promote_selection_to_top_level(
    groups: dict[uuid.UUID, ComponentGroup],
    active_id: uuid.UUID,
    selected_comp_ids: frozenset[uuid.UUID],
) -> tuple[frozenset[uuid.UUID], frozenset[uuid.UUID]]:
    """Split *selected_comp_ids* into (free components, top-level group IDs).

    Shim for :meth:`GroupTree.promote_selection_to_top_level`.
    """
    from protoboard.domain.layout.group_tree import GroupTree

    return GroupTree(groups).promote_selection_to_top_level(
        active_id, selected_comp_ids
    )


def parent_group_of(
    groups: dict[uuid.UUID, ComponentGroup],
    child_id: uuid.UUID,
) -> uuid.UUID | None:
    """The group listing *child_id* in its ``child_group_ids``, or None.

    Shim for :meth:`GroupTree.parent_group_of`.
    """
    from protoboard.domain.layout.group_tree import GroupTree

    return GroupTree(groups).parent_group_of(child_id)
