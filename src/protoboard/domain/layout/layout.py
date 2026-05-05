# SPDX-License-Identifier: MIT
"""Layout — physical realisation: placements and wires."""

from __future__ import annotations

import uuid
from dataclasses import dataclass, field

import structlog
from protoboard.domain.layout.group import ComponentGroup
from protoboard.domain.layout.placement import ComponentPlacement
from protoboard.domain.layout.wire import WireSegment

ROOT_GROUP_ID: uuid.UUID = uuid.UUID("00000000-0000-0000-0000-000000000001")

_log = structlog.get_logger(__name__)


@dataclass
class Layout:
    placements: dict[uuid.UUID, ComponentPlacement] = field(default_factory=dict)
    wires: list[WireSegment] = field(default_factory=list)
    groups: dict[uuid.UUID, ComponentGroup] = field(default_factory=dict)

    def ensure_root_group(self) -> None:
        """Ensure root group exists and all non-root groups are reachable from it.

        Called after loading from disk. Adopts any orphan groups (groups not
        referenced as a child by any other group) into root's child_group_ids so
        that the SelectTool can find them via _hit_test_group_outline.
        """
        if ROOT_GROUP_ID not in self.groups:
            self.groups[ROOT_GROUP_ID] = ComponentGroup(
                id=ROOT_GROUP_ID,
                name="",
                component_ids=frozenset(),
                child_group_ids=frozenset(),
            )

        # Collect all group IDs that are referenced as children by some group.
        referenced: set[uuid.UUID] = set()
        for g in self.groups.values():
            referenced |= set(g.child_group_ids)

        # Any non-root group not referenced by anyone is an orphan → attach to root.
        orphans = frozenset(
            gid for gid in self.groups
            if gid != ROOT_GROUP_ID and gid not in referenced
        )
        if orphans:
            _log.warning(
                "layout.orphan_groups_adopted",
                count=len(orphans),
                ids=[str(g) for g in orphans],
            )
            root = self.groups[ROOT_GROUP_ID]
            self.groups[ROOT_GROUP_ID] = ComponentGroup(
                id=root.id,
                name=root.name,
                component_ids=root.component_ids,
                child_group_ids=root.child_group_ids | orphans,
                exported_pins=root.exported_pins,
            )
