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
    from protoboard.domain.components.resolution import PlacedComponent
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

    def placed_component(self, comp_id: uuid.UUID) -> PlacedComponent | None:
        """Resolve component records against the preview placement map."""
        from protoboard.domain.components.resolution import resolve_placed_component

        return resolve_placed_component(
            comp_id,
            self.layout.placements,
            self.instances,
            self.components,
        )

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
        from protoboard.domain.layout.placement import iter_pin_world_positions

        return list(
            iter_pin_world_positions(
                comp_id,
                placements=self.layout.placements,
                instances=self.instances,
                components=self.components,
                families=self.package_families,
            )
        )

    def group_tree(self) -> GroupTree:
        return self._live.group_tree()

    def cached_wire_net_map(self) -> dict[uuid.UUID, uuid.UUID]:
        return self._live.cached_wire_net_map()
