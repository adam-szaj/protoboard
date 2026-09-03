# SPDX-License-Identifier: MIT
"""Legacy wire → Wire/Knot/Fragment migration.

Converts ``Layout.wires`` (list of legacy ``Wire``) into the new
``Layout.wires`` + ``Layout.knots`` representation.  Used both by the
v1 → v2 forward-migration on load and by tests/tools that build legacy
layouts on the fly and want the new model.

The legacy model already represents a fragmentation of "logical" wires:
consecutive segments sharing an endpoint pad and the same ``insulated``
flag were always conceptually one user-meaningful wire, but the storage
split them whenever ``side`` changed.  This migration rebuilds the
logical wires by:

1. Resolving every legacy ``Wire.path[0]`` / ``path[-1]`` to either
   a ``PinKnot`` (if the endpoint coincides with the world position of
   some component pin) or a fresh ``JointKnot`` on the pad.

2. Inserting each segment as a single-fragment ``Wire`` between the two
   knots.

3. Walking every ``JointKnot`` and calling ``Layout.try_collapse_joint``
   until no more 2-incidence joints can be merged.  This is the standard
   policy from §3 of the refactor plan: merge iff both sides agree on
   ``insulated`` and the meeting fragments share ``side``.

Resulting wires keep the id of the first legacy segment they were built
from (the rest are dropped); other segment ids do not survive migration,
matching the design intent that a legacy file's user-visible wires are
the merged result.
"""

from __future__ import annotations

import uuid
from collections.abc import Callable
from dataclasses import dataclass
from typing import TYPE_CHECKING

import structlog

from protoboard.domain.layout.knot import JointKnot
from protoboard.domain.layout.layout import Layout
from protoboard.domain.layout.wire import Fragment, KnotRef, Wire
from protoboard.domain.layout.wire_utils import board_pads_for_side, plated_through_pads
from protoboard.domain.schematic.net import PinRef
from protoboard.geometry import Point

if TYPE_CHECKING:
    from protoboard.domain.board.board import Board

_log = structlog.get_logger(__name__)

PinPositionResolver = Callable[[uuid.UUID, int], Point | None]
"""Resolver mapping (component_id, pin_number) → world position.

Returns ``None`` if the component or its definition is unknown.  In
practice this is ``EditorSession.pin_position`` bound through a closure.
"""


@dataclass(frozen=True)
class MigrationReport:
    """Outcome of a legacy → v2 migration run."""

    legacy_wire_count: int
    new_wire_count: int
    knot_count: int
    merges_performed: int
    off_pad_endpoints: list[Point]  # diagnostic — should be empty in healthy projects

    @property
    def reduced_by(self) -> int:
        return self.legacy_wire_count - self.new_wire_count


