# SPDX-License-Identifier: MIT
"""Commands for placing, moving and removing components.

All commands operate on a ComponentState protocol so that both
Layout.placements and session.instances stay in sync through undo/redo,
without creating a hard dependency on the GUI layer.
"""

from __future__ import annotations

import uuid
from dataclasses import dataclass, field
from typing import TYPE_CHECKING, Protocol

import structlog

from protoboard.domain.board.board import Board
from protoboard.domain.commands.base import Command
from protoboard.domain.components.definition import ComponentDefinition
from protoboard.domain.components.instance import ComponentInstance
from protoboard.domain.layout.layout import Layout
from protoboard.domain.layout.placement import (
    ComponentPlacement,
    component_bbox,
    pin_world_position,
    validate_pins_on_pads,
)
from protoboard.domain.layout.knot import JointKnot, PinKnot
from protoboard.domain.layout.wire import KnotRef, Wire
from protoboard.domain.schematic.net import PinRef
from protoboard.geometry import Point

_log = structlog.get_logger(__name__)

if TYPE_CHECKING:
    from protoboard.domain.board.pad_state import PadIndex
    from protoboard.domain.schematic.net import Net
    from protoboard.domain.schematic.schematic import Schematic
    from protoboard.services.pad_strategy import PadStrategy


def _strategy_from_state(state: "ComponentState") -> "PadStrategy | None":
    """Pull the DRC strategy off the host session, if any.

    ``ComponentState`` is a Protocol — the runtime object is usually a
    full ``EditorSession`` but unit tests can pass a minimal stand-in
    that has none of the session machinery.  Returns ``None`` when no
    strategy is reachable; callers must treat that as "skip the gate"
    so legacy tests keep working.
    """
    fn = getattr(state, "strategy_for", None)
    if fn is None:
        return None
    try:
        return fn("drc")  # type: ignore[no-any-return]
    except Exception:
        return None


def _pad_index_from_state(state: "ComponentState") -> "PadIndex | None":
    """Pull the cached :class:`PadIndex` off the host session, if any.

    Same defensiveness as :func:`_strategy_from_state` — when the
    state has no ``pad_index`` method (legacy unit tests, command
    replay), the strategy gates degrade to the historic behaviour
    encoded inline in :func:`_find_joint_at`.
    """
    fn = getattr(state, "pad_index", None)
    if fn is None:
        return None
    try:
        return fn()  # type: ignore[no-any-return]
    except Exception:
        return None


class ComponentState(Protocol):
    """Minimal state needed by all board/component commands."""

    board: Board
    layout: Layout
    instances: dict[uuid.UUID, ComponentInstance]
    components: dict[str, ComponentDefinition]


class SchematicState(ComponentState, Protocol):
    """Extended state that also exposes the schematic netlist."""

    schematic: Schematic


@dataclass
class PlaceComponentCommand(Command[ComponentState]):
    component_id: uuid.UUID
    placement: ComponentPlacement
    instance: ComponentInstance

    @property
    def description(self) -> str:  # type: ignore[override]
        return f"Place {self.instance.refdes}"

    def apply(self, state: ComponentState) -> None:
        state.layout.placements[self.component_id] = self.placement
        state.instances[self.component_id] = self.instance
        _warn_pins_off_pads(state, self.component_id, self.placement)

    def revert(self, state: ComponentState) -> None:
        del state.layout.placements[self.component_id]
        state.instances.pop(self.component_id, None)


@dataclass
class MoveComponentCommand(Command[ComponentState]):
    component_id: uuid.UUID
    new_placement: ComponentPlacement
    old_placement: ComponentPlacement
    # When True, skip the _move_blocked() check (caller already verified).
    skip_conflict_check: bool = field(default=False)
    # Wire IDs already translated by live drag — skip in _update_wires_for_move
    # to prevent double-translation when old_pin_pos == new_pin_pos of another component.
    exclude_wire_ids: frozenset[uuid.UUID] = field(default_factory=frozenset)
    # Wires before the move — saved for undo.
    _saved_wires: list[Wire] = field(
        default_factory=list, init=False, repr=False
    )
    _applied: bool = field(default=False, init=False, repr=False)

    @property
    def description(self) -> str:  # type: ignore[override]
        return f"Move component {self.component_id}"

    def apply(self, state: ComponentState) -> None:
        # Locked components never move — silently treat as no-op.
        if state.layout.placements.get(self.component_id, self.old_placement).locked:
            object.__setattr__(self, "_applied", False)
            return
        if not self.skip_conflict_check:
            blocked = _move_blocked(
                state, self.component_id, self.old_placement, self.new_placement
            )
            if blocked:
                # Conflict — silently keep old position; mark as no-op for undo.
                object.__setattr__(self, "_applied", False)
                state.layout.placements[self.component_id] = self.old_placement
                return
        object.__setattr__(self, "_applied", True)
        object.__setattr__(self, "_saved_wires", list(state.layout.wires))
        state.layout.placements[self.component_id] = self.new_placement
        _update_wires_for_move(
            state, self.component_id, self.old_placement, self.new_placement,
            self.exclude_wire_ids,
        )
        _warn_pins_off_pads(state, self.component_id, self.new_placement)

    def revert(self, state: ComponentState) -> None:
        if not self._applied:
            return
        state.layout.placements[self.component_id] = self.old_placement
        state.layout.wires[:] = self._saved_wires


@dataclass
class RemoveComponentCommand(Command[SchematicState]):
    """Remove a component from the layout and clean up its pins from all nets.

    Any net that becomes empty after pin removal is also deleted.
    All mutations are recorded for full undo/redo.
    """

    component_id: uuid.UUID
    _saved_placement: ComponentPlacement | None = field(
        default=None, init=False, repr=False
    )
    _saved_instance: ComponentInstance | None = field(
        default=None, init=False, repr=False
    )
    # net_id → (net_name, removed_pins) for every net touched by this remove
    _nets_before: dict[uuid.UUID, "tuple[str, set[object]]"] = field(
        default_factory=dict, init=False, repr=False
    )

    @property
    def description(self) -> str:  # type: ignore[override]
        return f"Remove component {self.component_id}"

    def apply(self, state: SchematicState) -> None:
        self._saved_placement = state.layout.placements.pop(self.component_id)
        self._saved_instance = state.instances.pop(self.component_id, None)

        nets_before: dict[uuid.UUID, tuple[str, set[object]]] = {}
        for net_id, net in list(state.schematic.nets.items()):
            removed = {p for p in net.pins if p.component_id == self.component_id}
            if not removed:
                continue
            nets_before[net_id] = (net.name, removed)
            net.pins -= removed
            if not net.pins:
                del state.schematic.nets[net_id]
        self._nets_before = nets_before

    def revert(self, state: SchematicState) -> None:
        from protoboard.domain.schematic.net import Net

        if self._saved_placement is not None:
            state.layout.placements[self.component_id] = self._saved_placement
        if self._saved_instance is not None:
            state.instances[self.component_id] = self._saved_instance
        for net_id, (net_name, removed_pins) in self._nets_before.items():
            net = state.schematic.nets.get(net_id)
            if net is not None:
                net.pins |= removed_pins  # type: ignore[arg-type]
            else:
                state.schematic.nets[net_id] = Net(
                    id=net_id, name=net_name, pins=set(removed_pins)  # type: ignore[arg-type]
                )


