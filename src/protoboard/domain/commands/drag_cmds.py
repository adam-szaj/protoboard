# SPDX-License-Identifier: MIT
"""Atomic drag-commit command — unified drag model (2026-06-02).

This is the primary commit-time mutation for the new drag pipeline.
It replaces the legacy macro
``[TranslateAnchoredWires(c1), MoveComponent(c1, placement_only=True),
   ..., RemoveWire(w_k)]`` with one snapshot-and-apply step.

Why atomic:

The legacy macro translates wires one component at a time.  For a wire
between two drag-set members (both PinKnots in the same drag), the
first component's ``TranslateAnchoredWires`` shifts one endpoint while
the second is still in its old position — leaving the wire
**skewed**.  ``_update_wires_for_move``'s ``wire_has_skewed_segment``
guard then drops the wire as garbage, silently severing
cross-component connections.  See
``docs/archive/bug-drag-skewed-wire-shorts.md`` for the full bug report.

``MoveDragSetCommand`` translates every internal wire by **one delta**
in a single pass — no intermediate skewed state, no per-component
heuristics, no drop_skewed.  Wires the caller already decided to
remove (free / stationary-group / cross-group-disconnect via the
preference resolver) are removed in the same atomic step.
"""

from __future__ import annotations

import uuid
from dataclasses import dataclass, field
from typing import TYPE_CHECKING

import structlog

from protoboard.domain.commands.base import ConditionalCommand
from protoboard.domain.layout.knot import JointKnot, WirePinKnot
from protoboard.geometry import Point, Vector

if TYPE_CHECKING:
    from protoboard.domain.commands.base import ComponentState
    from protoboard.domain.layout.placement import ComponentPlacement
    from protoboard.domain.layout.wire import Fragment, Wire


__all__ = ["MoveDragSetCommand", "TransformDragSetCommand"]


_log = structlog.get_logger(__name__)


@dataclass
class MoveDragSetCommand(ConditionalCommand["ComponentState"]):
    """Atomic move of a drag set with one-delta translation of internal wires.

    Args:
        members: ``component_id → (old_placement, new_placement)`` for
            every component moving atomically.  ``new_placement.position
            - old_placement.position`` must equal ``delta`` for every
            entry (no per-component drift).
        internal_wire_ids: Wire IDs whose path/fragments will be
            translated by ``delta`` (the
            ``DragWireClassification.travels_with_drag`` set).
        dropped_wire_ids: Wire IDs to remove on commit (free,
            stationary-group, cross-group-disconnect after user choice).
            Orphan ``JointKnot`` cleanup is handled the same way as
            :class:`RemoveWireCommand` and snapshotted for revert.
        delta: The shared (dx, dy) translation vector — applied to
            ``internal_wire_ids`` paths and used as the consistency
            check for ``members``.

    Apply algorithm (single pass):

    1. Snapshot placements + internal wire paths/fragments + dropped
       wires (+ orphan joints) for revert.
    2. Set new placements for each member.
    3. Translate every wire in ``internal_wire_ids`` by ``delta``.
       PinKnot refs stay intact — they resolve by (component_id,
       pin_number) against the new placement.
    4. Remove each wire in ``dropped_wire_ids`` (delegates to
       ``Layout.remove_wire``, snapshotting orphan joints).
    5. Invalidate the layout's interior-vertex cache.

    No ``_update_wires_for_move``.  No ``wire_has_skewed_segment``.
    No per-component repeated translation.
    """

    members: dict[uuid.UUID, tuple["ComponentPlacement", "ComponentPlacement"]]
    internal_wire_ids: frozenset[uuid.UUID]
    dropped_wire_ids: frozenset[uuid.UUID]
    delta: Vector

    # Saved state for revert.
    _saved_placements: dict[uuid.UUID, "ComponentPlacement"] = field(
        default_factory=dict, init=False, repr=False,
    )
    _saved_geometry: "dict[uuid.UUID, tuple[list[Point], list[Fragment]]]" = (
        field(default_factory=dict, init=False, repr=False)
    )
    _saved_dropped_wires: list["Wire"] = field(
        default_factory=list, init=False, repr=False,
    )
    _saved_orphan_joints: list[JointKnot | WirePinKnot] = field(
        default_factory=list, init=False, repr=False,
    )

    @property
    def description(self) -> str:  # type: ignore[override]
        n = len(self.members)
        return f"Move {n} component{'s' if n != 1 else ''}"

    # ------------------------------------------------------------------
    # Apply
    # ------------------------------------------------------------------

    def apply(self, state: "ComponentState") -> None:
        # 1. Snapshot placements (deep copy so undo restores rotation/
        #    side too).  Use the ``old_placement`` already on hand —
        #    avoids a redundant lookup in state.layout.placements.
        saved_pl: dict[uuid.UUID, "ComponentPlacement"] = {}
        for cid, (old_pl, _new_pl) in self.members.items():
            saved_pl[cid] = old_pl
        self._saved_placements = saved_pl

        # 2. Snapshot internal wire paths + fragments.
        from protoboard.domain.layout.wire_utils import snapshot_wire_geometry

        self._saved_geometry = snapshot_wire_geometry(
            state.layout, self.internal_wire_ids,
        )

        # 3. Set new placements.
        for cid, (_old_pl, new_pl) in self.members.items():
            state.layout.placements[cid] = new_pl

        # 4. Translate internal wires by delta.  Fragment paths get the
        #    same translation; PinKnot refs are untouched (they resolve
        #    by component_id+pin_number against the new placement).
        from protoboard.domain.layout.wire import transform_wire_geometry

        dx, dy = self.delta.dx, self.delta.dy
        for wid in self.internal_wire_ids:
            wire = state.layout.wires.get(wid)
            if wire is None:
                continue
            transform_wire_geometry(wire, lambda p: Point(p.x + dx, p.y + dy))

        # 5. Remove dropped wires (and capture orphan joints for revert).
        #    Shared helper with RemoveWireCommand / DropWiresCommand —
        #    keep them in sync via wire_utils, not via copy-paste.
        from protoboard.domain.layout.wire_utils import (
            remove_wire_capturing_orphans,
        )
        saved_dropped: list["Wire"] = []
        saved_orphans: list[JointKnot | WirePinKnot] = []
        for wid in self.dropped_wire_ids:
            wire, orphans = remove_wire_capturing_orphans(state.layout, wid)
            if wire is None:
                continue
            saved_dropped.append(wire)
            saved_orphans.extend(orphans)
        self._saved_dropped_wires = saved_dropped
        self._saved_orphan_joints = saved_orphans

        # 6. Invalidate caches dependent on wire path geometry.
        state.layout.invalidate_interior_vertex_index()

        self.mark_applied(True)
        _log.debug(
            "drag.move_drag_set.applied",
            members=[str(c)[:8] for c in self.members],
            internal=[str(w)[:8] for w in self.internal_wire_ids],
            dropped=[str(w)[:8] for w in self.dropped_wire_ids],
            delta=(dx, dy),
        )

    # ------------------------------------------------------------------
    # Revert
    # ------------------------------------------------------------------

    def _do_revert(self, state: "ComponentState") -> None:
        # 1. Restore dropped wires (+ orphan joints) first, so the
        #    sibling re-translation below sees the full wire set.
        from protoboard.domain.layout.wire_utils import (
            restore_wire_with_orphans,
        )
        for wire in self._saved_dropped_wires:
            restore_wire_with_orphans(
                state.layout, wire, self._saved_orphan_joints,
            )

        # 2. Untranslate internal wires.
        from protoboard.domain.layout.wire_utils import restore_wire_geometry

        restore_wire_geometry(state.layout, self._saved_geometry)

        # 3. Restore old placements.
        from protoboard.domain.layout.placement import restore_placements

        restore_placements(state.layout.placements, self._saved_placements)

        # 4. Invalidate caches again so next paint sees the rewound state.
        state.layout.invalidate_interior_vertex_index()


