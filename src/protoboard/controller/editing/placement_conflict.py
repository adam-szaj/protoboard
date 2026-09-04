# SPDX-License-Identifier: MIT
"""Pad / wire conflict resolution for a component placement — Qt-free.

Moved out of ``gui/placement_conflict.py`` so the placement verb can resolve
conflicts headlessly. Everything here is Qt-free: the interactive decisions are
taken through the injected :class:`PlacementPrompts` (qt-ports D4). The Qt
default prompts (``qt_placement_prompts``) and the drag-commit classifier stay
in the GUI module, which re-exports these names for its existing callers.
"""

from __future__ import annotations

import uuid
from dataclasses import dataclass
from enum import Enum
from typing import TYPE_CHECKING

import structlog

from protoboard.domain.commands.component_cmds import AddWireCommand, RemoveWireCommand
from protoboard.domain.components.resolution import iter_placed_components
from protoboard.domain.layout.pad_conflict import (
    ConflictKind,
    PadConflict,
    find_conflicts,
    find_wire_conflicts,
    footprint_of,
)
from protoboard.domain.layout.placement import (
    ComponentPlacement,
    component_bbox,
    iter_footprint_pin_positions,
)
from protoboard.domain.layout.wire import Wire
from protoboard.domain.routing.cost_model import RoutingCostModel
from protoboard.domain.schematic.net import Net, PinRef
from protoboard.geometry import round_coord as _round

if TYPE_CHECKING:
    from collections.abc import Callable

    from protoboard.controller.editing.contexts import EditingReadContext
    from protoboard.controller.ports.routing import PathfinderPort
    from protoboard.domain.components.definition import ComponentDefinition
    from protoboard.domain.components.instance import ComponentInstance

_log = structlog.get_logger(__name__)


class WireConflictAction(Enum):
    """What to do with wires colliding with a placed component.

    The pure decision enum (qt-ports D4) — the Qt ``WireConflictDialog``
    re-exports it for its UI, but headless prompts return it without
    touching Qt.
    """

    CANCEL = "cancel"
    REMOVE_AND_REROUTE = "remove_and_reroute"
    ADD_TO_NET = "add_to_net"
    CREATE_NET = "create_net"


@dataclass(frozen=True)
class PadConflictRequest:
    """The pad-conflict question shown before a placement commits."""

    conflicts: tuple[PadConflict, ...]
    incoming_refdes: str
    # conflict-identity (``id(conflict)``) → the existing component's refdes,
    # the shape ``PadConflictDialog`` already consumes.
    existing_refdes: dict[int, str]


@dataclass(frozen=True)
class WireConflictRequest:
    """The wire-conflict question (three-way action choice)."""

    conflicts: tuple[PadConflict, ...]
    incoming_refdes: str


@dataclass(frozen=True)
class PlacementPrompts:
    """The interactive surface of placement-conflict resolution (D4).

    Two injected decisions: *pad* answers "accept the shared-pad
    placement?" (also invoked, result ignored, to *inform* about a blocked
    one), *wire* picks a :class:`WireConflictAction` or ``None`` on cancel.
    The Qt defaults open the modal dialogs (``qt_placement_prompts`` in the GUI
    module); tests inject canned callables and place components headlessly.
    """

    pad: Callable[[PadConflictRequest], bool]
    wire: Callable[[WireConflictRequest], WireConflictAction | None]


@dataclass(frozen=True)
class PlacementResolution:
    """Result of :func:`check_and_resolve`: the resolved placement plus any
    follow-up wire commands the resolution produced.

    Replaces the previous pattern of monkey-attaching ``_wire_cmds`` onto the
    (otherwise immutable) :class:`ComponentPlacement` — an explicit result type
    keeps the placement clean and the wire commands typed.
    """

    placement: ComponentPlacement
    wire_cmds: tuple[object, ...] = ()