def _enrich_insulated_with_pad_nodes(
    wire: Wire, board: Board,
) -> Wire:
    """Insert board-pad positions into the path of an *insulated* wire.

    An insulated wire that crosses a pad geometrically between two of its
    recorded vertices has no "handle" at that pad — simplify, unweave and
    interactive edits cannot manipulate the path there.  Inserting an
    explicit pad-vertex at each crossed pad gives downstream tools the
    nodes they need.  Idempotent — pads already on the path are not
    duplicated.

    Bare wires are returned unchanged — they're produced with pad-vertices
    by their callers (RouteTool's ``insert_pad_nodes`` enrichment, A*
    step-by-step paths) and the bare-diagonal parity model relies on the
    caller-controlled vertex set.

    A new ``Wire`` is returned only when the path actually grows;
    otherwise the input is returned as-is.  The wire's id is preserved
    so callers comparing by identity stay consistent.
    """
    if not wire.insulated:
        return wire
    from protoboard.domain.layout.wire_utils import (
        board_pads_for_side,
        insert_pad_nodes,
    )
    # Insulated wires sit on one side; their endpoints solder to that side's
    # pads.  Pads from BOTH sides are relevant for vertex insertion because
    # the wire's geometric path may pass over a pad that physically exists
    # on either side (plated-through pads in particular).  Take the union.
    pads = board_pads_for_side(board, "top") | board_pads_for_side(board, "bottom")
    new_path = insert_pad_nodes(list(wire.path), pads)
    if len(new_path) == len(wire.path):
        return wire
    return Wire(
        id=wire.id,
        path=new_path,
        insulated=wire.insulated,
        side=wire.side,
        color=wire.color,
        a=wire.a,
        b=wire.b,
    )


_EPS_KNOT = 1e-6


def _find_pin_at(
    state: ComponentState,
    point: Point,
    *,
    our_net: uuid.UUID | None = None,
    strategy: "PadStrategy | None" = None,
) -> PinRef | None:
    """Return the PinRef of the pin sitting at ``point``, or None.

    Scans current placements.  Exact match first, then a tolerant
    fallback (``1e-6``) to absorb the rounding present in legacy paths.

    When ``strategy`` and a pad index are available, the returned pin
    is gated by :meth:`PadStrategy.can_place_pin` (rule 8 — a pin
    cannot accept an incoming wire of a foreign net even though the
    geometry matches). Without a strategy or pad index the gate is
    skipped and the historical behaviour applies — important for the
    many in-memory tests that drive commands without a full session.
    """
    pad_idx = _pad_index_from_state(state) if strategy is not None else None
    for comp_id, placement in state.layout.placements.items():
        inst = state.instances.get(comp_id)
        if inst is None:
            continue
        defn = state.components.get(inst.definition_ref)
        if defn is None:
            continue
        n_pins = defn.footprint.pin_count()
        for pin_num in range(1, n_pins + 1):
            try:
                pos = pin_world_position(defn.footprint, placement, pin_num)
            except (IndexError, ValueError):
                continue
            if not (
                abs(pos.x - point.x) < _EPS_KNOT
                and abs(pos.y - point.y) < _EPS_KNOT
            ):
                continue
            if strategy is not None and pad_idx is not None:
                pad = pad_idx.pad_at(pos)
                if pad is not None:
                    d = strategy.can_place_pin(pad, our_net)
                    from protoboard.services.pad_strategy import Verdict
                    if d.verdict is Verdict.REJECT:
                        # Rule 8: foreign-net wire cannot solder to
                        # this pin. Skip — caller treats it as "no pin
                        # here" and falls through to JointKnot creation
                        # (which will run its own net gate via
                        # _find_joint_at).
                        continue
            return PinRef(component_id=comp_id, pin_number=pin_num)
    return None


def _find_joint_at(
    state: ComponentState,
    point: Point,
    *,
    side: str | None = None,
    net_id: uuid.UUID | None = None,
    strategy: "PadStrategy | None" = None,
) -> JointKnot | None:
    """Return an existing JointKnot at ``point`` that is *compatible* with
    the incoming wire's ``side`` and ``net_id``.

    Pre-fix this returned the first JointKnot at the position regardless
    of who else was hanging off it.  That let an autoroute pass build a
    single degree-N JointKnot where wires of different schematic nets
    converged on the same off-pad point — the *reference_big_project*
    bug.

    Compatibility (delegated to :class:`PadStrategy` when available,
    otherwise enforced inline for legacy callers):
      * If the joint already carries any wire on a different ``side``
        **and the position is not a plated-through pad** → skip; top
        and bottom can only share one solder joint via a via.
      * If any incident wire resolves to a net different from ``net_id``
        (both sides explicit, not None) → skip; merging would short two
        nets.  Net-less wires already on the joint are tolerated only
        when the incoming wire is also net-less.

    ``side`` / ``net_id`` of ``None`` mean "caller does not know" (legacy
    call sites pre-fix) — behave like before and return the first hit.
    """
    schematic = getattr(state, "schematic", None)
    pad_idx = _pad_index_from_state(state) if strategy is not None else None
    plated_pads: frozenset[Point] | None = None
    for knot in state.layout.knots.values():
        if not isinstance(knot, JointKnot):
            continue
        if not (
            abs(knot.position.x - point.x) < _EPS_KNOT
            and abs(knot.position.y - point.y) < _EPS_KNOT
        ):
            continue
        if side is None and net_id is None:
            return knot
        if not _joint_accepts_wire(
            state, knot, side=side, net_id=net_id,
            schematic=schematic,
            strategy=strategy, pad_idx=pad_idx, plated_pads_cache=plated_pads,
        ):
            continue
        return knot
    return None


def _joint_accepts_wire(
    state: ComponentState,
    knot: JointKnot,
    *,
    side: str | None,
    net_id: uuid.UUID | None,
    schematic: "Schematic | None",
    strategy: "PadStrategy | None",
    pad_idx: "PadIndex | None",
    plated_pads_cache: frozenset[Point] | None,
) -> bool:
    """Return True if *knot* is compatible with the incoming wire.

    Pulled out of :func:`_find_joint_at` so the strategy-driven and
    legacy-inline branches share the per-incident-wire loop.
    """
    from protoboard.services.pad_strategy import Verdict

    pad = pad_idx.pad_at(knot.position) if pad_idx is not None else None
    for wid in knot.wire_ids:
        w = state.layout.wires.get(wid)
        if w is None:
            continue
        cross_layer = side is not None and w.side != side
        # Strategy path — single source of truth when available.
        if strategy is not None and pad is not None:
            w_net = _wire_net_for_joint(state, w, schematic)
            d = strategy.can_form_t_junction(
                pad,
                w.side,
                w_net,
                net_id,
                cross_layer,
            )
            if d.verdict is Verdict.REJECT:
                return False
            # ALLOW / ASSIGN_NET both keep the joint as a candidate;
            # ASSIGN_NET's actual net mutation is handled later by
            # `_split_existing_wires_at_endpoints` when the wire is
            # spliced into the existing one.
            continue
        # Legacy inline path — preserved for tests that drive commands
        # without a session-backed strategy.
        if side is not None and w.side != side:
            if plated_pads_cache is None:
                from protoboard.domain.layout.wire_utils import (
                    plated_through_pads,
                )
                plated_pads_cache = frozenset(plated_through_pads(state.board))
            if knot.position not in plated_pads_cache:
                return False
        if schematic is not None:
            w_net = _wire_net_for_joint(state, w, schematic)
            if (
                (w_net is not None and net_id is not None and w_net != net_id)
                or (w_net is not None and net_id is None)
            ):
                return False
    return True


