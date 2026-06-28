# SPDX-License-Identifier: MIT
"""Single source of truth for drag-conflict classification.

`DragDetector` consumes a :class:`DragSnapshot` (immutable view of the
layout *as it currently looks* under a hypothetical drag), delegates
every per-pad question to a :class:`PadStrategy`, and produces a
:class:`DragCommitDecision` describing what the commit would do.

The same detector drives the live drag overlay (called per snap) and
the release-time commit pipeline (called once on mouse-up). Previously
these were two independent implementations that drifted: the live
overlay missed bottom-side wires and mid-segment crossings while the
commit path did not, leading to bugs `1328a8e`, `ee489a7`, `e28346a`
(see ``docs/archive/drag-pipeline-refactor-plan.md`` Context).

Pure logic — no Qt, no side effects on the layout. The snapshot's
``pad_index`` is assumed to already reflect the dragged positions
(callers either clone-and-mutate or pass a stale one and accept the
limitation).
"""

from __future__ import annotations

import uuid
from dataclasses import dataclass, field
from typing import TYPE_CHECKING

import structlog

from protoboard.domain.layout.drag_classification import (
    DragWireClassification,
    classify_wires_for_drag,
)
from protoboard.domain.layout.pad_conflict import (
    ConflictKind,
    PadConflict,
    _round,
    find_conflicts,
    find_wire_conflicts,
    find_wires_crossing_pads,
    footprint_of,
)
from protoboard.domain.layout.placement import (
    component_bbox,
    pin_world_position,
)
from protoboard.domain.layout.wire_utils import find_skewed_wires
from protoboard.geometry import Point
from protoboard.gui.tools.drag_commit_decision import (
    DragCommitDecision,
    DragSeverity,
)
from protoboard.services.pad_strategy import PadStrategy, Verdict

if TYPE_CHECKING:
    from protoboard.domain.board.pad_state import PadIndex
    from protoboard.domain.components.definition import ComponentDefinition
    from protoboard.domain.components.instance import ComponentInstance
    from protoboard.domain.layout.group import ComponentGroup
    from protoboard.domain.layout.knot import Knot
    from protoboard.domain.layout.placement import ComponentPlacement
    from protoboard.domain.layout.wire import Wire
    from protoboard.domain.schematic.schematic import Schematic

_log = structlog.get_logger(__name__)


def _dbg_enabled() -> bool:
    """Cheap gate for hot-path debug logs (see ``select_tool._dbg_enabled``)."""
    import logging
    return logging.getLogger(__name__).isEnabledFor(logging.DEBUG)


@dataclass(frozen=True)
class DragSnapshot:
    """Immutable view of the layout for one classify() call.

    ``pad_index`` is the preferred authority — when supplied, it must
    already reflect the moved components' positions (clone + apply by
    the caller). When ``None``, :class:`DragDetector` falls back to
    the legacy from-layout paths in :func:`find_conflicts` /
    :func:`find_wire_conflicts` (slower; used by test stubs and by
    sessions whose ``board`` is not available).

    ``placements``/``schematic``/``instances``/``components`` are
    needed for body-overlap classification (no pad_index fast path
    exists) and for the legacy fallbacks. All dicts are referenced,
    not copied — the caller must not mutate the underlying layout
    between :meth:`DragDetector.classify` and consumption of the
    result.

    Unified drag model (2026-06-02) extras — when ``drag_set_members``
    is non-empty the detector also produces a
    :class:`DragWireClassification` and partitions wire-removal hints
    into preference-bucketed sets (free / stationary-group /
    cross-group-disconnect).  Legacy callers passing only the older
    arguments still get the original single-bucket
    ``auto_remove_wire_ids`` result.
    """

    pad_index: "PadIndex | None"
    placements: dict[uuid.UUID, "ComponentPlacement"]
    instances: dict[uuid.UUID, "ComponentInstance"]
    components: dict[str, "ComponentDefinition"]
    schematic: "Schematic"
    wires: list["Wire"] = field(default_factory=list)
    drag_set_members: frozenset[uuid.UUID] = field(default_factory=frozenset)
    """Component IDs the drag moves atomically.  Empty disables the
    new classification path; ``classify`` falls back to the legacy
    auto-remove-everything behaviour."""

    wire_net_map: dict[uuid.UUID, uuid.UUID] = field(default_factory=dict)
    """``wire.id → net.id`` map (typically
    ``EditorSession.cached_wire_net_map()``).  Required by half-orphan
    classification — without it, all half-orphans land in the
    ``half_orphan_external`` bucket (drop on commit)."""

    knots: "dict[uuid.UUID, Knot]" = field(default_factory=dict)
    """``layout.knots`` — required for endpoint flavour resolution
    in the wire classifier."""

    member_groups: "list[ComponentGroup]" = field(default_factory=list)
    """Groups whose ``component_ids`` overlap ``drag_set_members``.
    Empty when dragging a free component or a pure-free multi-selection;
    used only for half-orphan internal-net resolution."""

    stationary_locked_groups: "list[ComponentGroup]" = field(
        default_factory=list,
    )
    """Stationary groups (not in drag set) with
    ``lock_internal_wires=True``.  Owned wires of these groups promote
    soft conflicts to HARD when a moved pin lands on them."""


