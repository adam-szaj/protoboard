# SPDX-License-Identifier: MIT
"""Layout — physical realisation: placements and wires."""

from __future__ import annotations

import uuid
from dataclasses import dataclass, field

from protoboard.domain.layout.group import ComponentGroup
from protoboard.domain.layout.placement import ComponentPlacement
from protoboard.domain.layout.wire import WireSegment

ROOT_GROUP_ID: uuid.UUID = uuid.UUID("00000000-0000-0000-0000-000000000001")


@dataclass
class Layout:
    placements: dict[uuid.UUID, ComponentPlacement] = field(default_factory=dict)
    wires: list[WireSegment] = field(default_factory=list)
    groups: dict[uuid.UUID, ComponentGroup] = field(default_factory=dict)

    def ensure_root_group(self) -> None:
        """Ensure root group exists. Call after loading from disk."""
        if ROOT_GROUP_ID not in self.groups:
            self.groups[ROOT_GROUP_ID] = ComponentGroup(
                id=ROOT_GROUP_ID,
                name="",
                component_ids=frozenset(),
                child_group_ids=frozenset(),
            )