def _wire_net_for_joint(
    state: ComponentState,
    wire: Wire,
    schematic: "Schematic | None",
) -> uuid.UUID | None:
    """Resolve *wire*'s schematic net id for joint-compatibility checks."""
    if schematic is None:
        return None
    from protoboard.domain.layout.wire_utils import _wire_net_id
    return _wire_net_id(
        wire, schematic, state.layout, state.instances, state.components,
        board=state.board,
    )


def _wire_intended_net(
    state: ComponentState, wire: Wire,
) -> uuid.UUID | None:
    """Resolve *wire* to a schematic net id via its endpoint pins.

    Returns None when the wire touches no pin (orphan / pure
    knot-to-knot junction) or when the state has no schematic.  Used by
    AddWireCommand to gate joint reuse so an autoroute pass cannot
    merge wires of different nets into a phantom shared hub.

    Passes ``state.board`` so cross-side pin resolution through
    plated-through pads works — the autoroute common case where a wire
    is on the bottom layer but the component pin lives on top.
    """
    schematic = getattr(state, "schematic", None)
    if schematic is None:
        return None
    from protoboard.domain.layout.wire_utils import _wire_net_id
    return _wire_net_id(
        wire, schematic, state.layout, state.instances, state.components,
        board=state.board,
    )


def _resolve_endpoint_knot(
    state: ComponentState,
    point: Point,
    *,
    side: str | None = None,
    net_id: uuid.UUID | None = None,
    strategy: "PadStrategy | None" = None,
) -> tuple[KnotRef, JointKnot | None]:
    """Resolve an endpoint position to (KnotRef, optional created JointKnot).

    Pin hit → reuse/create the singleton PinKnot for that PinRef.
    Existing JointKnot at this position **and compatible with the
    incoming wire's ``side`` / ``net_id``** → reuse it (so two wires of
    the same net meeting at the same off-pin pad share one hub).
    Otherwise → create a fresh JointKnot at ``point``.  The created knot
    (if any) is returned so AddWireCommand.revert can drop it.

    ``strategy`` (and the session's pad index reachable through
    ``state``) gates pin and joint reuse via the strategy contract —
    rules 7 + 8 of pad/pin/wire/junction semantics.  Defaults to the
    DRC-enforcing strategy when ``state`` is session-backed; legacy
    callers that pass a bare ``ComponentState`` get the same inline
    behaviour as before this refactor.
    """
    if strategy is None:
        strategy = _strategy_from_state(state)
    pin = _find_pin_at(state, point, our_net=net_id, strategy=strategy)
    if pin is not None:
        pk = state.layout.get_or_create_pin_knot(pin)
        return KnotRef(id=pk.id), None
    existing = _find_joint_at(
        state, point, side=side, net_id=net_id, strategy=strategy,
    )
    if existing is not None:
        return KnotRef(id=existing.id), None
    # Joint at this position (off-pad joints are flagged by DRC, not here).
    jk = state.layout.create_joint_knot(point)
    return KnotRef(id=jk.id), jk


def _split_existing_wires_at_endpoints(
    state: ComponentState, wire: Wire,
) -> "list[_SplitRecord]":
    """For each endpoint of ``wire`` that lands on the interior vertex
    of an existing wire, atomically build a T-junction (split that
    wire and re-anchor ``wire``'s endpoint to the shared hub).

    Returns one ``_SplitRecord`` per split so revert can stitch the
    split halves back into a single wire.  Pin endpoints of ``wire``
    still take precedence over interior hits — pins are physical hubs
    and never demote an existing wire to T-junction status.
    """
    from protoboard.domain.layout.wire_utils import (
        TJunctionVerdict,
        can_form_t_junction,
        find_wire_with_interior_vertex_at,
        make_t_junction,
    )
    records: list[_SplitRecord] = []
    if wire.a is None or wire.b is None:
        return records
    # EditorSession exposes the full SchematicState; bare-bones tests
    # may pass a ComponentState without ``.schematic``.  Resolve it
    # defensively so the gate is permissive on those.
    schematic = getattr(state, "schematic", None)
    # Snapshot original wire endpoint positions — these don't move,
    # but the wire's own ``a``/``b`` knot refs may change as the
    # splits retarget them to the shared hub.
    for endpoint_name, point in (("a", wire.path[0]), ("b", wire.path[-1])):
        hit = find_wire_with_interior_vertex_at(state.layout, point)
        if hit is None:
            continue
        w_through, vertex_idx = hit
        if w_through.id == wire.id:
            continue
        # Net-conflict gate.  When a fresh wire endpoint sits inside an
        # existing wire of a different net, we leave the geometry as a
        # crossing — collapsing it into a shared hub would short the two
        # nets.  When the endpoint is a pin with no net yet, the verdict
        # tells us which net to assign before splicing.
        verdict, orphan_pin, net_to_assign = can_form_t_junction(
            w_through, wire, endpoint_name,
            schematic=schematic,
            layout=state.layout,
            instances=state.instances,
            components=state.components,
            board=state.board,
        )
        if verdict is TJunctionVerdict.CONFLICT_REJECT:
            continue
        if verdict is TJunctionVerdict.INCOMPATIBLE_GEOMETRY:
            continue
        if verdict is TJunctionVerdict.ASSIGN_NET_TO_INCOMING_PIN:
            if schematic is None or orphan_pin is None or net_to_assign is None:
                continue
            net = schematic.nets.get(net_to_assign)
            if net is None:
                continue
            net.pins.add(orphan_pin)
        # Snapshot the pre-split state of the wire we're about to cut,
        # so revert can rebuild it from one piece.
        snapshot = _snapshot_wire(w_through, state.layout)
        try:
            w_a, w_b, _, hub = make_t_junction(
                state.layout, w_through, vertex_idx, wire, endpoint_name,
            )
        except ValueError:
            # Revert the speculative pin assignment if the split itself
            # failed — we don't want a dangling pin-to-net link without
            # the corresponding geometry.
            if verdict is TJunctionVerdict.ASSIGN_NET_TO_INCOMING_PIN:
                if schematic is not None and orphan_pin is not None and net_to_assign is not None:
                    _net = schematic.nets.get(net_to_assign)
                    if _net is not None:
                        _net.pins.discard(orphan_pin)
            continue
        records.append(_SplitRecord(
            original=snapshot,
            new_a_id=w_a.id,
            new_b_id=w_b.id,
            hub_id=hub.id,  # type: ignore[union-attr]
            incoming_endpoint=endpoint_name,
        ))
    return records


@dataclass
class _SplitRecord:
    """Snapshot of one ``make_t_junction`` for revert."""

    original: _WireSnapshot
    new_a_id: uuid.UUID
    new_b_id: uuid.UUID
    hub_id: uuid.UUID
    incoming_endpoint: str  # "a" or "b"