def check_and_resolve(
    session: EditingReadContext,
    placement: ComponentPlacement,
    defn: ComponentDefinition,
    instance_id: uuid.UUID,
    prompts: PlacementPrompts,
    pathfinder: PathfinderPort,
    instance: ComponentInstance | None = None,
) -> PlacementResolution | None:
    """Check pad conflicts for *placement* and resolve interactively.

    Returns:
    - Updated ComponentPlacement (with shared_pins set) if placement is allowed.
    - None if placement is blocked (different-net conflict or user declined).

    Side-effects when returning non-None:
    - Updates schematic (adds unconnected pins to nets) directly on session.

    *prompts* carries the two user decisions (qt-ports D4). The GUI wrapper
    defaults it to the parentless Qt dialogs; this Qt-free function requires it.

    *instance* is the (preferably bound) incoming instance. At placement time it
    is not yet in ``session.instances``, so the caller must pass it — otherwise
    the incoming geometry falls back to the definition default footprint (wrong
    pin spacing for a chosen variant). When omitted, it is looked up by id (the
    move/re-check path, where the instance is already registered).
    """
    # Body-overlap check: block if the incoming bbox intersects a placed component
    # AND no pin of the incoming component actually coincides with a pin of that
    # component (which would be a shared-pad case handled by pad-conflict below).
    #
    # Geometry comes from each component's *resolved variant* footprint (the body
    # the user actually chose), not the definition default — otherwise the bbox
    # and pin cloud use the wrong pin spacing and the overlap/coincidence test is
    # wrong (e.g. an SMD-0805 part checked against axial 3.2-hole pitch).
    incoming_instance = instance or session.instances.get(instance_id)
    in_footprint = footprint_of(defn, incoming_instance)
    incoming_bbox = component_bbox(in_footprint, placement, defn.component_class)
    _EPS = 1e-4
    for other in iter_placed_components(
        session.layout.placements,
        session.instances,
        session.components,
    ):
        if other.placement.component_id == instance_id:
            continue
        other_footprint = footprint_of(other.definition, other.instance)
        other_bbox = component_bbox(
            other_footprint,
            other.placement,
            other.definition.component_class,
        )
        if not incoming_bbox.intersects(other_bbox):
            continue
        # Bboxes overlap — check whether any pin pair actually coincides.
        # If at least one pin coincides this is a shared-pad scenario: let
        # the pad-conflict logic below decide.  If no pin coincides at all,
        # it is a pure body-on-body overlap → block.
        has_pin_overlap = False
        for _in_pin, in_pos in iter_footprint_pin_positions(in_footprint, placement):
            for _ot_pin, ot_pos in iter_footprint_pin_positions(
                other_footprint, other.placement
            ):
                if abs(in_pos.x - ot_pos.x) < _EPS and abs(in_pos.y - ot_pos.y) < _EPS:
                    has_pin_overlap = True
                    break
            if has_pin_overlap:
                break
        if not has_pin_overlap:
            _log.debug("conflict.body_overlap_blocked", incoming=str(instance_id))
            return None

    # PadIndex is the authoritative pad-state source.  For single-component
    # placement we can use the session's index directly: pins of `instance_id`
    # at their *old* positions are excluded by id when computing conflicts,
    # and the new pin positions are looked up against pads that other
    # components already occupy.  Test stubs without a ``pad_index`` method
    # fall back to the legacy from-layout path.
    pad_index = session.pad_index() if hasattr(session, "pad_index") else None

    pad_conflicts = find_conflicts(
        incoming_placement=placement,
        incoming_defn=defn,
        incoming_instance_id=instance_id,
        layout_placements=session.layout.placements,
        instances=session.instances,
        components=session.components,
        schematic=session.schematic,
        pad_index=pad_index,
        incoming_instance=incoming_instance,
    )

    # Build point→net map for wire-conflict detection.
    # Maps rounded (x, y) of any placed pin that has a net → (net_id, net_name).
    point_to_net: dict[tuple[float, float], tuple[uuid.UUID, str]] = {}
    for ref, net in session.schematic.pin_net_map().items():
        p = session.get_pin_position(ref.component_id, ref.pin_number)
        if p is None:
            continue
        point_to_net[(_round(p.x), _round(p.y))] = (net.id, net.name)

    already_conflicted = {
        (_round(c.position.x), _round(c.position.y)) for c in pad_conflicts
    }
    wire_conflicts = find_wire_conflicts(
        incoming_placement=placement,
        incoming_defn=defn,
        incoming_instance_id=instance_id,
        wires=session.layout.wires.values(),
        schematic=session.schematic,
        point_to_net=point_to_net,
        already_conflicted=already_conflicted,
        pad_index=pad_index,
        incoming_instance=incoming_instance,
    )

    # --- Separate pad conflicts from wire conflicts ---
    _WIRE_KINDS = {
        ConflictKind.PIN_ON_WIRE_DIFFERENT_NET,
        ConflictKind.PIN_ON_WIRE_SAME_NET,
        ConflictKind.PIN_ON_WIRE_WITH_NET,
        ConflictKind.PIN_ON_WIRE_UNCONNECTED,
    }
    pure_pad = [c for c in pad_conflicts if c.kind not in _WIRE_KINDS]
    wire_only = wire_conflicts  # all wire_conflicts are already wire-kind

    # --- Pad-on-pad conflict resolution (unchanged logic) ---
    _PAD_BLOCKED = {ConflictKind.DIFFERENT_NETS}
    _PAD_AUTO_OK = {ConflictKind.SAME_NET}
    _PAD_DECISION = {ConflictKind.ONE_UNCONNECTED, ConflictKind.BOTH_UNCONNECTED}

    pad_blocked = [c for c in pure_pad if c.kind in _PAD_BLOCKED]
    pad_auto_ok = [c for c in pure_pad if c.kind in _PAD_AUTO_OK]
    pad_decision = [c for c in pure_pad if c.kind in _PAD_DECISION]

    if pad_blocked:
        _log.info("conflict.pad_blocked", kinds=[c.kind.name for c in pad_blocked])
        _ask_pad_conflicts(
            session, pad_blocked + pad_decision, defn, instance_id, prompts
        )
        return None

    if pad_decision:
        accepted = _ask_pad_conflicts(session, pure_pad, defn, instance_id, prompts)
        if not accepted:
            return None

    new_shared = set(placement.shared_pins)
    for c in pad_auto_ok + pad_decision:
        new_shared.add(c.incoming_pin)
    if pad_decision:
        _update_schematic(session, pad_decision, placement, defn, instance_id)

    # --- Wire conflict resolution (new: dialog with choices) ---
    wire_action_cmds: list[object] = []
    if wire_only:
        _same = ConflictKind.PIN_ON_WIRE_SAME_NET
        _wire_needs = [c for c in wire_only if c.kind != _same]

        if _wire_needs:
            result_pl, wire_action_cmds = _resolve_wire_conflicts(
                session=session,
                wire_conflicts=_wire_needs,
                placement=placement,
                defn=defn,
                instance_id=instance_id,
                prompts=prompts,
                pathfinder=pathfinder,
            )
            if result_pl is None:
                # Revert shared_pins already applied to pad conflicts.
                return None
            placement = result_pl

    result = placement.copy()
    result.shared_pins = frozenset(new_shared)
    return PlacementResolution(placement=result, wire_cmds=tuple(wire_action_cmds))


