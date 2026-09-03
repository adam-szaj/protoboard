# SPDX-License-Identifier: MIT
"""Pure drag-conflict classification over a proposed layout snapshot."""

from __future__ import annotations

import uuid
from dataclasses import dataclass, field
from typing import TYPE_CHECKING

import structlog

from protoboard.domain.components.resolution import (
    iter_placed_components,
    resolve_placed_component,
)
from protoboard.domain.layout.drag_analysis import (
    DragAnalysis,
    DragReason,
    DragReasonCode,
    DragSeverity,
)
from protoboard.domain.layout.drag_classification import (
    DragWireClassification,
    classify_wires_for_drag,
)
from protoboard.domain.layout.group_ownership import OwnershipResolver
from protoboard.domain.layout.pad_conflict import (
    ConflictKind,
    PadConflict,
    find_conflicts,
    find_wire_conflicts,
    find_wires_crossing_pads,
    footprint_of,
)
from protoboard.domain.layout.pad_strategy import PadStrategy
from protoboard.domain.layout.placement import (
    component_bbox,
    iter_footprint_pin_positions,
    pin_world_position,
    resolve_pin_world_position,
)
from protoboard.domain.layout.wire_utils import find_skewed_wires
from protoboard.geometry import Point
from protoboard.geometry import round_coord as _round
from protoboard.instrumentation.logging import is_debug_enabled

if TYPE_CHECKING:
    from protoboard.domain.board.pad_state import PadIndex
    from protoboard.domain.components.definition import ComponentDefinition
    from protoboard.domain.components.instance import ComponentInstance
    from protoboard.domain.layout.group import ComponentGroup
    from protoboard.domain.layout.knot import Knot
    from protoboard.domain.layout.placement import (
        ComponentPlacement,
        PlacementMap,
    )
    from protoboard.domain.layout.wire import Wire
    from protoboard.domain.schematic.schematic import Schematic