def _restore_split(
    state: ComponentState, wire: Wire, rec: _SplitRecord,
) -> None:
    """Inverse of ``make_t_junction`` — replace the two split halves
    with the original wire and detach ``wire``'s endpoint from the
    shared hub.
    """
    from protoboard.domain.layout.wire import Fragment as _Fragment
    # Detach wire's endpoint from the hub: revert its knot ref to the
    # original endpoint knot id, which was preserved on the wire
    # itself before make_t_junction ran (we stored it in ``rec`` only
    # implicitly — derive it from the wire's pre-split endpoint).
    # Concretely: the wire's matching endpoint knot was the same as
    # what AddWireCommand resolved earlier; ``make_t_junction``
    # swapped it to the hub.  AddWireCommand.revert removes the wire
    # entirely, so we don't need to revert the knot ref here —
    # restore the wires the split consumed.
    for new_id in (rec.new_a_id, rec.new_b_id):
        if state.layout.wires.get(new_id) is not None:
            state.layout.remove_wire(new_id)
    # Drop the hub if no other wires reference it (it was created by
    # make_t_junction and is now orphan after removing the two halves).
    hub = state.layout.knots.get(rec.hub_id)
    if hub is not None:
        from protoboard.domain.layout.knot import JointKnot
        if isinstance(hub, JointKnot):
            if not (hub.wire_ids - {rec.new_a_id, rec.new_b_id, wire.id}):
                state.layout.knots.pop(rec.hub_id, None)
    # Revive the original wire from snapshot.
    snap = rec.original
    fragments = [
        _Fragment(path=list(p), side=s)  # type: ignore[arg-type]
        for p, s in zip(snap.fragment_paths, snap.fragment_sides)
    ]
    revived = Wire(
        id=snap.wire_id,
        path=list(snap.path),
        insulated=snap.insulated,
        side=snap.side,  # type: ignore[arg-type]
        color=snap.color,
        fragments=fragments,
        a=KnotRef(id=snap.a_id),
        b=KnotRef(id=snap.b_id),
    )
    # Outer knots of the original may have been pruned by remove_wire
    # earlier (orphan after split); revive them from snapshot like
    # _restore_collapse does.
    from protoboard.domain.layout.knot import JointKnot as _JK
    for knot_id, snap_knot in (
        (snap.a_id, snap.a_knot), (snap.b_id, snap.b_knot),
    ):
        if knot_id in state.layout.knots:
            continue
        if snap_knot is None:
            continue
        state.layout.knots[knot_id] = _JK(
            id=knot_id, position=snap_knot.position,
        )
    state.layout.add_wire(revived)


def _collapse_endpoint_joints(
    state: ComponentState, wire: Wire,
) -> "list[_CollapseRecord]":
    """Run ``Layout.try_collapse_joint`` on both endpoints of ``wire``.

    Walks ``wire.a`` and ``wire.b`` (after they have been resolved and the
    wire inserted).  When an endpoint joint has exactly two incident wires
    with matching ``insulated``, fuse them into a single Wire — the joint
    disappears, the two source wires are replaced by one ``merged`` wire
    keeping the smaller id (deterministic).

    Returns a list of records describing each collapse so the AddWireCommand
    can roll the layout back on revert.  The list may contain 0, 1, or 2
    entries (one per endpoint that collapsed).  Pin endpoints are never
    collapsed — pins are physical hubs and remain solder targets.
    """
    from protoboard.domain.layout.wire_utils import plated_through_pads
    records: list[_CollapseRecord] = []
    # Materialise plated-through set once; cheap on small boards, used by
    # the cross-side collapse branch.  ``try_collapse_joint`` wants
    # ``frozenset`` so freeze here once.
    pt_pads = frozenset(plated_through_pads(state.board))
    # Track endpoint joint ids to visit.  We snapshot the current wire's
    # endpoints because the collapse can replace ``wire`` mid-loop (the
    # merged Wire inherits the lower of the two ids).
    if wire.a is None or wire.b is None:
        return records
    endpoint_ids = (wire.a.id, wire.b.id)
    for ep_id in endpoint_ids:
        # Look up joint freshly each iteration — the previous collapse may
        # have shifted the current wire's identity (new ``wire.a``/``b``).
        knot = state.layout.knots.get(ep_id)
        if not isinstance(knot, JointKnot):
            continue
        if len(knot.wire_ids) != 2:
            continue
        w_ids = tuple(knot.wire_ids)
        # Only collapse when the *other* wire has matching ``insulated`` —
        # ``try_collapse_joint`` already checks this, but we snapshot the
        # two source wires for revert before invoking it.
        w1 = state.layout.wires.get(w_ids[0])
        w2 = state.layout.wires.get(w_ids[1])
        if w1 is None or w2 is None:
            continue
        if w1.insulated != w2.insulated:
            continue
        snapshot = _CollapseRecord(
            joint_id=ep_id,
            joint_pos=knot.position,
            w1_snapshot=_snapshot_wire(w1, state.layout),
            w2_snapshot=_snapshot_wire(w2, state.layout),
        )
        merged = state.layout.try_collapse_joint(
            ep_id, plated_through_pads=pt_pads,
        )
        if merged is None:
            continue
        snapshot.merged_wire_id = merged.id
        records.append(snapshot)
        # Re-pin the wire so the second-endpoint pass sees the merged wire.
        wire = merged
    return records


@dataclass
class _WireSnapshot:
    """Frozen copy of a Wire's fields, sufficient to recreate it on revert."""

    wire_id: uuid.UUID
    path: list[Point]
    insulated: bool
    side: str
    color: "str | None"
    a_id: uuid.UUID
    b_id: uuid.UUID
    # Per-fragment side breakdown — preserves multi-fragment wires
    # (via boundaries).
    fragment_sides: list[str]
    fragment_paths: list[list[Point]]
    # Snapshots of the endpoint knots themselves so revert can revive any
    # that ``remove_wire`` later pruned as orphans (outer joints of the
    # source wires that had no other incident wire than this one).  Keyed
    # by knot id; ``None`` for ``PinKnot`` entries — pins live in
    # ``layout.pin_knots`` and survive ``remove_wire`` independently.
    a_knot: "JointKnot | None"
    b_knot: "JointKnot | None"


def _snapshot_wire(w: Wire, layout: Layout) -> _WireSnapshot:
    assert w.a is not None and w.b is not None
    a_knot = layout.knots.get(w.a.id)
    b_knot = layout.knots.get(w.b.id)
    return _WireSnapshot(
        wire_id=w.id,
        path=list(w.path),
        insulated=w.insulated,
        side=w.side,
        color=w.color,
        a_id=w.a.id,
        b_id=w.b.id,
        fragment_sides=[f.side for f in w.fragments],
        fragment_paths=[list(f.path) for f in w.fragments],
        a_knot=a_knot if isinstance(a_knot, JointKnot) else None,
        b_knot=b_knot if isinstance(b_knot, JointKnot) else None,
    )


@dataclass
class _CollapseRecord:
    """Snapshot of one ``try_collapse_joint`` operation for revert."""

    joint_id: uuid.UUID
    joint_pos: Point
    w1_snapshot: _WireSnapshot
    w2_snapshot: _WireSnapshot
    merged_wire_id: "uuid.UUID | None" = None


