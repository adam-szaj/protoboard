# SPDX-License-Identifier: MIT
"""DragCommitDecision — classifier result for component-drag commits.

Drives both the live drag overlay (red/yellow coloring + corner text) and the
LPM-release branching in SelectTool.  Pure data; no Qt imports.

Severity scale:

* CLEAR — no conflicts; safe to commit immediately.
* SOFT  — conflicts the user can accept ([Enter] / double-click LPM commits):
  pin lands on alien wire (auto-remove), shared pad with one/both unconnected
  pins (auto-create net), wires that became skewed after drag (auto-drop),
  wires that now cross alien pads (auto-drop).
* HARD  — conflicts the user cannot accept: pure body-on-body overlap,
  different-net pad-pad collision.  Commit is blocked; the user must move
  somewhere legal or press [Esc] to revert.
"""

from __future__ import annotations

import uuid
from dataclasses import dataclass, field
from enum import Enum
from typing import TYPE_CHECKING

from protoboard.geometry import Point

if TYPE_CHECKING:
    from protoboard.domain.layout.drag_classification import (
        DragWireClassification,
    )


class DragSeverity(Enum):
    CLEAR = "clear"
    SOFT = "soft"
    HARD = "hard"


@dataclass(frozen=True)
class DragCommitDecision:
    """Outcome of classifying a drag attempt against the current layout."""

    severity: DragSeverity
    """Worst severity across all moved components and dragged wires."""

    description: str = ""
    """Human-readable summary for the corner-text overlay.  Empty when CLEAR."""

    auto_remove_wire_ids: tuple[uuid.UUID, ...] = field(default_factory=tuple)
    """Wires that will be removed on SOFT commit (skewed, pin-on-foreign,
    crossing alien pads).  Empty for CLEAR/HARD."""

    auto_net_actions: tuple[object, ...] = field(default_factory=tuple)
    """Schematic edits to apply on SOFT commit (e.g. add pin to existing
    net, create new net).  Deferred as opaque objects so the dataclass stays
    Qt-free; SelectTool/placement_conflict know how to execute them."""

    blocking_pad_positions: tuple[Point, ...] = field(default_factory=tuple)
    """Pad positions that triggered HARD severity — drawn as red crosses."""

    overlay_pad_positions: tuple[Point, ...] = field(default_factory=tuple)
    """Pad/wire-point positions associated with SOFT severity — informational."""

    # --- Unified drag model (2026-06-02) ---
    # The fields below carry the 5-way wire classification produced by
    # ``DragDetector`` when ``drag_set_members`` is supplied on the
    # ``DragSnapshot``.  When None / empty, ``SelectTool`` falls back
    # to the legacy ``auto_remove_wire_ids`` pipeline.

    wire_classification: "DragWireClassification | None" = None
    """Full 5-way partition of wires under the drag; None when caller did
    not provide drag_set_members (legacy fallback path)."""

    free_drop_wire_ids: tuple[uuid.UUID, ...] = field(default_factory=tuple)
    """Wires the detector resolved as free (no owning group) and would
    drop on commit — subject to ``drag.free_wire_conflict`` preference."""

    stationary_group_drop_wire_ids: tuple[uuid.UUID, ...] = field(
        default_factory=tuple,
    )
    """Wires owned by stationary groups (not in drag set) that the
    detector wants to drop — subject to
    ``drag.stationary_group_wire_conflict`` preference."""

    cross_group_disconnect_wire_ids: tuple[uuid.UUID, ...] = field(
        default_factory=tuple,
    )
    """Wires that would be torn apart by the drag (one PinKnot in set,
    other outside) — subject to ``drag.cross_group_disconnect``
    preference (default ask-reroute)."""

    hard_locked_wire_pads: tuple[Point, ...] = field(default_factory=tuple)
    """Pad positions where a moved pin would land on a wire owned by a
    ``lock_internal_wires=True`` group — promotes severity to HARD."""

    @property
    def is_clear(self) -> bool:
        return self.severity == DragSeverity.CLEAR

    @property
    def is_soft(self) -> bool:
        return self.severity == DragSeverity.SOFT

    @property
    def is_hard(self) -> bool:
        return self.severity == DragSeverity.HARD

    @property
    def can_commit(self) -> bool:
        """True iff [Enter]/double-click LPM should commit the drag."""
        return self.severity != DragSeverity.HARD


__all__ = ["DragSeverity", "DragCommitDecision"]