class DragDetector:
    """Stateless classifier — one instance per drag operation.

    The strategy is injected at construction so multiple subsystems
    (drag, simplify-preview, place-tool conflict resolution) can plug
    different rule sets without changing call sites.
    """

    def __init__(self, strategy: PadStrategy) -> None:
        self._strategy = strategy

    # ------------------------------------------------------------------
    # Public entry point
    # ------------------------------------------------------------------

    def classify(
        self,
        snapshot: DragSnapshot,
        moved_comp_ids: set[uuid.UUID],
        dragged_wire_ids: frozenset[uuid.UUID] = frozenset(),
    ) -> DragCommitDecision:
        """Single entry point — return a DragCommitDecision for the
        snapshot's current state.

        Severity hierarchy (worst wins):

        * HARD — pure body-overlap, DIFFERENT_NETS pin-on-pin.
        * SOFT — pin-on-wire (any net), shared pad (one/both unconnected),
          wires crossing alien pads, wires that became skewed.
        * CLEAR — none of the above.

        :raises: nothing; defensive against missing instances/defns.
        """
        hard_pads: list[Point] = []
        soft_pads: list[Point] = []
        remove_wires: set[uuid.UUID] = set()
        net_actions: list[object] = []
        descriptions: list[str] = []

        for cid in moved_comp_ids:
            pl = snapshot.placements.get(cid)
            inst = snapshot.instances.get(cid)
            if pl is None or inst is None:
                continue
            defn = snapshot.components.get(inst.definition_ref)
            if defn is None:
                continue

            # --- Body-on-body overlap (no strategy involvement) ---
            body_overlap_refdes = self._detect_body_overlap(
                snapshot=snapshot,
                cid=cid,
                pl=pl,
                defn=defn,
                inst=inst,
                moved_comp_ids=moved_comp_ids,
            )
            if body_overlap_refdes is not None:
                hard_pads.append(
                    pin_world_position(footprint_of(defn, inst), pl, 1)
                )
                descriptions.append(
                    f"{inst.refdes} body overlap with {body_overlap_refdes}"
                )

            # --- Pad-on-pad (delegated to strategy via pin gate) ---
            pad_conflicts = find_conflicts(
                incoming_placement=pl,
                incoming_defn=defn,
                incoming_instance_id=cid,
                layout_placements=snapshot.placements,
                instances=snapshot.instances,
                components=snapshot.components,
                schematic=snapshot.schematic,
                pad_index=snapshot.pad_index,
            )
            for c in pad_conflicts:
                if c.kind == ConflictKind.DIFFERENT_NETS:
                    hard_pads.append(c.position)
                    descriptions.append(
                        f"{inst.refdes} pin {c.incoming_pin}: "
                        f"different-net conflict"
                    )
                elif c.kind in (
                    ConflictKind.ONE_UNCONNECTED,
                    ConflictKind.BOTH_UNCONNECTED,
                ):
                    soft_pads.append(c.position)
                    descriptions.append(
                        f"{inst.refdes} pin {c.incoming_pin}: shared pad "
                        f"(will join net)"
                    )

            # --- Pin-on-wire (rule 6 strict via strategy) ---
            already_conflicted = {
                (_round(c.position.x), _round(c.position.y))
                for c in pad_conflicts
            }
            wire_conflicts = self._wire_conflicts_via_strategy(
                snapshot=snapshot,
                cid=cid,
                pl=pl,
                defn=defn,
                already_conflicted=already_conflicted,
            )
            for wc, reason in wire_conflicts:
                # Skip conflicts where the wire is one of the group's
                # own wires being translated together with the move.
                # By construction the wire moves *with* the pin (its
                # path was offset by the same delta), so the geometric
                # coincidence is a self-relation, not a conflict.
                # Without this filter, a group's own internal wire
                # ending on a group pin would be auto-deleted on every
                # commit — silently producing shorts when subsequent
                # auto-route reroutes through the cleared corridor.
                if wc.wire_id is not None and wc.wire_id in dragged_wire_ids:
                    continue
                soft_pads.append(wc.position)
                if wc.wire_id is not None:
                    remove_wires.add(wc.wire_id)
                descriptions.append(
                    f"{inst.refdes} pin {wc.incoming_pin}: {reason}"
                )

        # --- Wires crossing alien pads after the move ---
        crossing = find_wires_crossing_pads(
            wires=snapshot.wires,
            moved_comp_ids=moved_comp_ids,
            layout_placements=snapshot.placements,
            instances=snapshot.instances,
            components=snapshot.components,
            schematic=snapshot.schematic,
            moved_wire_ids=set(dragged_wire_ids),
            pad_index=snapshot.pad_index,
        )
        if crossing:
            remove_wires.update(crossing)
            descriptions.append(
                f"{len(crossing)} wire(s) cross alien pads (will remove)"
            )

        # --- Skewed wires ---
        skewed = find_skewed_wires(
            snapshot.wires, moved_wire_ids=set(dragged_wire_ids),
        )
        if skewed:
            remove_wires.update(skewed)
            descriptions.append(
                f"{len(skewed)} wire(s) became skewed (will remove)"
            )

        # --- Unified drag model classification (2026-06-02) ---
        # When the caller supplied drag_set_members, partition every wire
        # that would be removed into the three preference-driven buckets:
        # free / stationary-group / cross-group-disconnect.  Wires that
        # are part of the drag set's structural body
        # (internal / half_orphan_internal) must NEVER auto-remove —
        # they should travel with the drag delta in
        # ``MoveDragSetCommand``.
        wire_classification: DragWireClassification | None = None
        free_drop: set[uuid.UUID] = set()
        sgroup_drop: set[uuid.UUID] = set()
        cross_disc: set[uuid.UUID] = set()
        hard_locked_pads: list[Point] = []

        if snapshot.drag_set_members:
            wire_classification = classify_wires_for_drag(
                wires=snapshot.wires,
                drag_set_members=snapshot.drag_set_members,
                knots=snapshot.knots,
                wire_net_map=snapshot.wire_net_map,
                schematic=snapshot.schematic,
                member_groups=snapshot.member_groups,
            )
            # Wires structurally part of the drag set must not be
            # bucketed as removable — they travel with the delta.
            travels = wire_classification.travels_with_drag
            removable = remove_wires - travels

            # Wires owned by any stationary group (drag set excluded)
            # land in sgroup_drop; the rest are free.
            sgroup_wire_ids = self._stationary_group_owned_wires(
                wires=snapshot.wires,
                drag_set_members=snapshot.drag_set_members,
                stationary_groups=[
                    g for g in self._all_groups_for_snapshot(snapshot)
                    if g not in snapshot.member_groups
                ],
                knots=snapshot.knots,
                wire_net_map=snapshot.wire_net_map,
                schematic=snapshot.schematic,
            )

            for wid in removable:
                if wid in wire_classification.cross_group_disconnect:
                    cross_disc.add(wid)
                elif wid in sgroup_wire_ids:
                    sgroup_drop.add(wid)
                else:
                    free_drop.add(wid)
            # cross_group_disconnect wires that were NOT picked up as
            # remove_wires (no skewed/cross/pin-on-wire issue) still need
            # a user decision — the drag breaks the connection, and the
            # user must choose drop or reroute.
            for wid in wire_classification.cross_group_disconnect:
                if wid not in cross_disc:
                    cross_disc.add(wid)

            # Stationary groups with lock_internal_wires=True promote
            # soft pad conflicts (moved pin lands on owned wire) to HARD.
            locked_wires_to_pads = self._lock_internal_pad_positions(
                stationary_locked_groups=snapshot.stationary_locked_groups,
                wires=snapshot.wires,
                wire_net_map=snapshot.wire_net_map,
                knots=snapshot.knots,
                schematic=snapshot.schematic,
                conflict_wire_ids=sgroup_drop | cross_disc | free_drop,
            )
            hard_locked_pads.extend(locked_wires_to_pads)

        # Severity (worst wins).  Hard-locked group wires escalate to HARD.
        if hard_pads or hard_locked_pads:
            severity = DragSeverity.HARD
        elif soft_pads or remove_wires or cross_disc:
            severity = DragSeverity.SOFT
        else:
            severity = DragSeverity.CLEAR

        if _dbg_enabled():
            _log.debug(
                "drag_detector.classify",
                severity=severity.value,
                moved=[str(c)[:8] for c in moved_comp_ids],
                dragged_wires=[str(w)[:8] for w in dragged_wire_ids],
                auto_remove_wires=[str(w)[:8] for w in remove_wires],
                free_drop=[str(w)[:8] for w in free_drop],
                sgroup_drop=[str(w)[:8] for w in sgroup_drop],
                cross_disc=[str(w)[:8] for w in cross_disc],
                hard=[(p.x, p.y) for p in hard_pads],
                soft=[(p.x, p.y) for p in soft_pads],
            )

        # Combine HARD pads (geometric body/pin overlap + locked-wire
        # promotion) for the overlay.
        all_hard_pads: tuple[Point, ...] = tuple(hard_pads) + tuple(
            hard_locked_pads,
        )
        return DragCommitDecision(
            severity=severity,
            description="; ".join(descriptions),
            auto_remove_wire_ids=tuple(remove_wires),
            auto_net_actions=tuple(net_actions),
            blocking_pad_positions=all_hard_pads,
            overlay_pad_positions=tuple(soft_pads),
            wire_classification=wire_classification,
            free_drop_wire_ids=tuple(free_drop),
            stationary_group_drop_wire_ids=tuple(sgroup_drop),
            cross_group_disconnect_wire_ids=tuple(cross_disc),
            hard_locked_wire_pads=tuple(hard_locked_pads),
        )

    # ------------------------------------------------------------------
    # Unified drag model helpers (2026-06-02)
    # ------------------------------------------------------------------

    @staticmethod
    def _all_groups_for_snapshot(
        snapshot: "DragSnapshot",
    ) -> "list[ComponentGroup]":
        """Best-effort group list — for stationary-group classification
        we need every group, not just the member subset.  The snapshot
        carries member_groups + stationary_locked_groups; this helper
        returns their union.  Callers wanting full layout.groups must
        supply both sets at construction time.
        """
        return list(snapshot.member_groups) + list(
            snapshot.stationary_locked_groups,
        )

    @staticmethod
    def _stationary_group_owned_wires(
        wires: "list[Wire]",
        drag_set_members: frozenset[uuid.UUID],
        stationary_groups: "list[ComponentGroup]",
        knots: "dict[uuid.UUID, Knot]",
        wire_net_map: dict[uuid.UUID, uuid.UUID],
        schematic: "Schematic | None",
    ) -> set[uuid.UUID]:
        """Wires owned by any stationary group (member-disjoint).

        Used to bucket auto-remove candidates as "needs ask-remove"
        when they belong to a group the drag set is not part of.
        """
        from protoboard.domain.layout.group_ownership import OwnershipResolver
        # No placements/instances/definitions here → the resolver skips the
        # geometric sanity check, matching the original call's argument set.
        resolver = OwnershipResolver(
            knots=knots, wire_net_map=wire_net_map, schematic=schematic,
        )
        result: set[uuid.UUID] = set()
        for group in stationary_groups:
            if group.component_ids & drag_set_members:
                continue
            for wire in wires:
                if wire.id in result:
                    continue
                if resolver.is_owned(wire, group, group.component_ids):
                    result.add(wire.id)
        return result

    @staticmethod
    def _lock_internal_pad_positions(
        stationary_locked_groups: "list[ComponentGroup]",
        wires: "list[Wire]",
        wire_net_map: dict[uuid.UUID, uuid.UUID],
        knots: "dict[uuid.UUID, Knot]",
        schematic: "Schematic | None",
        conflict_wire_ids: set[uuid.UUID],
    ) -> list[Point]:
        """Pad positions where a moved pin landed on a wire owned by a
        stationary group with ``lock_internal_wires=True``.

        Each such pad is HARD — drag must be refused.  Returned as a
        flat list of pad positions (vertices of the offending wires)
        so the overlay can mark them red.
        """
        from protoboard.domain.layout.group_ownership import OwnershipResolver
        if not stationary_locked_groups or not conflict_wire_ids:
            return []
        resolver = OwnershipResolver(
            knots=knots, wire_net_map=wire_net_map, schematic=schematic,
        )
        pads: list[Point] = []
        wire_by_id = {w.id: w for w in wires}
        for group in stationary_locked_groups:
            for wid in conflict_wire_ids:
                wire = wire_by_id.get(wid)
                if wire is None:
                    continue
                if resolver.is_owned(wire, group, group.component_ids):
                    pads.extend(list(wire.path))
        return pads

    # ------------------------------------------------------------------
    # Internals — body overlap + strategy-driven wire check
    # ------------------------------------------------------------------

    def _detect_body_overlap(
        self,
        *,
        snapshot: DragSnapshot,
        cid: uuid.UUID,
        pl: "ComponentPlacement",
        defn: "ComponentDefinition",
        inst: "ComponentInstance",
        moved_comp_ids: set[uuid.UUID],
    ) -> str | None:
        """Return the refdes of the first non-moved component whose bbox
        overlaps *pl*'s without any pin coincidence, else None.
        """
        incoming_fp = footprint_of(defn, inst)
        incoming_bbox = component_bbox(
            incoming_fp, pl, defn.component_class,
        )
        _eps = 1e-4
        for other_id, other_pl in snapshot.placements.items():
            if other_id == cid or other_id in moved_comp_ids:
                continue
            other_inst = snapshot.instances.get(other_id)
            if other_inst is None:
                continue
            other_defn = snapshot.components.get(other_inst.definition_ref)
            if other_defn is None:
                continue
            other_fp = footprint_of(other_defn, other_inst)
            other_bbox = component_bbox(
                other_fp, other_pl, other_defn.component_class,
            )
            if not incoming_bbox.intersects(other_bbox):
                continue
            has_pin_overlap = False
            for in_pin in range(1, defn.footprint.pin_count() + 1):
                in_pos = pin_world_position(incoming_fp, pl, in_pin)
                for ot_pin in range(1, other_defn.footprint.pin_count() + 1):
                    ot_pos = pin_world_position(
                        other_fp, other_pl, ot_pin,
                    )
                    if (
                        abs(in_pos.x - ot_pos.x) < _eps
                        and abs(in_pos.y - ot_pos.y) < _eps
                    ):
                        has_pin_overlap = True
                        break
                if has_pin_overlap:
                    break
            if not has_pin_overlap:
                return other_inst.refdes
        return None

    def _wire_conflicts_via_strategy(
        self,
        *,
        snapshot: DragSnapshot,
        cid: uuid.UUID,
        pl: "ComponentPlacement",
        defn: "ComponentDefinition",
        already_conflicted: set[tuple[float, float]],
    ) -> list[tuple[PadConflict, str]]:
        """Detect pin-on-wire conflicts through the strategy.

        For every pin landing on a wire-bearing pad, ask
        ``strategy.can_place_pin(pad, our_net)``. Under rule 6 strict
        the strategy rejects every such case (any wire on pin pad).
        The wire identity, position, and net are carried back through
        the legacy :class:`PadConflict` shape so commit-time pipelines
        can build remove commands without re-deriving anything.

        Returns ``(PadConflict, reason)`` pairs; reason is the
        strategy's verdict text when populated, otherwise a legacy
        fallback phrasing.
        """
        # Pin → net for this component (so we can pass our_net to the
        # strategy and produce coherent SAME_NET vs DIFFERENT_NET
        # descriptions).
        pin_net: dict[int, tuple[uuid.UUID, str]] = {}
        for net in snapshot.schematic.nets.values():
            for ref in net.pins:
                if ref.component_id == cid:
                    pin_net[ref.pin_number] = (net.id, net.name)

        # Wire-net lookup for description-only use; the strategy
        # doesn't need it.
        net_name_by_id = {
            net.id: net.name for net in snapshot.schematic.nets.values()
        }

        # Build a legacy point_to_net so the fallback path inside
        # find_wire_conflicts has a chance to populate wire_net_info
        # when pad_index doesn't carry it.
        point_to_net: dict[tuple[float, float], tuple[uuid.UUID, str]] = {}
        for net in snapshot.schematic.nets.values():
            for ref in net.pins:
                pos = _get_pin_position(
                    snapshot, ref.component_id, ref.pin_number,
                )
                if pos is None:
                    continue
                point_to_net[(_round(pos.x), _round(pos.y))] = (
                    net.id, net.name,
                )

        # First produce the legacy find_wire_conflicts output. We then
        # filter / annotate it through the strategy: every conflict
        # find_wire_conflicts produced is, by definition, a pin landing
        # on a wire — strategy.can_place_pin(pad) will reject it
        # (under rule 6 strict), so we accept all of them but rephrase
        # the description from the strategy's reason.
        wire_conflicts = find_wire_conflicts(
            incoming_placement=pl,
            incoming_defn=defn,
            incoming_instance_id=cid,
            wires=snapshot.wires,
            schematic=snapshot.schematic,
            point_to_net=point_to_net,
            already_conflicted=already_conflicted,
            pad_index=snapshot.pad_index,
            # The incoming geometry must come from the bound instance — without
            # it find_wire_conflicts has no instances dict to resolve from and
            # raises UnboundInstanceError (previously masked by the silent
            # defn.footprint fallback, which also meant drag used the wrong
            # variant pitch).
            incoming_instance=snapshot.instances.get(cid),
        )

        annotated: list[tuple[PadConflict, str]] = []
        for wc in wire_conflicts:
            our_net = (
                pin_net[wc.incoming_pin][0]
                if wc.incoming_pin in pin_net
                else None
            )
            pad = (
                snapshot.pad_index.pad_at(wc.position)
                if snapshot.pad_index is not None
                else None
            )
            if pad is None:
                annotated.append((wc, _legacy_wire_reason(wc)))
                continue
            decision = self._strategy.can_place_pin(pad, our_net=our_net)
            reason = (
                decision.reason
                if decision.verdict is Verdict.REJECT and decision.reason
                else _legacy_wire_reason(wc)
            )
            annotated.append((wc, reason))

        _ = net_name_by_id  # populated for future reason-string upgrades
        return annotated