def _restore_collapse(state: ComponentState, rec: _CollapseRecord) -> None:
    """Inverse of ``try_collapse_joint`` — split the merged wire back into two.

    Removes the merged wire (which prunes any outer JointKnots that have
    no other incident wire), revives the central joint that was consumed
    by the collapse, revives any outer joints that ``remove_wire`` pruned
    as orphans, and re-inserts the two source wires.  PinKnots survive
    ``remove_wire`` independently — no revival needed for those.
    """
    from protoboard.domain.layout.wire import Fragment as _Fragment
    if rec.merged_wire_id is not None:
        merged = state.layout.wires.get(rec.merged_wire_id)
        if merged is not None:
            state.layout.remove_wire(merged.id)
    # Re-create the central joint consumed by the merge.
    if rec.joint_id not in state.layout.knots:
        revived_joint = JointKnot(id=rec.joint_id, position=rec.joint_pos)
        state.layout.knots[rec.joint_id] = revived_joint
    # Re-create any outer JointKnots that ``remove_wire`` pruned as
    # orphans.  ``add_wire`` below will repopulate their ``wire_ids``.
    for snap in (rec.w1_snapshot, rec.w2_snapshot):
        for knot_id, snap_knot in (
            (snap.a_id, snap.a_knot), (snap.b_id, snap.b_knot),
        ):
            if knot_id in state.layout.knots:
                continue
            if snap_knot is None:
                continue
            # Revive with empty wire_ids; add_wire repopulates.
            state.layout.knots[knot_id] = JointKnot(
                id=knot_id, position=snap_knot.position,
            )
    # Re-insert both source wires.
    for snap in (rec.w1_snapshot, rec.w2_snapshot):
        fragments = [
            _Fragment(path=list(p), side=s)  # type: ignore[arg-type]
            for p, s in zip(snap.fragment_paths, snap.fragment_sides)
        ]
        revived = Wire(
            id=snap.wire_id,
            path=list(snap.path),
            insulated=snap.insulated,
            side=snap.side,  # type: ignore[arg-type]
            color=snap.color,
            fragments=fragments,
            a=KnotRef(id=snap.a_id),
            b=KnotRef(id=snap.b_id),
        )
        state.layout.add_wire(revived)


@dataclass
class AddWireCommand(Command[ComponentState]):
    wire: Wire
    # Joint knots created on first apply; restored on redo, removed on revert.
    _created_joints: list[JointKnot] = field(
        default_factory=list, init=False, repr=False
    )
    # True once apply() has resolved knots and switched to v2 storage.
    _knots_resolved: bool = field(default=False, init=False, repr=False)
    # Split records — one per endpoint that produced a T-junction by
    # cutting an existing wire at the meeting pad.  Empty when no
    # interior-vertex collision was detected.
    _splits: list[_SplitRecord] = field(
        default_factory=list, init=False, repr=False
    )
    # Collapse records — one per endpoint that fused with a neighbouring
    # wire of matching ``insulated``.  Empty when no fusion happened.
    _collapses: list[_CollapseRecord] = field(
        default_factory=list, init=False, repr=False
    )

    @property
    def description(self) -> str:  # type: ignore[override]
        return "Add wire"

    def apply(self, state: ComponentState) -> None:
        # Enrich insulated wires with pad-position vertices so simplify /
        # unweave / edit tools have nodes at every pad the wire physically
        # crosses.  Idempotent — redo doesn't re-grow the path.
        self.wire = _enrich_insulated_with_pad_nodes(self.wire, state.board)
        if not self._knots_resolved:
            if self.wire.a is None or self.wire.b is None:
                start = self.wire.path[0]
                end = self.wire.path[-1]
                # Constrain joint reuse to compatible existing joints so
                # an autoroute pass cannot fuse wires of different nets
                # into a phantom degree-N hub at an off-pin pad (the
                # reference_big_project bug).
                wire_net_id = _wire_intended_net(state, self.wire)
                a_ref, a_created = _resolve_endpoint_knot(
                    state, start, side=self.wire.side, net_id=wire_net_id,
                )
                b_ref, b_created = _resolve_endpoint_knot(
                    state, end, side=self.wire.side, net_id=wire_net_id,
                )
                self.wire.a = a_ref
                self.wire.b = b_ref
                if a_created is not None:
                    self._created_joints.append(a_created)
                if b_created is not None:
                    self._created_joints.append(b_created)
            self._knots_resolved = True
        else:
            # Redo: re-register joints we created and dropped on revert.
            for jk in self._created_joints:
                state.layout.knots.setdefault(jk.id, jk)
        # add_wire is a no-op on redo when the previous revert restored
        # ``self.wire`` from a collapse snapshot — it's already in layout.
        if self.wire.id not in state.layout.wires:
            state.layout.add_wire(self.wire)
        # Interior-vertex split: when an endpoint of ``self.wire`` lands
        # on the interior vertex of an existing wire, atomically split
        # that wire and re-anchor our endpoint to the shared hub
        # (T-junction with degree-3 at the meeting pad).  ``make_t_junction``
        # mutates ``self.wire``'s ``a``/``b`` knot refs in place.
        new_splits = _split_existing_wires_at_endpoints(state, self.wire)
        if new_splits and not self._splits:
            self._splits = new_splits
        # Endpoint-merge: when this wire's endpoint sits on an off-pin pad
        # already used by exactly one other wire and they share
        # ``insulated``, the two wires represent one solderable artifact —
        # fuse them through ``try_collapse_joint``.  Pin endpoints are
        # never fused (pins are physical hubs).  We record each collapse
        # so revert() can undo it on demand.  Recompute on every apply
        # (including redo) so the layout state matches what the first
        # apply produced — the recorded snapshots are only used by revert.
        new_collapses = _collapse_endpoint_joints(state, self.wire)
        if new_collapses and not self._collapses:
            self._collapses = new_collapses

    def revert(self, state: ComponentState) -> None:
        if self._collapses:
            # Undo collapses in reverse order — ``_restore_collapse``
            # reinstates BOTH source wires (including ``self.wire`` in its
            # pre-collapse form, so we don't remove it again here).
            for rec in reversed(self._collapses):
                _restore_collapse(state, rec)
            # Now drop just ``self.wire`` so the layout reaches the
            # pre-apply state (one wire = the other source = neighbour
            # that existed before this AddWireCommand fired).
            if self.wire.id in state.layout.wires:
                state.layout.remove_wire(self.wire.id)
        else:
            if self.wire.id in state.layout.wires:
                state.layout.remove_wire(self.wire.id)
        # Undo any T-junction splits we created so the wires we cut
        # are restored to their pre-split single-piece form.  Run after
        # we removed ``self.wire`` so the hub becomes orphan-eligible.
        for rec in reversed(self._splits):
            _restore_split(state, self.wire, rec)
        # Joints we created may still hold back-refs to other wires (shared
        # joint).  ``remove_wire`` already prunes orphans; anything still
        # in layout.knots is a non-orphan we must leave alone.
        for jk in self._created_joints:
            if jk.id in state.layout.knots:
                continue  # not orphan after remove_wire; keep
            # Already removed by remove_wire as orphan — nothing to do.


