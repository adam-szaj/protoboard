# SPDX-License-Identifier: MIT
"""Layout — physical realisation: placements and wires."""

from __future__ import annotations

import uuid
from dataclasses import dataclass, field

from protoboard.domain.layout.group import ComponentGroup
from protoboard.domain.layout.placement import ComponentPlacement
from protoboard.domain.layout.wire import WireSegment


@dataclass
class Layout:
    placements: dict[uuid.UUID, ComponentPlacement] = field(default_factory=dict)
    wires: list[WireSegment] = field(default_factory=list)
    groups: dict[uuid.UUID, ComponentGroup] = field(default_factory=dict)
