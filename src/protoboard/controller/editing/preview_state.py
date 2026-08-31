# SPDX-License-Identifier: MIT
"""Project reads viewed through a preview component pose.

The armed transform (``ArmedTransform``) no longer mutates the session's layout
off the undo stack; it mutates a **preview** :class:`Layout` and leaves the live
one pristine (E5).  But the two subsystems that decide *whether the pose is
legal* — snap (nearest pad/pin) and conflict classification
(:func:`classify_drag_commit`) — read their pose exclusively off the session's
``layout`` and the reads *derived* from it (``comp_spatial_index`` /
``iter_pin_positions``).

``PreviewState`` wraps the live project context and swaps in the preview layout,
so those consumers see the transformed pose without changing the live layout.
It exposes only the project and spatial reads required by preview calculations:

* ``layout`` — returns the preview layout;
* ``comp_spatial_index`` / ``iter_pin_positions`` — recomputed from the preview
  placements (the moved pins), so snap targets the previewed pose;

The group tree and wire-net map delegate to the live session because a component
transform changes geometry, not group membership or connectivity.
"""

from __future__ import annotations

import uuid
from typing import TYPE_CHECKING

if TYPE_CHECKING:
    from protoboard.controller.editing.contexts import PreviewProjectContext
    from protoboard.domain.layout.group_tree import GroupTree
    from protoboard.domain.layout.layout import Layout
    from protoboard.geometry import Point


class PreviewState:
    """A project context whose ``layout`` is a preview pose."""

    def __init__(self, live: PreviewProjectContext, preview: Layout) -> None:
        self._live = live
        self.layout = preview
        self.board = live.board
        self.instances = live.instances
        self.components = live.components
        self.schematic = live.schematic
        self.package_families = live.package_families
        self.axis_label_settings = live.axis_label_settings

    # -- the swapped-in pose (preview) --------------------------------------

    def comp_spatial_index(self) -> dict[tuple[int, int], list[uuid.UUID]]:
        """Grid-cell → comp_id index from the *preview* placements (uncached).

        Snap buckets candidates through this; it must reflect the moved pose,
        so it is rebuilt from the preview each call rather than served from the
        live session's placement-keyed cache.
        """
        from protoboard.controller.editing.spatial_index import (
            build_component_spatial_index,
        )

        return build_component_spatial_index(
            self.layout, self.instances, self.components, self.package_families
        )

    def iter_pin_positions(self, comp_id: uuid.UUID) -> list[tuple[int, Point]]:
        """``[(pin_number, world_pos), …]`` for *comp_id* at the preview pose."""
        from protoboard.domain.layout.pad_conflict import (
            resolved_footprint_or_default,
        )
        from protoboard.domain.layout.placement import resolve_pin_world_position

        inst = self.instances.get(comp_id)
        if inst is None:
            return []
        defn = self.components.get(inst.definition_ref)
        if defn is None:
            return []
        result: list[tuple[int, Point]] = []
        for n in range(resolved_footprint_or_default(defn, inst).pin_count()):
            pin_num = n + 1
            pos = resolve_pin_world_position(
                comp_id,
                pin_num,
                placements=self.layout.placements,
                instances=self.instances,
                components=self.components,
                families=self.package_families,
            )
            if pos is not None:
                result.append((pin_num, pos))
        return result

    def group_tree(self) -> GroupTree:
        return self._live.group_tree()

    def cached_wire_net_map(self) -> dict[uuid.UUID, uuid.UUID]:
        return self._live.cached_wire_net_map()