@dataclass
class AddWireWithNetsCommand(Command[ComponentState]):
    """Add a wire and atomically record schematic net mutations for undo/redo.

    The resolver mutates session.schematic.nets BEFORE stack.execute() is
    called.  apply() on first invocation therefore only appends the wire.
    On redo, apply() also re-applies the net mutations recorded via
    record_net_change().  revert() always rolls back both the wire and nets.

    Typed as Command[ComponentState] so it fits the shared CommandStack, but
    requires the concrete state to also carry a `.schematic` attribute
    (satisfied at runtime by EditorSession).
    """

    wire: Wire
    # net_id → Net before mutation (None = net did not exist yet)
    _nets_before: dict[uuid.UUID, Net | None] = field(
        default_factory=dict, init=False, repr=False
    )
    # net_id → Net after mutation (None = net was deleted)
    _nets_after: dict[uuid.UUID, Net | None] = field(
        default_factory=dict, init=False, repr=False
    )
    _first_apply: bool = field(default=True, init=False, repr=False)
    _created_joints: list[JointKnot] = field(
        default_factory=list, init=False, repr=False
    )
    _knots_resolved: bool = field(default=False, init=False, repr=False)
    _splits: list[_SplitRecord] = field(
        default_factory=list, init=False, repr=False
    )
    _collapses: list[_CollapseRecord] = field(
        default_factory=list, init=False, repr=False
    )

    @property
    def description(self) -> str:  # type: ignore[override]
        return "Add wire (with nets)"

    def record_net_change(
        self,
        net_id: uuid.UUID,
        before: Net | None,
        after: Net | None,
    ) -> None:
        """Called by the resolver before each schematic mutation."""
        if net_id not in self._nets_before:
            self._nets_before[net_id] = before
        self._nets_after[net_id] = after

    def apply(self, state: ComponentState) -> None:
        # Enrich insulated wires the same way AddWireCommand does — single
        # chokepoint for every wire-creation path.
        self.wire = _enrich_insulated_with_pad_nodes(self.wire, state.board)
        if not self._knots_resolved:
            if self.wire.a is None or self.wire.b is None:
                start = self.wire.path[0]
                end = self.wire.path[-1]
                a_ref, a_created = _resolve_endpoint_knot(state, start)
                b_ref, b_created = _resolve_endpoint_knot(state, end)
                self.wire.a = a_ref
                self.wire.b = b_ref
                if a_created is not None:
                    self._created_joints.append(a_created)
                if b_created is not None:
                    self._created_joints.append(b_created)
            self._knots_resolved = True
        else:
            for jk in self._created_joints:
                state.layout.knots.setdefault(jk.id, jk)
        if self.wire.id not in state.layout.wires:
            state.layout.add_wire(self.wire)
        # Interior-vertex split + endpoint-merge — see AddWireCommand.apply
        # for the full rationale.  Same ordering matters: split first so
        # the hub exists at degree-3 before merge looks for degree-2
        # endpoint joints to collapse.
        new_splits = _split_existing_wires_at_endpoints(state, self.wire)
        if new_splits and not self._splits:
            self._splits = new_splits
        new_collapses = _collapse_endpoint_joints(state, self.wire)
        if new_collapses and not self._collapses:
            self._collapses = new_collapses
        if self._first_apply:
            # Net mutations already applied by resolver — nothing more to do.
            object.__setattr__(self, "_first_apply", False)
            return
        # Redo path: re-apply recorded net mutations.
        schematic = getattr(state, "schematic")  # EditorSession always has this
        for net_id, net_after in self._nets_after.items():
            if net_after is None:
                schematic.nets.pop(net_id, None)
            else:
                schematic.nets[net_id] = net_after

    def revert(self, state: ComponentState) -> None:
        for rec in reversed(self._collapses):
            _restore_collapse(state, rec)
        if self.wire.id in state.layout.wires:
            state.layout.remove_wire(self.wire.id)
        for rec in reversed(self._splits):
            _restore_split(state, self.wire, rec)
        schematic = getattr(state, "schematic")
        for net_id, net_before in self._nets_before.items():
            if net_before is None:
                schematic.nets.pop(net_id, None)
            else:
                schematic.nets[net_id] = net_before


@dataclass
class RemoveWireCommand(Command[ComponentState]):
    wire_id: uuid.UUID
    _saved_wire: Wire | None = field(default=None, init=False, repr=False)
    # JointKnots that became orphan when the wire was removed; restored on revert.
    _saved_orphan_joints: list[JointKnot] = field(
        default_factory=list, init=False, repr=False
    )

    @property
    def description(self) -> str:  # type: ignore[override]
        return "Remove wire"

    def apply(self, state: ComponentState) -> None:
        wire = state.layout.wires.get(self.wire_id)
        if wire is None:
            raise KeyError(f"Wire {self.wire_id} not found")
        object.__setattr__(self, "_saved_wire", wire)
        # Capture would-be-orphan JointKnots so revert can put them back.
        orphans: list[JointKnot] = []
        if wire.a is not None and wire.b is not None:
            for ref in (wire.a, wire.b):
                k = state.layout.knots.get(ref.id)
                if isinstance(k, JointKnot) and k.wire_ids == frozenset({wire.id}):
                    orphans.append(k)
            state.layout.remove_wire(self.wire_id)
        else:
            # Legacy fallback — wire without knots (shouldn't happen post
            # AddWireCommand migration, but tolerate during transition).
            state.layout.wires.remove(wire)
        self._saved_orphan_joints = orphans

    def revert(self, state: ComponentState) -> None:
        if self._saved_wire is None:
            return
        if self._saved_wire.a is not None and self._saved_wire.b is not None:
            for jk in self._saved_orphan_joints:
                state.layout.knots.setdefault(jk.id, jk)
            state.layout.add_wire(self._saved_wire)
        else:
            state.layout.wires.append(self._saved_wire)


@dataclass
class SetWireInsulatedCommand(Command[ComponentState]):
    """Toggle the insulated flag of a wire."""

    wire_id: uuid.UUID
    insulated: bool
    _old_insulated: bool = field(default=True, init=False, repr=False)

    @property
    def description(self) -> str:  # type: ignore[override]
        return "Set wire insulated" if self.insulated else "Set wire bare"

    def apply(self, state: ComponentState) -> None:
        for wire in state.layout.wires:
            if wire.id == self.wire_id:
                self._old_insulated = wire.insulated
                wire.insulated = self.insulated
                return
        raise KeyError(f"Wire {self.wire_id} not found")

    def revert(self, state: ComponentState) -> None:
        for wire in state.layout.wires:
            if wire.id == self.wire_id:
                wire.insulated = self._old_insulated
                return


@dataclass
class SetWireSideCommand(Command[ComponentState]):
    """Move a wire to the top or bottom layer."""

    wire_id: uuid.UUID
    side: str  # "top" or "bottom"
    _old_side: str = field(default="top", init=False, repr=False)

    @property
    def description(self) -> str:  # type: ignore[override]
        return f"Move wire to {self.side}"

    def apply(self, state: ComponentState) -> None:
        for wire in state.layout.wires:
            if wire.id == self.wire_id:
                self._old_side = wire.side
                wire.side = self.side  # type: ignore[assignment]
                return
        raise KeyError(f"Wire {self.wire_id} not found")

    def revert(self, state: ComponentState) -> None:
        for wire in state.layout.wires:
            if wire.id == self.wire_id:
                wire.side = self._old_side  # type: ignore[assignment]
                return


@dataclass
class SetPinVariantCommand(Command[ComponentState]):
    """Change the pin-name variant of a component instance."""

    component_id: uuid.UUID
    new_variant: str | None
    _old_variant: str | None = field(default=None, init=False, repr=False)

    @property
    def description(self) -> str:  # type: ignore[override]
        return f"Set pin variant to {self.new_variant or 'default'}"

    def apply(self, state: ComponentState) -> None:
        inst = state.instances[self.component_id]
        self._old_variant = inst.pin_variant
        import dataclasses as _dc
        state.instances[self.component_id] = _dc.replace(inst, pin_variant=self.new_variant)

    def revert(self, state: ComponentState) -> None:
        inst = state.instances[self.component_id]
        import dataclasses as _dc
        state.instances[self.component_id] = _dc.replace(inst, pin_variant=self._old_variant)