def _ask_pad_conflicts(
    session: EditingReadContext,
    conflicts: list[PadConflict],
    defn: ComponentDefinition,
    instance_id: uuid.UUID,
    prompts: PlacementPrompts,
) -> bool:
    """Ask the pad-conflict prompt. Returns True if the user accepted."""
    # Same-net-only: auto-accept without asking.
    if all(c.kind == ConflictKind.SAME_NET for c in conflicts):
        return True

    # Build refdes map: conflict-identity → the existing component's refdes.
    existing_refdes: dict[int, str] = {}
    for c in conflicts:
        inst = session.instances.get(c.existing_comp_id)
        existing_refdes[id(c)] = inst.refdes if inst else "?"

    inst = session.instances.get(instance_id)
    incoming_refdes = inst.refdes if inst else defn.name

    return prompts.pad(
        PadConflictRequest(
            conflicts=tuple(conflicts),
            incoming_refdes=incoming_refdes,
            existing_refdes=existing_refdes,
        )
    )


def _update_schematic(
    session: EditingReadContext,
    conflicts: list[PadConflict],
    placement: ComponentPlacement,  # noqa: ARG001
    defn: ComponentDefinition,  # noqa: ARG001
    instance_id: uuid.UUID,
) -> None:
    """Add unconnected pins to nets after conflicts are resolved."""
    _PAD_KINDS = {ConflictKind.ONE_UNCONNECTED, ConflictKind.BOTH_UNCONNECTED}
    _WIRE_KINDS = {
        ConflictKind.PIN_ON_WIRE_UNCONNECTED,
        ConflictKind.PIN_ON_WIRE_WITH_NET,
    }

    for c in conflicts:
        incoming_ref = PinRef(component_id=instance_id, pin_number=c.incoming_pin)

        if c.kind in _PAD_KINDS:
            existing_ref = PinRef(
                component_id=c.existing_comp_id, pin_number=c.existing_pin
            )
            if c.existing_net_id is not None:
                session.schematic.add_pin_to_net(incoming_ref, c.existing_net_id)
            elif c.incoming_net_id is not None:
                session.schematic.add_pin_to_net(existing_ref, c.incoming_net_id)
            else:
                inst = session.instances.get(instance_id)
                new_net_id = uuid.uuid4()
                net_name = f"Net-({inst.refdes if inst else '?'}-{c.incoming_pin})"
                session.schematic.nets[new_net_id] = Net(
                    id=new_net_id,
                    name=net_name,
                    pins={incoming_ref, existing_ref},
                )
                _log.info("conflict.net_created", net_name=net_name, kind=c.kind.name)

        elif c.kind in _WIRE_KINDS:
            if c.existing_net_id is not None:
                session.schematic.add_pin_to_net(incoming_ref, c.existing_net_id)
            else:
                inst = session.instances.get(instance_id)
                new_net_id = uuid.uuid4()
                session.schematic.nets[new_net_id] = Net(
                    id=new_net_id,
                    name=f"Net-({inst.refdes if inst else '?'}-{c.incoming_pin})",
                    pins={incoming_ref},
                )