@dataclass
class TransformDragSetCommand(ConditionalCommand["ComponentState"]):
    """Atomic full-pose change of a component set — one undo entry.

    The commit command for the unified *float-transform* mode (rotate +
    translate + flip, applied in any order off the undo stack before the
    user accepts).  Unlike :class:`MoveDragSetCommand` it is **purely
    snapshot-based**: it carries the complete before/after geometry and on
    ``apply`` writes the *after*, on ``revert`` writes the *before* — it
    never re-derives a transform, so it absorbs an arbitrary mix of
    operations.  Two differences from the move command:

    * ``members`` may differ per component in **rotation** and **side**, not
      just position — no single ``delta``.
    * It snapshots/restores free-knot (:class:`JointKnot` /
      :class:`WirePinKnot`) **positions**, because rotation moves them
      whereas pure translation does not (so ``MoveDragSetCommand`` skips
      them).  ``PinKnot`` positions are not stored — they resolve from the
      placement, which ``members`` already carries.

    Soft-conflict wire *removal* is deliberately NOT part of this command:
    the caller wraps it with a :class:`DropWiresCommand` in a
    :class:`MacroCommand` when the commit decision asks for drops, so the
    two concerns stay separable while remaining one undo step.

    Args:
        members: ``component_id → (old_placement, new_placement)``.
        wire_paths: ``wire_id → (old_path, new_path)`` for every wire whose
            geometry the transform changed (path points).
        wire_fragments: ``wire_id → (old_fragments, new_fragments)`` for the
            same wires (empty lists when a wire has no fragments).
        knot_positions: ``knot_id → (old_position, new_position)`` for every
            free knot the transform moved.
    """

    members: dict[uuid.UUID, tuple["ComponentPlacement", "ComponentPlacement"]]
    wire_paths: dict[uuid.UUID, tuple[list[Point], list[Point]]]
    wire_fragments: dict[
        uuid.UUID, tuple["list[Fragment]", "list[Fragment]"]
    ]
    knot_positions: dict[uuid.UUID, tuple[Point, Point]]

    @property
    def description(self) -> str:  # type: ignore[override]
        n = len(self.members)
        return f"Transform {n} component{'s' if n != 1 else ''}"

    def _write(self, state: "ComponentState", *, end: int) -> None:
        """Write one end of every snapshot to the layout.

        ``end`` indexes the (old, new) tuples: ``1`` writes the *after*
        state (apply), ``0`` the *before* (revert).  apply and revert are
        mirror images, so they share this single pass.
        """
        for cid, pls in self.members.items():
            state.layout.placements[cid] = pls[end]
        for wid, paths in self.wire_paths.items():
            wire = state.layout.wires.get(wid)
            if wire is not None:
                wire.path = list(paths[end])
        for wid, frags in self.wire_fragments.items():
            wire = state.layout.wires.get(wid)
            if wire is not None:
                wire.fragments = list(frags[end])
        for kid, positions in self.knot_positions.items():
            knot = state.layout.knots.get(kid)
            if knot is not None:
                state.layout.knots[kid] = knot.moved_to(positions[end])
        state.layout.invalidate_interior_vertex_index()

    def apply(self, state: "ComponentState") -> None:
        self._write(state, end=1)
        self.mark_applied(True)

    def _do_revert(self, state: "ComponentState") -> None:
        self._write(state, end=0)