@dataclass
class FlipWireFragmentCommand(Command[ComponentState]):
    """Flip a contiguous fragment of a wire to the opposite layer.

    The fragment is identified by *start_idx* and *end_idx* (inclusive) in
    the wire's current path.  If the fragment spans the whole wire, the wire's
    ``side`` attribute is toggled and no splitting occurs.  Otherwise the
    original wire is replaced by up to three new wires:

    - prefix  : path[:start_idx+1]  on the original side  (omitted if start==0)
    - middle  : path[start_idx:end_idx+1]  on the target side
    - suffix  : path[end_idx:]  on the original side  (omitted if end==last)

    Undo restores the original single wire.
    """

    wire_id: uuid.UUID
    start_idx: int
    end_idx: int
    target_side: str  # "top" or "bottom"
    # Filled on first apply for undo:
    _original_wire: object = field(default=None, init=False, repr=False)
    _added_ids: list[uuid.UUID] = field(default_factory=list, init=False, repr=False)

    @property
    def description(self) -> str:  # type: ignore[override]
        return f"Flip wire fragment to {self.target_side}"

    def apply(self, state: ComponentState) -> None:
        from protoboard.domain.layout.wire import Wire

        wire: Wire | None = next(
            (w for w in state.layout.wires if w.id == self.wire_id), None
        )
        if wire is None:
            raise KeyError(f"Wire {self.wire_id} not found")

        self._original_wire = wire
        path = wire.path
        start, end = self.start_idx, self.end_idx
        original_side = wire.side

        # Remove the original wire
        state.layout.wires.pop(self.wire_id, None)
        self._added_ids.clear()

        if start == 0 and end == len(path) - 1:
            # Whole wire — just change side, no split
            new_wire = Wire(
                id=self.wire_id,
                path=list(path),
                insulated=wire.insulated,
                side=self.target_side,  # type: ignore[arg-type]
                color=wire.color,
            )
            state.layout.wires.append(new_wire)
            self._added_ids.append(new_wire.id)
            return

        # Split into up to 3 segments
        segments: list[tuple[list, str]] = []
        if start > 0:
            segments.append((path[:start + 1], original_side))
        segments.append((path[start:end + 1], self.target_side))
        if end < len(path) - 1:
            segments.append((path[end:], original_side))

        for seg_path, seg_side in segments:
            if len(seg_path) < 2:
                continue
            new_wire = Wire(
                id=uuid.uuid4(),
                path=list(seg_path),
                insulated=wire.insulated,
                side=seg_side,  # type: ignore[arg-type]
                color=wire.color,
            )
            state.layout.wires.append(new_wire)
            self._added_ids.append(new_wire.id)

    def revert(self, state: ComponentState) -> None:
        from protoboard.domain.layout.wire import Wire

        # Remove added wires
        for added_id in self._added_ids:
            state.layout.wires.pop(added_id, None)
        # Restore original
        if self._original_wire is not None:
            state.layout.wires.append(self._original_wire)  # type: ignore[arg-type]


@dataclass
class DisconnectPinCommand(Command[SchematicState]):
    """Remove a pin from its net. Deletes the net if it becomes empty."""

    component_id: uuid.UUID
    pin_number: int
    _saved_net_id: uuid.UUID | None = field(default=None, init=False, repr=False)
    _saved_net_name: str = field(default="", init=False, repr=False)
    _saved_pins: object = field(default=None, init=False, repr=False)  # set[PinRef]|None

    @property
    def description(self) -> str:  # type: ignore[override]
        return f"Disconnect pin {self.pin_number}"

    def apply(self, state: SchematicState) -> None:
        from protoboard.domain.schematic.net import PinRef

        ref = PinRef(component_id=self.component_id, pin_number=self.pin_number)
        net = state.schematic.net_for_pin(ref)
        if net is None:
            return
        self._saved_net_id = net.id
        self._saved_net_name = net.name
        self._saved_pins = net.pins.copy()
        net.pins.discard(ref)
        if not net.pins:
            del state.schematic.nets[net.id]

    def revert(self, state: SchematicState) -> None:
        from protoboard.domain.schematic.net import Net, PinRef

        if self._saved_net_id is None or self._saved_pins is None:
            return
        ref = PinRef(component_id=self.component_id, pin_number=self.pin_number)
        net = state.schematic.nets.get(self._saved_net_id)
        if net is not None:
            net.pins.add(ref)
        else:
            # Net was deleted (became empty) — restore it.
            saved = self._saved_pins
            assert isinstance(saved, set)
            state.schematic.nets[self._saved_net_id] = Net(
                id=self._saved_net_id,
                name=self._saved_net_name,
                pins=saved,
            )


@dataclass
class LockComponentCommand(Command[ComponentState]):
    """Toggle the locked flag on a component placement."""

    component_id: uuid.UUID
    locked: bool
    _old_locked: bool = field(default=False, init=False, repr=False)

    @property
    def description(self) -> str:  # type: ignore[override]
        return "Lock component" if self.locked else "Unlock component"

    def apply(self, state: ComponentState) -> None:
        pl = state.layout.placements.get(self.component_id)
        if pl is None:
            return
        self._old_locked = pl.locked
        pl.locked = self.locked

    def revert(self, state: ComponentState) -> None:
        pl = state.layout.placements.get(self.component_id)
        if pl is None:
            return
        pl.locked = self._old_locked


# ---------------------------------------------------------------------------
# Knot/Fragment-aware path-edit command
# ---------------------------------------------------------------------------


from protoboard.domain.layout.wire import Fragment as _Fragment


@dataclass
class ReplaceWireFragmentsCommand(Command[ComponentState]):
    """Replace ``Wire.fragments`` in place — preserving ``Wire.id``.

    This is the explicit v2 counterpart of the path-edit operations
    (simplify, unweave, nudge, …) that today mutate Wire.path or
    mint fresh segments.  Identity stays stable; highlight, recording and
    undo follow.
    """

    wire_id: uuid.UUID
    new_fragments: tuple[_Fragment, ...]
    # Caller-supplied pre-mutation fragments; if empty, apply() reads from state.
    # Mirrors the old_path escape hatch on ReplaceWireSegmentCommand for callers
    # (drag-commit) that mutate the wire in place before issuing the command.
    old_fragments: tuple[_Fragment, ...] = field(default=())
    _saved_fragments: tuple[_Fragment, ...] = field(default=(), init=False, repr=False)
    # Pre-apply positions of any free (degree-1) JointKnot endpoints, so
    # revert can slide them back to the original spot even when an
    # interleaved RemoveWireCommand later restored a sibling wire onto
    # the same joint (which would otherwise push degree above 1 and
    # cause ``replace_wire_fragments`` to refuse the slide).
    _saved_a_pos: "Point | None" = field(default=None, init=False, repr=False)
    _saved_b_pos: "Point | None" = field(default=None, init=False, repr=False)

    @property
    def description(self) -> str:  # type: ignore[override]
        return "Replace wire fragments"

    def apply(self, state: ComponentState) -> None:
        wire = state.layout.wires.get(self.wire_id)
        if wire is None:
            return
        if self.old_fragments:
            self._saved_fragments = self.old_fragments
        else:
            self._saved_fragments = tuple(wire.fragments)
        # Snapshot pre-apply JointKnot positions so revert can restore
        # them irrespective of how many wires the joint ends up holding
        # at revert time (see _saved_a_pos / _saved_b_pos docstring).
        if wire.a is not None:
            ka = state.layout.knots.get(wire.a.id)
            if isinstance(ka, JointKnot):
                self._saved_a_pos = ka.position
        if wire.b is not None:
            kb = state.layout.knots.get(wire.b.id)
            if isinstance(kb, JointKnot):
                self._saved_b_pos = kb.position
        state.layout.replace_wire_fragments(self.wire_id, self.new_fragments)

    def revert(self, state: ComponentState) -> None:
        if not self._saved_fragments:
            return
        # Restore JointKnot positions FIRST, so the subsequent
        # ``replace_wire_fragments`` call sees endpoints that match the
        # saved fragments and skips the slide-validation that would
        # otherwise reject a degree-2+ joint (the joint may have grown
        # incident-wire count between apply and revert when an
        # interleaved ``RemoveWireCommand`` was reverted before us).
        wire = state.layout.wires.get(self.wire_id)
        if wire is not None:
            if wire.a is not None and self._saved_a_pos is not None:
                ka = state.layout.knots.get(wire.a.id)
                if isinstance(ka, JointKnot):
                    ka.position = self._saved_a_pos
            if wire.b is not None and self._saved_b_pos is not None:
                kb = state.layout.knots.get(wire.b.id)
                if isinstance(kb, JointKnot):
                    kb.position = self._saved_b_pos
        state.layout.replace_wire_fragments(self.wire_id, self._saved_fragments)