def _resolve_wire_conflicts(
    session: EditingReadContext,
    wire_conflicts: list[PadConflict],
    placement: ComponentPlacement,
    defn: ComponentDefinition,
    instance_id: uuid.UUID,
    prompts: PlacementPrompts,
    pathfinder: PathfinderPort,
) -> tuple[ComponentPlacement | None, list[object]]:
    """Ask the wire-conflict prompt and build commands for the chosen action.

    Returns (resolved_placement, extra_commands) or (None, []) on cancel.
    """
    inst = session.instances.get(instance_id)
    incoming_refdes = inst.refdes if inst else defn.name

    action = prompts.wire(
        WireConflictRequest(
            conflicts=tuple(wire_conflicts), incoming_refdes=incoming_refdes
        )
    )
    cmds: list[object] = []

    if action is None or action == WireConflictAction.CANCEL:
        return None, []

    if action == WireConflictAction.REMOVE_AND_REROUTE:
        # Collect unique wire IDs to remove.
        seen: set[uuid.UUID] = set()
        for c in wire_conflicts:
            if c.wire_id is not None and c.wire_id not in seen:
                seen.add(c.wire_id)
                wire_obj = session.layout.wires.get(c.wire_id)
                cmds.append(RemoveWireCommand(wire_id=c.wire_id))
                if wire_obj is not None:
                    reroute_cmd = _try_reroute_wire(
                        session, pathfinder, wire_obj, placement
                    )
                    if reroute_cmd is not None:
                        cmds.append(reroute_cmd)
        return placement, cmds

    if action == WireConflictAction.ADD_TO_NET:
        for c in wire_conflicts:
            ref = PinRef(component_id=instance_id, pin_number=c.incoming_pin)
            if c.existing_net_id is not None:
                session.schematic.add_pin_to_net(ref, c.existing_net_id)
        return placement, []

    if action == WireConflictAction.CREATE_NET:
        for c in wire_conflicts:
            ref = PinRef(component_id=instance_id, pin_number=c.incoming_pin)
            new_net_id = uuid.uuid4()
            net_name = f"Net-({incoming_refdes}-{c.incoming_pin})"
            session.schematic.nets[new_net_id] = Net(
                id=new_net_id, name=net_name, pins={ref}
            )
        return placement, []

    return None, []


def _try_reroute_wire(
    session: EditingReadContext,
    pathfinder: PathfinderPort,
    wire: object,
    blocker_placement: ComponentPlacement,  # noqa: ARG001
) -> object | None:
    """Attempt A* reroute for *wire* after its removal.

    Returns an AddWireWithNetsCommand if a path is found, else None.
    """
    if not isinstance(wire, Wire) or len(wire.path) < 2:
        return None

    source = wire.path[0]
    target = wire.path[-1]

    graph = session.connectivity_graph()
    results = pathfinder.find_paths(
        graph=graph,
        source=source,
        targets=[target],
        side=wire.side,
        cost_model=RoutingCostModel(),
        k=1,
        insulated=wire.insulated,
    )
    if not results:
        return None

    best = results[0]
    new_wire = Wire(
        id=uuid.uuid4(),
        path=[pt for pt, _side in best.path],
        insulated=wire.insulated,
        side=wire.side,
    )
    return AddWireCommand(wire=new_wire)


def build_wire_removal_cmds(
    wire_ids: set[uuid.UUID] | frozenset[uuid.UUID],
) -> list[object]:
    """Build RemoveWireCommand instances for *wire_ids*.

    Extracted from ``_resolve_wire_conflicts`` so the drag pipeline can
    auto-remove wires without going through the dialog.  Reroute is *not*
    attempted here — the user-confirmed policy for drag commits is "remove
    silently with toast + Ctrl+Z".  Callers wanting reroute can build
    `_try_reroute_wire` follow-ups separately.
    """
    cmds: list[object] = []
    seen: set[uuid.UUID] = set()
    for wid in wire_ids:
        if wid in seen:
            continue
        seen.add(wid)
        cmds.append(RemoveWireCommand(wire_id=wid))
    return cmds


__all__ = [
    "PadConflictRequest",
    "PlacementPrompts",
    "PlacementResolution",
    "WireConflictAction",
    "WireConflictRequest",
    "build_wire_removal_cmds",
    "check_and_resolve",
]
