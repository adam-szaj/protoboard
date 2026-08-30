# SPDX-License-Identifier: MIT
"""High-level drag-set abstraction for the SelectTool (Phase 3).

A ``DragSet`` is what the user has *selected* and is about to drag — a
combination of:

* **free components** (top-level, not locked inside any non-active group)
* **whole groups** (selected as monolithic units)
* **wires** (selected explicitly or as part of a multi-selection)

This is distinct from the command-level
:class:`protoboard.domain.commands.drag_cmds.MoveDragSetCommand` which
operates on a flat dict of ``component_id → (old_pl, new_pl)`` plus a
classified wire partition.  The GUI builds a ``DragSet`` first; on commit
the drag-detector expands it into the flat form the command expects.

Why a separate type:

* ``MoveDragSetCommand.members`` is the *final* expanded set after
  groups are decomposed into their component_ids.  The pre-commit
  conflict-check needs to know *which group* each component came from so
  it can apply spec-rev-3 rule 4 (SOFT for intra-drag-set collisions).
* Selection state in ``SelectTool`` is stored as three independent fields
  (``_selected_ids``, ``_selected_wire_ids``, ``_selected_group_ids``).
  ``DragSet`` is the immutable snapshot of those at press-time, frozen
  for the duration of the drag.

Pure Python — no Qt, no PadIndex, no side effects.  Tested without
spinning up a ``QApplication``.
"""

from __future__ import annotations

import uuid
from collections.abc import Callable
from dataclasses import dataclass, field
from typing import TYPE_CHECKING

if TYPE_CHECKING:
    from protoboard.domain.layout.group import ComponentGroup


__all__ = ["DragSet"]


@dataclass(frozen=True, slots=True)
class DragSet:
    """Immutable snapshot of what the user is dragging.

    Attributes:
        free_components: Top-level component IDs that are *not* members
            of any group whose `gid` appears in :attr:`groups`.  These
            move individually under one shared delta.
        groups: Group IDs to be moved as monolithic units.  Every direct
            and transitive member of these groups participates in the
            drag.  Empty by default.
        wires: Explicit wire IDs in the selection.  Note that wires
            owned by a moving component / group are picked up
            automatically by the wire classifier (``drag_classification``)
            and need not be listed here — this set is for wires the
            user *also* selected explicitly (e.g. rubber-band over a
            free wire).
    """

    free_components: frozenset[uuid.UUID] = field(default_factory=frozenset)
    groups: frozenset[uuid.UUID] = field(default_factory=frozenset)
    wires: frozenset[uuid.UUID] = field(default_factory=frozenset)

    def all_component_ids(
        self,
        groups_map: dict[uuid.UUID, ComponentGroup],
    ) -> frozenset[uuid.UUID]:
        """All component IDs participating in this drag.

        Includes :attr:`free_components` plus every direct and transitive
        member of every group in :attr:`groups` (resolved via
        :func:`protoboard.domain.layout.group.all_component_ids`).

        ``groups_map`` is the session's current
        ``layout.groups`` dict — passed in rather than stored on the
        ``DragSet`` so the structure stays pure-data and the resolution
        always reflects the latest group hierarchy.
        """
        from protoboard.domain.layout.group import all_component_ids as _all_member_ids

        result: set[uuid.UUID] = set(self.free_components)
        for gid in self.groups:
            result |= _all_member_ids(groups_map, gid)
        return frozenset(result)

    @property
    def is_empty(self) -> bool:
        """True when nothing would move under this drag."""
        return not (self.free_components or self.groups or self.wires)

    def with_added(
        self,
        *,
        free_components: frozenset[uuid.UUID] | None = None,
        groups: frozenset[uuid.UUID] | None = None,
        wires: frozenset[uuid.UUID] | None = None,
    ) -> DragSet:
        """Return a new DragSet with the given members union-added.

        Convenience for incremental construction in tests.  ``None``
        means "no change".  No deduplication side-effects on the
        original — DragSet is frozen.
        """
        return DragSet(
            free_components=(
                self.free_components | free_components
                if free_components is not None
                else self.free_components
            ),
            groups=(self.groups | groups if groups is not None else self.groups),
            wires=(self.wires | wires if wires is not None else self.wires),
        )

    @classmethod
    def from_selection(
        cls,
        *,
        selected_ids: set[uuid.UUID] | frozenset[uuid.UUID],
        selected_wire_ids: set[uuid.UUID] | frozenset[uuid.UUID],
        selected_group_ids: set[uuid.UUID] | frozenset[uuid.UUID],
        groups_map: dict[uuid.UUID, ComponentGroup],
        is_locked_in_group: Callable[[uuid.UUID], bool],
    ) -> DragSet:
        """Build a DragSet from SelectTool's storage fields.

        Args:
            selected_ids: Union of multi-component selection + primary
                component.  May contain components that belong to a
                non-active group (locked-in-group).  Locked components
                are sorted into two buckets:
                * member of a group in ``selected_group_ids`` → kept as
                  part of that group's monolith (not duplicated in
                  ``free_components``)
                * member of some other group → dropped (the user did
                  not select that group as a whole, so the locked
                  component cannot move alone)
            selected_wire_ids: Wire selection set (multi + primary
                merged).  Passed through verbatim.
            selected_group_ids: Group IDs selected as monoliths.
            groups_map: Current layout group dict.
            is_locked_in_group: Callable that returns True iff a
                component belongs to a non-active group.  Provided as a
                callback so this function stays decoupled from
                ``SelectTool._is_locked_in_group``'s context-tracking.

        Returns:
            DragSet with ``free_components`` containing only unlocked
            top-level IDs, ``groups`` mirroring ``selected_group_ids``,
            and ``wires`` mirroring ``selected_wire_ids``.
        """
        from protoboard.domain.layout.group import all_component_ids as _all_member_ids

        groups = frozenset(selected_group_ids)

        # Components covered by selected groups — these don't enter
        # `free_components` (they move via the group monolith).
        covered_by_selected_group: set[uuid.UUID] = set()
        for gid in groups:
            covered_by_selected_group |= _all_member_ids(groups_map, gid)

        free: set[uuid.UUID] = set()
        for cid in selected_ids:
            if cid in covered_by_selected_group:
                continue  # picked up by the group monolith
            if is_locked_in_group(cid):
                continue  # locked in a group not in selection — cannot move
            free.add(cid)

        return cls(
            free_components=frozenset(free),
            groups=groups,
            wires=frozenset(selected_wire_ids),
        )