# ---------------------------------------------------------------------------
# Wire-update helper (used by MoveComponentCommand)
# ---------------------------------------------------------------------------

_EPS = 1e-6  # tolerance for point equality (raster units)


def _pts_equal(a: Point, b: Point) -> bool:
    return abs(a.x - b.x) < _EPS and abs(a.y - b.y) < _EPS


def _warn_pins_off_pads(
    state: ComponentState,
    comp_id: uuid.UUID,
    placement: ComponentPlacement,
) -> None:
    """Log a warning if any pin of the component does not land on a board pad."""
    inst = state.instances.get(comp_id)
    if inst is None:
        return
    defn = state.components.get(inst.definition_ref)
    if defn is None:
        return
    from protoboard.domain.board.boxes import GapAtom, StripAtom
    pad_set: frozenset[tuple[float, float]] = frozenset(
        (center.x, center.y)
        for atom, center in state.board.atoms()
        if not isinstance(atom, GapAtom)
    )
    misses = validate_pins_on_pads(defn.footprint, placement, pad_set)
    if misses:
        _log.warning(
            "placement.pins_off_pads",
            comp_id=str(comp_id),
            refdes=inst.refdes,
            placement_pos=f"({placement.position.x:.4f}, {placement.position.y:.4f})",
            rotation=placement.rotation,
            misses=[
                {"pin": pin_num, "pos": f"({wp.x:.4f}, {wp.y:.4f})"}
                for pin_num, wp in misses
            ],
        )


def _move_blocked(
    state: ComponentState,
    comp_id: uuid.UUID,
    old_pl: ComponentPlacement,
    new_pl: ComponentPlacement,
) -> bool:
    """Return True if the move would cause a pin overlap or body overlap."""
    inst = state.instances.get(comp_id)
    if inst is None:
        return False
    defn = state.components.get(inst.definition_ref)
    if defn is None:
        return False

    new_bbox = component_bbox(defn.footprint, new_pl, defn.component_class)

    n_pins = defn.footprint.pin_count()
    new_pin_positions: set[tuple[float, float]] = set()
    for pin_num in range(1, n_pins + 1):
        p = pin_world_position(defn.footprint, new_pl, pin_num)
        new_pin_positions.add((round(p.x, 4), round(p.y, 4)))

    for other_id, other_pl in state.layout.placements.items():
        if other_id == comp_id:
            continue
        other_inst = state.instances.get(other_id)
        if other_inst is None:
            continue
        other_defn = state.components.get(other_inst.definition_ref)
        if other_defn is None:
            continue

        other_bbox = component_bbox(other_defn.footprint, other_pl, other_defn.component_class)
        if new_bbox.intersects(other_bbox):
            # Only block when no pin of the moved component coincides with a pin
            # of the other — that would be a shared-pad scenario which the GUI
            # conflict resolver handles separately.
            other_pin_positions: set[tuple[float, float]] = set()
            for pin_num in range(1, other_defn.footprint.pin_count() + 1):
                p = pin_world_position(other_defn.footprint, other_pl, pin_num)
                other_pin_positions.add((round(p.x, 4), round(p.y, 4)))
            if new_pin_positions.isdisjoint(other_pin_positions):
                return True

    return False


def _update_wires_for_move(
    state: ComponentState,
    comp_id: uuid.UUID,
    old_pl: ComponentPlacement,
    new_pl: ComponentPlacement,
    exclude_ids: frozenset[uuid.UUID] = frozenset(),
) -> None:
    """Adjust wire endpoints that coincide with pins of the moved component.

    For each pin of the component, if a wire endpoint was at the old pin
    position, move it to the new pin position.  If the resulting path
    collapses (start == end) or has fewer than 2 distinct points, the
    wire is removed.

    exclude_ids: wire IDs already translated by live drag — skipped here to
    avoid double-translation when old_pin_pos of this component coincides with
    the (already-shifted) position of another component's pin.
    """
    inst = state.instances.get(comp_id)
    if inst is None:
        return
    defn = state.components.get(inst.definition_ref)
    if defn is None:
        return

    n_pins = defn.footprint.pin_count()
    # Build map: old_pin_pos → new_pin_pos
    pin_map: dict[tuple[float, float], Point] = {}
    for pin_num in range(1, n_pins + 1):
        old_pos = pin_world_position(defn.footprint, old_pl, pin_num)
        new_pos = pin_world_position(defn.footprint, new_pl, pin_num)
        if not _pts_equal(old_pos, new_pos):
            pin_map[(round(old_pos.x, 6), round(old_pos.y, 6))] = new_pos

    if not pin_map:
        return

    from protoboard.domain.layout.wire_utils import wire_has_skewed_segment

    new_wires: list[Wire] = []
    for wire in state.layout.wires:
        if wire.id in exclude_ids:
            # Even excluded (already live-translated) wires may have ended up
            # with a skewed segment if the component's pin offsets stretched
            # the path unevenly. Drop those.
            if wire_has_skewed_segment(wire):
                continue
            new_wires.append(wire)
            continue
        new_path: list[Point] = []
        for pt in wire.path:
            key = (round(pt.x, 6), round(pt.y, 6))
            new_path.append(pin_map.get(key, pt))
        # Drop wire if it collapsed (all points identical or < 2 distinct).
        distinct = {(round(p.x, 6), round(p.y, 6)) for p in new_path}
        if len(distinct) < 2:
            continue
        if any(
            not _pts_equal(new_path[i], wire.path[i])
            for i in range(len(new_path))
        ):
            wire.path = new_path
            # Keep fragments in sync — same boundary topology, translated
            # vertices via pin_map.  Single-fragment wires stay single.
            if wire.fragments:
                new_fragments_local = []
                for frag in wire.fragments:
                    translated = [
                        pin_map.get(
                            (round(p.x, 6), round(p.y, 6)), p,
                        )
                        for p in frag.path
                    ]
                    new_fragments_local.append(
                        type(frag)(path=translated, side=frag.side),
                    )
                wire.fragments = new_fragments_local
        # Drop wire if any segment became skewed (neither orthogonal nor
        # diagonal) after the endpoint translation.  Cannot be represented
        # by the routing/cost model and is almost always a visual artifact
        # of repeated drag.
        if wire_has_skewed_segment(wire):
            continue
        new_wires.append(wire)

    state.layout.wires[:] = new_wires
