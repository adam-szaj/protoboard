# SPDX-License-Identifier: MIT
"""Structured result of classifying a proposed component drag."""

from __future__ import annotations

import uuid
from dataclasses import dataclass, field
from enum import Enum, StrEnum

from protoboard.domain.layout.drag_classification import DragWireClassification
from protoboard.geometry import Point


class DragSeverity(Enum):
    CLEAR = "clear"
    SOFT = "soft"
    HARD = "hard"


class DragReasonCode(StrEnum):
    BODY_OVERLAP = "body_overlap"
    DIFFERENT_NETS = "different_nets"
    SHARED_PAD = "shared_pad"
    SAME_NET_WIRE = "same_net_wire"
    FOREIGN_WIRE = "foreign_wire"
    ALIEN_PAD_CROSSING = "alien_pad_crossing"
    SKEWED_WIRE = "skewed_wire"
    LOCKED_GROUP_WIRE = "locked_group_wire"
    ROUTE_ALIEN_PAD_CROSSING = "route_alien_pad_crossing"


@dataclass(frozen=True)
class DragReason:
    code: DragReasonCode
    refdes: str | None = None
    pin_number: int | None = None
    related_refdes: str | None = None
    count: int = 1


@dataclass(frozen=True)
class DragAnalysis:
    """Conflict facts and commit actions for one proposed drag pose."""

    severity: DragSeverity
    reasons: tuple[DragReason, ...] = field(default_factory=tuple)
    auto_remove_wire_ids: tuple[uuid.UUID, ...] = field(default_factory=tuple)
    blocking_pad_positions: tuple[Point, ...] = field(default_factory=tuple)
    overlay_pad_positions: tuple[Point, ...] = field(default_factory=tuple)
    wire_classification: DragWireClassification | None = None
    free_drop_wire_ids: tuple[uuid.UUID, ...] = field(default_factory=tuple)
    stationary_group_drop_wire_ids: tuple[uuid.UUID, ...] = field(default_factory=tuple)
    cross_group_disconnect_wire_ids: tuple[uuid.UUID, ...] = field(
        default_factory=tuple
    )
    hard_locked_wire_pads: tuple[Point, ...] = field(default_factory=tuple)

    @property
    def is_clear(self) -> bool:
        return self.severity is DragSeverity.CLEAR

    @property
    def is_soft(self) -> bool:
        return self.severity is DragSeverity.SOFT

    @property
    def is_hard(self) -> bool:
        return self.severity is DragSeverity.HARD

    @property
    def can_commit(self) -> bool:
        return self.severity is not DragSeverity.HARD
