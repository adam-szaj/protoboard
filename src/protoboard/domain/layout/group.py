# SPDX-License-Identifier: MIT
"""ComponentGroup — named, nestable collection of component instances."""

from __future__ import annotations

import dataclasses
import uuid
from dataclasses import dataclass, field

from protoboard.domain.schematic.net import PinRef
from protoboard.geometry import Transform2D


@dataclass(frozen=True)
class ComponentGroup:
    """A named group of component instances and/or child groups.

    Groups form a tree (no cycles allowed).  ``component_ids`` holds only the
    *direct* members; child groups are referenced by ``child_group_ids``.
    To collect all component IDs recursively use
    ``GroupTree(groups).all_component_ids(group_id)``.

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
