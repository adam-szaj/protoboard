# SPDX-License-Identifier: MIT
"""``PreviewState`` — the live session viewed *as if* a preview pose applied.

The armed transform (``ArmedTransform``) no longer mutates the session's layout
off the undo stack; it mutates a **preview** :class:`Layout` and leaves the live
one pristine (E5).  But the two subsystems that decide *whether the pose is
legal* — snap (nearest pad/pin) and conflict classification
(:func:`classify_drag_commit`) — read their pose exclusively off the session's
``layout`` and the reads *derived* from it (``comp_spatial_index`` /
``iter_pin_positions``).

``PreviewState`` bridges the gap: it wraps the real :class:`EditingContext` and swaps
in the preview layout, so those consumers see the transformed pose without the
live layout ever changing.  Every member is delegated to the live session except:

* ``layout`` — returns the preview layout;
* ``comp_spatial_index`` / ``iter_pin_positions`` — recomputed from the preview
  placements (the moved pins), so snap targets the previewed pose;

and connectivity-shaped reads (``cached_wire_net_map`` / ``connectivity_graph`` /
``pad_index``) delegate to the live session unchanged — during a transform the
*set* of wires and nets is fixed (geometry moves, topology does not), and
``classify_drag_commit`` builds its own ``PadIndex`` from ``self.layout`` (the
preview) anyway.

It is a plain delegating class rather than a subclass: :class:`EditingContext` is a
structural :class:`Protocol`, so satisfying it member-for-member is enough.
"""

from __future__ import annotations

import uuid
from typing import TYPE_CHECKING

if TYPE_CHECKING:
    from collections.abc import Iterable

    from protoboard.controller.editing.controller import EditController, EditingContext
    from protoboard.controller.editing.item_handle import ItemId
    from protoboard.domain.board.pad_state import PadIndex
    from protoboard.domain.connectivity.incremental import ConnectivityGraph
    from protoboard.domain.layout.group_tree import GroupTree
    from protoboard.domain.layout.layout import Layout
    from protoboard.domain.layout.pad_conflict import ConflictScanner
    from protoboard.domain.layout.wire import Wire
    from protoboard.geometry import Point, Rect


class PreviewState:
    """A read-through :class:`EditingContext` whose ``layout`` is a preview pose."""

    def __init__(self, live: EditingContext, preview: Layout) -> None:
        self._live = live
        # The swapped-in pose.  The delegated data members are plain attributes
        # (not properties): the ``EditingContext`` protocol declares them as settable,
        # so read-only properties would fail the structural match — and these are
        # immutable references shared straight through from the live session.
        self.layout = preview
        self.board = live.board
        self.instances = live.instances
        self.components = live.components
        self.schematic = live.schematic
        self.stack = live.stack
        self.package_families = live.package_families
        self.axis_label_settings = live.axis_label_settings
        # Settable EditingContext data members — plain attributes (not properties)
        # for the same reason as the block above: the protocol declares them
        # mutable, so a read-only property would fail the structural match.
        # Shared straight through from the live session.
        self.routing_settings = live.routing_settings
        self.selection_service = live.selection_service
        self.highlight_stack = live.highlight_stack

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

    def meta_pin_for_package_pin(
        self, comp_id: uuid.UUID, package_pin_number: int
    ) -> int | None:
        return self._live.meta_pin_for_package_pin(comp_id, package_pin_number)

    # -- delegated methods --------------------------------------------------

    def wire_spatial_index(self) -> dict[tuple[int, int], list[uuid.UUID]]:
        """Grid-cell → wire_id index, delegated to the live session.

        Unlike :meth:`comp_spatial_index`, a component-drag preview only swaps
        component *placements*; the wires are untouched, so the live session's
        index still reflects them.
        """
        return self._live.wire_spatial_index()

    def wire_by_id(self, wire_id: uuid.UUID) -> Wire | None:
        return self._live.wire_by_id(wire_id)

    def group_tree(self) -> GroupTree:
        return self._live.group_tree()

    def cached_wire_net_map(self) -> dict[uuid.UUID, uuid.UUID]:
        return self._live.cached_wire_net_map()

    def conflict_scanner(self, *, pad_index: PadIndex | None = None) -> ConflictScanner:
        return self._live.conflict_scanner(pad_index=pad_index)

    def pad_index(self) -> PadIndex:
        return self._live.pad_index()

    def connectivity_graph(self) -> ConnectivityGraph:
        return self._live.connectivity_graph()

    def get_pin_position(self, comp_id: uuid.UUID, pin_number: int) -> Point | None:
        return self._live.get_pin_position(comp_id, pin_number)

    def selection(self) -> frozenset[ItemId]:
        return self._live.selection()

    def select(self, ids: Iterable[ItemId]) -> None:
        self._live.select(ids)

    def notify_changed(self) -> None:
        self._live.notify_changed()

    def notify_active_tool_changed(self) -> None:
        self._live.notify_active_tool_changed()

    def notify_selection(
        self,
        comp_ids: frozenset[uuid.UUID],
        wire_ids: frozenset[uuid.UUID],
        net_id: uuid.UUID | None,
    ) -> None:
        self._live.notify_selection(comp_ids, wire_ids, net_id)

    @property
    def edit_controller(self) -> EditController:
        return self._live.edit_controller

    def set_routing_net(self, net_id: uuid.UUID | None) -> None:
        self._live.set_routing_net(net_id)

    @property
    def routing_net_id(self) -> uuid.UUID | None:
        return self._live.routing_net_id

    def component_bbox(self, comp_id: uuid.UUID) -> Rect | None:
        return self._live.component_bbox(comp_id)

    def cached_plated_through_pads(self) -> set[Point]:
        return self._live.cached_plated_through_pads()

    def invalidate_graph(self) -> None:
        self._live.invalidate_graph()