_log = structlog.get_logger(__name__)


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

    pad_index: PadIndex | None
    placements: PlacementMap
    instances: dict[uuid.UUID, ComponentInstance]
    components: dict[str, ComponentDefinition]
    schematic: Schematic
    wires: list[Wire] = field(default_factory=list)
    drag_set_members: frozenset[uuid.UUID] = field(default_factory=frozenset)
    """Component IDs the drag moves atomically.  Empty disables the
    new classification path; ``classify`` falls back to the legacy
    auto-remove-everything behaviour."""

    wire_net_map: dict[uuid.UUID, uuid.UUID] = field(default_factory=dict)
    """``wire.id → net.id`` map (typically
    ``EditorSession.cached_wire_net_map()``).  Required by half-orphan
    classification — without it, all half-orphans land in the
    ``half_orphan_external`` bucket (drop on commit)."""

    knots: dict[uuid.UUID, Knot] = field(default_factory=dict)
    """``layout.knots`` — required for endpoint flavour resolution
    in the wire classifier."""

    member_groups: list[ComponentGroup] = field(default_factory=list)
    """Groups whose ``component_ids`` overlap ``drag_set_members``.
    Empty when dragging a free component or a pure-free multi-selection;
    used only for half-orphan internal-net resolution."""

    stationary_locked_groups: list[ComponentGroup] = field(
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
    ) -> DragAnalysis:
        """Return structured conflicts and commit actions for this pose.

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
        reasons: list[DragReason] = []

        for cid in moved_comp_ids:
            component = resolve_placed_component(
                cid,
                snapshot.placements,
                snapshot.instances,
                snapshot.components,
            )
            if component is None:
                continue
            pl = component.placement
            inst = component.instance
            defn = component.definition

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
                hard_pads.append(pin_world_position(footprint_of(defn, inst), pl, 1))
                reasons.append(
                    DragReason(
                        DragReasonCode.BODY_OVERLAP,
                        refdes=inst.refdes,
                        related_refdes=body_overlap_refdes,
                    )
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
                    reasons.append(
                        DragReason(
                            DragReasonCode.DIFFERENT_NETS,
                            refdes=inst.refdes,
                            pin_number=c.incoming_pin,
                        )
                    )
                elif c.kind in (
                    ConflictKind.ONE_UNCONNECTED,
                    ConflictKind.BOTH_UNCONNECTED,
                ):
                    soft_pads.append(c.position)
                    reasons.append(
                        DragReason(
                            DragReasonCode.SHARED_PAD,
                            refdes=inst.refdes,
                            pin_number=c.incoming_pin,
                        )
                    )

            # --- Pin-on-wire (rule 6 strict via strategy) ---
            already_conflicted = {
                (_round(c.position.x), _round(c.position.y)) for c in pad_conflicts
            }
            wire_conflicts = self._wire_conflicts_via_strategy(
                snapshot=snapshot,
                cid=cid,
                pl=pl,
                defn=defn,
                already_conflicted=already_conflicted,
            )
            for wc in wire_conflicts:
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
                code = (
                    DragReasonCode.SAME_NET_WIRE
                    if wc.kind == ConflictKind.PIN_ON_WIRE_SAME_NET
                    else DragReasonCode.FOREIGN_WIRE
                )
                reasons.append(
                    DragReason(code, refdes=inst.refdes, pin_number=wc.incoming_pin)
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
            reasons.append(
                DragReason(DragReasonCode.ALIEN_PAD_CROSSING, count=len(crossing))
            )

        # --- Skewed wires ---
        skewed = find_skewed_wires(
            snapshot.wires,
            moved_wire_ids=set(dragged_wire_ids),
        )
        if skewed:
            remove_wires.update(skewed)
            reasons.append(DragReason(DragReasonCode.SKEWED_WIRE, count=len(skewed)))

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
                    g
                    for g in self._all_groups_for_snapshot(snapshot)
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
            if locked_wires_to_pads:
                reasons.append(
                    DragReason(
                        DragReasonCode.LOCKED_GROUP_WIRE,
                        count=len(locked_wires_to_pads),
                    )
                )

        # Severity (worst wins).  Hard-locked group wires escalate to HARD.
        if hard_pads or hard_locked_pads:
            severity = DragSeverity.HARD
        elif soft_pads or remove_wires or cross_disc:
            severity = DragSeverity.SOFT
        else:
            severity = DragSeverity.CLEAR

        if is_debug_enabled(__name__):
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
        return DragAnalysis(
            severity=severity,
            reasons=tuple(reasons),
            auto_remove_wire_ids=tuple(remove_wires),
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
        snapshot: DragSnapshot,
    ) -> list[ComponentGroup]:
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
        wires: list[Wire],
        drag_set_members: frozenset[uuid.UUID],
        stationary_groups: list[ComponentGroup],
        knots: dict[uuid.UUID, Knot],
        wire_net_map: dict[uuid.UUID, uuid.UUID],
        schematic: Schematic | None,
    ) -> set[uuid.UUID]:
        """Wires owned by any stationary group (member-disjoint).

        Used to bucket auto-remove candidates as "needs ask-remove"
        when they belong to a group the drag set is not part of.
        """
        # No placements/instances/definitions here → the resolver skips the
        # geometric sanity check, matching the original call's argument set.
        resolver = OwnershipResolver(
            knots=knots,
            wire_net_map=wire_net_map,
            schematic=schematic,
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
        stationary_locked_groups: list[ComponentGroup],
        wires: list[Wire],
        wire_net_map: dict[uuid.UUID, uuid.UUID],
        knots: dict[uuid.UUID, Knot],
        schematic: Schematic | None,
        conflict_wire_ids: set[uuid.UUID],
    ) -> list[Point]:
        """Pad positions where a moved pin landed on a wire owned by a
        stationary group with ``lock_internal_wires=True``.

        Each such pad is HARD — drag must be refused.  Returned as a
        flat list of pad positions (vertices of the offending wires)
        so the overlay can mark them red.
        """
        if not stationary_locked_groups or not conflict_wire_ids:
            return []
        resolver = OwnershipResolver(
            knots=knots,
            wire_net_map=wire_net_map,
            schematic=schematic,
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
        pl: ComponentPlacement,
        defn: ComponentDefinition,
        inst: ComponentInstance,
        moved_comp_ids: set[uuid.UUID],
    ) -> str | None:
        """Return the refdes of the first non-moved component whose bbox
        overlaps *pl*'s without any pin coincidence, else None.
        """
        incoming_fp = footprint_of(defn, inst)
        incoming_bbox = component_bbox(
            incoming_fp,
            pl,
            defn.component_class,
        )
        _eps = 1e-4
        for other in iter_placed_components(
            snapshot.placements,
            snapshot.instances,
            snapshot.components,
        ):
            other_id = other.placement.component_id
            if other_id == cid or other_id in moved_comp_ids:
                continue
            other_fp = footprint_of(other.definition, other.instance)
            other_bbox = component_bbox(
                other_fp,
                other.placement,
                other.definition.component_class,
            )
            if not incoming_bbox.intersects(other_bbox):
                continue
            has_pin_overlap = False
            for _in_pin, in_pos in iter_footprint_pin_positions(incoming_fp, pl):
                for _ot_pin, ot_pos in iter_footprint_pin_positions(
                    other_fp, other.placement
                ):
                    if (
                        abs(in_pos.x - ot_pos.x) < _eps
                        and abs(in_pos.y - ot_pos.y) < _eps
                    ):
                        has_pin_overlap = True
                        break
                if has_pin_overlap:
                    break
            if not has_pin_overlap:
                return other.instance.refdes
        return None

    def _wire_conflicts_via_strategy(
        self,
        *,
        snapshot: DragSnapshot,
        cid: uuid.UUID,
        pl: ComponentPlacement,
        defn: ComponentDefinition,
        already_conflicted: set[tuple[float, float]],
    ) -> list[PadConflict]:
        """Detect pin-on-wire conflicts through the strategy.

        For every pin landing on a wire-bearing pad, ask
        ``strategy.can_place_pin(pad, our_net)``. Under rule 6 strict
        the strategy rejects every such case (any wire on pin pad).
        Wire identity, position, and net remain in :class:`PadConflict` so
        commit-time pipelines can build remove commands without re-deriving
        them.

        The strategy gate remains the authority for whether the pad accepts a
        pin; presentation text is derived later from the structured conflict.
        """
        pin_net_map = snapshot.schematic.pin_net_map()

        # Pin → net for this component, passed to the placement strategy.
        pin_net: dict[int, tuple[uuid.UUID, str]] = {}
        for ref, net in pin_net_map.items():
            if ref.component_id == cid:
                pin_net[ref.pin_number] = (net.id, net.name)

        # Supply endpoint net identities when the pad index does not carry
        # them; find_wire_conflicts uses this to classify the conflict kind.
        point_to_net: dict[tuple[float, float], tuple[uuid.UUID, str]] = {}
        for ref, net in pin_net_map.items():
            pos = _get_pin_position(
                snapshot,
                ref.component_id,
                ref.pin_number,
            )
            if pos is None:
                continue
            point_to_net[(_round(pos.x), _round(pos.y))] = (net.id, net.name)

        # Every returned conflict is a pin landing on a wire. Under rule 6,
        # the strategy rejects that pad; the conflict kind carries the fact
        # used later for the structured reason.
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

        classified: list[PadConflict] = []
        for wc in wire_conflicts:
            our_net = (
                pin_net[wc.incoming_pin][0] if wc.incoming_pin in pin_net else None
            )
            pad = (
                snapshot.pad_index.pad_at(wc.position)
                if snapshot.pad_index is not None
                else None
            )
            if pad is None:
                classified.append(wc)
                continue
            self._strategy.can_place_pin(pad, our_net=our_net)
            classified.append(wc)

        return classified


def _get_pin_position(
    snapshot: DragSnapshot,
    comp_id: uuid.UUID,
    pin_number: int,
) -> Point | None:
    """Thin wrapper over :func:`resolve_pin_world_position` for a snapshot.

    Kept as a module-level alias for callsite convenience; the actual
    safety chain lives in domain.layout.placement.
    """
    return resolve_pin_world_position(
        comp_id,
        pin_number,
        placements=snapshot.placements,
        instances=snapshot.instances,
        components=snapshot.components,
    )


__all__ = ["DragDetector", "DragSnapshot"]