def migrate_layout_to_v2(
    layout: Layout,
    board: Board,
    placements_pin_index: dict[Point, PinRef],
    pin_position_resolver: PinPositionResolver,
) -> MigrationReport:
    """Populate ``layout.wires`` + ``layout.knots`` from ``layout.wires``.

    Args:
        layout: target layout (mutated in place).  ``layout.wires`` stays
            untouched for now — Commit B drops it once consumers are
            migrated.
        board: board whose pads constrain joint placement (off-pad joints
            are illegal — flagged via ``off_pad_endpoints``).
        placements_pin_index: ``{world_position: PinRef}`` snapshot built
            by the caller from current placements.  Endpoint coincidences
            within ``1e-6`` are accepted.
        pin_position_resolver: callable returning a pin's world position;
            needed so ``PinKnot`` lookups survive layout edits.

    Returns:
        A ``MigrationReport`` with counts and any off-pad endpoint that
        could not be resolved to a pin.  The caller (e.g. the GUI) may
        surface this via a status-bar message.
    """
    if layout.wires or layout.knots:
        # Already migrated (or freshly v2-saved); idempotent no-op.
        return MigrationReport(
            legacy_wire_count=0,
            new_wire_count=len(layout.wires),
            knot_count=len(layout.knots),
            merges_performed=0,
            off_pad_endpoints=[],
        )

    # Wire the resolver into the layout so PinKnot positions can be
    # validated by `replace_wire_fragments` (used downstream).
    layout.set_pin_resolver(_make_pin_resolver(pin_position_resolver))

    # Snap-tolerant pin lookup.  Legacy paths sometimes carry fractional
    # rounding (handled separately by ``normalize_layout_positions``); we
    # accept anything within 1e-6 here.
    def find_pin(point: Point) -> PinRef | None:
        # Fast exact match first.
        hit = placements_pin_index.get(point)
        if hit is not None:
            return hit
        # Tolerant fallback.
        for pos, pin in placements_pin_index.items():
            if abs(pos.x - point.x) < 1e-6 and abs(pos.y - point.y) < 1e-6:
                return pin
        return None

    # Build pad lookups: all pads (any side) for off-pad detection,
    # plated-through subset for auto-merge via-aware merging.
    all_pads: set[Point] = board_pads_for_side(board, "top") | board_pads_for_side(
        board, "bottom"
    )
    plated_pads: frozenset[Point] = frozenset(plated_through_pads(board))

    off_pad: list[Point] = []
    legacy_wires: list[Wire] = list(layout.wires)

    # Pass 1: build knots + one Wire per legacy segment.
    joint_at: dict[Point, uuid.UUID] = {}

    def knot_for(point: Point) -> KnotRef:
        pin = find_pin(point)
        if pin is not None:
            pk = layout.get_or_create_pin_knot(pin)
            return KnotRef(id=pk.id)
        if point in joint_at:
            return KnotRef(id=joint_at[point])
        # Off-pad joints are illegal — surface a diagnostic, but go ahead
        # so the migration does not abort mid-project.  DRC will flag
        # this after the run.
        if not _point_on_pads(point, all_pads):
            off_pad.append(point)
        jk = layout.create_joint_knot(point)
        joint_at[point] = jk.id
        return KnotRef(id=jk.id)

    for seg in legacy_wires:
        start = seg.path[0]
        end = seg.path[-1]
        a_ref = knot_for(start)
        b_ref = knot_for(end)
        wire = Wire(
            id=seg.id,
            a=a_ref,
            b=b_ref,
            fragments=[Fragment(path=list(seg.path), side=seg.side)],
            insulated=seg.insulated,
            color=seg.color,
        )
        layout.add_wire(wire)

    # Pass 2: collapse all auto-mergeable 2-incidence joints.  Each merge
    # may create new opportunities (a merged wire might still terminate
    # at another collapsible joint), so iterate until stable.
    merges = 0
    while True:
        collapsible: list[uuid.UUID] = [
            k.id
            for k in layout.knots.values()
            if isinstance(k, JointKnot) and len(k.wire_ids) == 2
        ]
        if not collapsible:
            break
        progressed = False
        for kid in collapsible:
            if kid not in layout.knots:
                # Removed by a previous merge in this batch.
                continue
            if (
                layout.try_collapse_joint(
                    kid,
                    plated_through_pads=plated_pads,
                )
                is not None
            ):
                merges += 1
                progressed = True
        if not progressed:
            break

    report = MigrationReport(
        legacy_wire_count=len(legacy_wires),
        new_wire_count=len(layout.wires),
        knot_count=len(layout.knots),
        merges_performed=merges,
        off_pad_endpoints=off_pad,
    )

    if report.merges_performed > 0 or report.legacy_wire_count > 0:
        _log.info(
            "wire_migration.summary",
            legacy=report.legacy_wire_count,
            new=report.new_wire_count,
            knots=report.knot_count,
            merges=report.merges_performed,
            off_pad=len(off_pad),
        )
    if off_pad:
        _log.warning(
            "wire_migration.off_pad_endpoints",
            count=len(off_pad),
            sample=[(p.x, p.y) for p in off_pad[:5]],
        )
    return report


def build_pin_index(
    placements_pin_resolver: Callable[[], list[tuple[uuid.UUID, int, Point]]],
) -> dict[Point, PinRef]:
    """Helper for callers: build ``{position: PinRef}`` from a snapshot."""
    index: dict[Point, PinRef] = {}
    for comp_id, pin_num, pos in placements_pin_resolver():
        index[pos] = PinRef(component_id=comp_id, pin_number=pin_num)
    return index


def _make_pin_resolver(
    pin_position_resolver: PinPositionResolver,
) -> Callable[[PinRef], Point]:
    """Wrap the session-style resolver in a callable that matches PinResolver."""

    def resolve(pin: PinRef) -> Point:
        pos = pin_position_resolver(pin.component_id, pin.pin_number)
        if pos is None:
            raise RuntimeError(
                f"PinKnot {pin} cannot be resolved — component not in layout"
            )
        return pos

    return resolve


def _point_on_pads(point: Point, pads: set[Point]) -> bool:
    if point in pads:
        return True
    for p in pads:
        if abs(p.x - point.x) < 1e-6 and abs(p.y - point.y) < 1e-6:
            return True
    return False