def _legacy_wire_reason(wc: PadConflict) -> str:
    """Mirror the pre-refactor phrasing in placement_conflict.py so
    existing description-based assertions keep working."""
    if wc.kind == ConflictKind.PIN_ON_WIRE_SAME_NET:
        return (
            "on same-net wire (will remove — re-route through a "
            "separate hub)"
        )
    # PIN_ON_WIRE_DIFFERENT_NET / PIN_ON_WIRE_WITH_NET /
    # PIN_ON_WIRE_UNCONNECTED all collapsed to "foreign wire" in the
    # legacy code path. Keep that phrasing for back-compat with the
    # corner-text overlay and existing tests.
    return "on foreign wire (will remove)"


def _get_pin_position(
    snapshot: DragSnapshot,
    comp_id: uuid.UUID,
    pin_number: int,
) -> Point | None:
    """Thin wrapper over :func:`resolve_pin_world_position` for a snapshot.

    Kept as a module-level alias for callsite convenience; the actual
    safety chain lives in domain.layout.placement.
    """
    from protoboard.domain.layout.placement import resolve_pin_world_position
    return resolve_pin_world_position(
        comp_id, pin_number,
        placements=snapshot.placements,
        instances=snapshot.instances,
        components=snapshot.components,
    )


__all__ = ["DragDetector", "DragSnapshot"]
