# SPDX-License-Identifier: MIT
"""Stable DRC result exposed by the application boundary."""

from __future__ import annotations

import uuid
from dataclasses import dataclass, field
from typing import Literal

from protoboard.domain.schematic.net import PinRef
from protoboard.geometry import Point

Severity = Literal["error", "warning", "info"]
_VALID_SEVERITIES = frozenset({"error", "warning", "info"})


@dataclass(frozen=True)
class DRCIssue:
    severity: Severity
    code: str
    message: str
    locations: list[Point]
    affected_nets: list[uuid.UUID]
    affected_pins: list[PinRef] = field(default_factory=list)
    affected_wires: list[uuid.UUID] = field(default_factory=list)
    affected_components: list[uuid.UUID] = field(default_factory=list)

    def __post_init__(self) -> None:
        if self.severity not in _VALID_SEVERITIES:
            raise ValueError(
                f"severity must be one of {_VALID_SEVERITIES}; got {self.severity!r}"
            )
