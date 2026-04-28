# SPDX-License-Identifier: MIT
"""Layout — physical realisation: placements, wires, strip cuts."""

from __future__ import annotations

import uuid
from dataclasses import dataclass, field

from protoboard.domain.layout.cut import StripCut
from protoboard.domain.layout.placement import ComponentPlacement
from protoboard.domain.layout.wire import WireSegment


@dataclass
class Layout:
    placements: dict[uuid.UUID, ComponentPlacement] = field(default_factory=dict)
    wires: list[WireSegment] = field(default_factory=list)
    cuts: list[StripCut] = field(default_factory=list)
