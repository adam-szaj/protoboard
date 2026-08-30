# SPDX-License-Identifier: MIT
"""Pad-conflict detection and resolution logic for component placement.

Pure functions — no Qt, no side effects.
"""

from __future__ import annotations

import math
import uuid
from dataclasses import dataclass
from enum import Enum
from typing import TYPE_CHECKING

import structlog

_log = structlog.get_logger(__name__)


def _dbg_enabled() -> bool:
    """Cheap gate for hot-path debug logs (see ``select_tool._dbg_enabled``)."""
    import logging

    return logging.getLogger(__name__).isEnabledFor(logging.DEBUG)


from protoboard.domain.components.package.pin_mapping import (  # noqa: E402
    meta_pin_for_package_pin,
)
from protoboard.domain.layout.placement import (  # noqa: E402
    ComponentPlacement,
    PlacementMap,
    component_bbox,
    pin_world_position,
)
from protoboard.geometry import Point  # noqa: E402

if TYPE_CHECKING:
    from protoboard.domain.board.pad_state import PadIndex
    from protoboard.domain.components.definition import ComponentDefinition
    from protoboard.domain.components.footprints import Footprint
    from protoboard.domain.components.instance import ComponentInstance
    from protoboard.domain.components.package.shapes import Package
    from protoboard.domain.layout.wire import Wire
    from protoboard.domain.schematic.schematic import Schematic


def footprint_of(
    defn: ComponentDefinition,
    inst: ComponentInstance | None,
) -> Footprint:
    """The variant-aware footprint of a placed component.

    Delegates to :meth:`ComponentInstance.resolved_footprint`. It does NOT
    fall back to the definition default for a missing instance: a placed component
    in a conflict/geometry path must have a bound instance, so ``None`` raises
    :class:`~protoboard.errors.UnboundInstanceError` (surfacing the bug instead
    of silently using the wrong default body). ``defn`` is kept in the
    signature only for the error message and source-compatibility.
    """
    if inst is None:
        from protoboard.errors import UnboundInstanceError

        raise UnboundInstanceError(
            f"footprint_of called for {defn.name!r} with no instance; a placed "
            "component's geometry must come from its bound instance"
        )
    return inst.resolved_footprint()


def package_of(
    defn: ComponentDefinition,
    inst: ComponentInstance | None,
) -> Package | None:
    """The variant-aware package (body) of a placed component, or ``None``.

    The body-side twin of :func:`footprint_of`: delegates to
    :meth:`ComponentInstance.resolved_package` and does NOT fall back to
    ``defn.package`` for a missing instance — reading the definition's default
    body for a placed part is the exact silent-wrong-body defect (a resistor
    placed as SMD-0805 colliding via its phantom axial body). ``None`` from a
    bound instance means the part genuinely declares no package.
    """
    if inst is None:
        from protoboard.errors import UnboundInstanceError

        raise UnboundInstanceError(
            f"package_of called for {defn.name!r} with no instance; a placed "
            "component's body must come from its bound instance"
        )
    return inst.resolved_package()


def resolved_footprint_or_default(
    defn: ComponentDefinition,
    inst: ComponentInstance | None,
) -> Footprint:
    """The variant-aware footprint of a placed component, tolerating unbound.

    A bound instance returns its resolved variant footprint. Geometry cannot be
    recovered from a definition alone because definitions contain references,
    not packages.
    """
    if inst is None or inst.definition is None:
        from protoboard.errors import UnboundInstanceError

        raise UnboundInstanceError(
            f"component {defn.name!r} is not bound to package families"
        )
    return inst.resolved_footprint()


class ConflictKind(Enum):
    SAME_NET = "same_net"  # auto-allowed: shared intentionally
    DIFFERENT_NETS = "diff_nets"  # always blocked
    ONE_UNCONNECTED = "one_unconnected"  # needs user decision
    BOTH_UNCONNECTED = "both_unconnected"  # needs user decision
    # Wire-conflict kinds (pin lands on a wire point):
    PIN_ON_WIRE_UNCONNECTED = "pin_on_wire_unconnected"  # wire no net -> auto-create
    PIN_ON_WIRE_WITH_NET = "pin_on_wire_with_net"  # wire has net -> auto-join
    PIN_ON_WIRE_SAME_NET = "pin_on_wire_same_net"  # same net -> auto-ok
    PIN_ON_WIRE_DIFFERENT_NET = "pin_on_wire_different_net"  # blocked


@dataclass(frozen=True)
class PadConflict:
    """One conflicting pin of the incoming component with an existing pin or wire."""

    # The incoming component's pin number.
    incoming_pin: int
    # The existing component and its pin (uuid.UUID(int=0) / 0 for wire conflicts).
    existing_comp_id: uuid.UUID
    existing_pin: int
    # Shared world position.
    position: Point
    # Classification.
    kind: ConflictKind
    # The net of the existing pin/wire point (None if unconnected).
    existing_net_id: uuid.UUID | None
    existing_net_name: str | None
    # The net of the incoming pin (None if not yet known — always None at placement).
    incoming_net_id: uuid.UUID | None
    incoming_net_name: str | None
    # Set for wire conflicts — identifies which Wire caused the conflict.
    wire_id: uuid.UUID | None = None


def find_conflicts(
    incoming_placement: ComponentPlacement,
    incoming_defn: ComponentDefinition,
    incoming_instance_id: uuid.UUID,
    layout_placements: PlacementMap,
    instances: dict[uuid.UUID, ComponentInstance],
    components: dict[str, ComponentDefinition],
    schematic: Schematic,
    pad_index: PadIndex | None = None,
    incoming_instance: ComponentInstance | None = None,
) -> list[PadConflict]:
    """Return all pad-on-pad conflicts for *incoming_placement*.

    Ignores pins already listed in *incoming_placement.shared_pins*
    (they were previously resolved).

    When *pad_index* is provided the existing-pin lookup is delegated to
    it (single source of truth).  When None, legacy path rebuilds the
    pin position map from the supplied layout/instances dictionaries.
    """
    incoming_inst = incoming_instance or instances.get(incoming_instance_id)
    incoming_fp = footprint_of(incoming_defn, incoming_inst)
    n_pins = incoming_fp.pin_count()
    existing: dict[tuple[float, float], tuple[uuid.UUID, int]]
    if pad_index is None:
        # Pre-build: world pos -> (comp_id, pin_num) for all existing placements.
        existing = {}
        for comp_id, pl in layout_placements.items():
            if comp_id == incoming_instance_id:
                continue
            inst = instances.get(comp_id)
            if inst is None:
                continue
            defn = components.get(inst.definition_ref)
            if defn is None:
                continue
            fp = footprint_of(defn, inst)
            for pin_num in range(1, fp.pin_count() + 1):
                p = pin_world_position(fp, pl, pin_num)
                key = (_round(p.x), _round(p.y))
                existing[key] = (comp_id, pin_num)
    else:
        existing = {}

    # Build pin->net map from schematic.
    pin_net: dict[tuple[uuid.UUID, int], tuple[uuid.UUID, str]] = {}
    for net in schematic.nets.values():
        for ref in net.pins:
            pin_net[(ref.component_id, ref.pin_number)] = (net.id, net.name)

    def net_for_package_pin(
        component_id: uuid.UUID,
        package_pin_number: int,
    ) -> tuple[uuid.UUID, str] | None:
        instance = instances.get(component_id)
        if instance is None:
            return None
        definition = components.get(instance.definition_ref)
        if definition is None:
            return None
        meta_pin_index = meta_pin_for_package_pin(
            definition, instance, package_pin_number
        )
        if meta_pin_index is None:
            return None
        return pin_net.get((component_id, meta_pin_index))

    conflicts: list[PadConflict] = []
    for pin_num in range(1, n_pins + 1):
        if pin_num in incoming_placement.shared_pins:
            continue
        p = pin_world_position(incoming_fp, incoming_placement, pin_num)
        key = (_round(p.x), _round(p.y))
        hit: tuple[uuid.UUID, int] | None
        if pad_index is None:
            hit = existing.get(key)
        else:
            hit = _pin_hit_via_pad_index(pad_index, p, incoming_instance_id)
        if hit is None:
            continue
        ex_comp_id, ex_pin_num = hit

        # Net of existing pin.
        ex_net = net_for_package_pin(ex_comp_id, ex_pin_num)
        ex_net_id = ex_net[0] if ex_net else None
        ex_net_name = ex_net[1] if ex_net else None

        # Net of incoming pin (by instance id, if it's being moved).
        incoming_meta_pin = meta_pin_for_package_pin(
            incoming_defn, incoming_inst, pin_num
        )
        in_net = (
            None
            if incoming_meta_pin is None
            else pin_net.get((incoming_instance_id, incoming_meta_pin))
        )
        in_net_id = in_net[0] if in_net else None
        in_net_name = in_net[1] if in_net else None

        # Classify.
        if ex_net_id is not None and in_net_id is not None:
            if ex_net_id == in_net_id:
                kind = ConflictKind.SAME_NET
            else:
                kind = ConflictKind.DIFFERENT_NETS
        elif ex_net_id is None and in_net_id is None:
            kind = ConflictKind.BOTH_UNCONNECTED
        else:
            kind = ConflictKind.ONE_UNCONNECTED

        conflicts.append(
            PadConflict(
                incoming_pin=pin_num,
                existing_comp_id=ex_comp_id,
                existing_pin=ex_pin_num,
                position=p,
                kind=kind,
                existing_net_id=ex_net_id,
                existing_net_name=ex_net_name,
                incoming_net_id=in_net_id,
                incoming_net_name=in_net_name,
            )
        )

    return conflicts


def find_wire_conflicts(
    incoming_placement: ComponentPlacement,
    incoming_defn: ComponentDefinition,
    incoming_instance_id: uuid.UUID,
    wires: list[Wire],
    schematic: Schematic,
    point_to_net: dict[tuple[float, float], tuple[uuid.UUID, str]],
    already_conflicted: set[tuple[float, float]],
    pad_index: PadIndex | None = None,
    incoming_instance: ComponentInstance | None = None,
) -> list[PadConflict]:
    """Return wire conflicts for *incoming_placement*.

    A wire conflict occurs when a pin of the incoming component lands on a
    point that is part of an existing Wire's path.

    *point_to_net* maps rounded (x, y) of any placed pin that has a net to
    (net_id, net_name).  A wire is considered to carry a net when any point
    in its path appears in point_to_net (i.e. another pin connected to that
    wire already has a net).

    *already_conflicted* positions are skipped (already reported as pad conflicts).

    When *pad_index* is provided, wire/net lookups consult it instead of
    rebuilding from *wires* and *point_to_net*.

    Note for drag-pipeline callers: prefer
    :class:`protoboard.services.drag_detector.DragDetector` over calling
    this function directly. The detector wraps this helper plus
    :func:`find_conflicts` and :func:`find_wires_crossing_pads` behind
    a single strategy-aware entry point. This function is kept as a
    pure helper for the detector and for the dialog-driven
    ``check_and_resolve`` path in ``placement_conflict.py``.
    """
    # Net name lookup — even with pad_index we still need the net's name
    # (PadIndex only carries net IDs).
    net_name_by_id: dict[uuid.UUID, str] = {
        net.id: net.name for net in schematic.nets.values()
    }
    wire_points: dict[tuple[float, float], uuid.UUID] = {}
    wire_net_map: dict[uuid.UUID, tuple[uuid.UUID, str] | None] = {}
    if pad_index is None:
        # Build wire points: rounded (x, y) -> wire_id.
        for wire in wires:
            for pt in wire.path:
                wire_points[(_round(pt.x), _round(pt.y))] = wire.id

        # Determine the net carried by each wire (first netted point wins).
        for wire in wires:
            found: tuple[uuid.UUID, str] | None = None
            for pt in wire.path:
                info = point_to_net.get((_round(pt.x), _round(pt.y)))
                if info is not None:
                    found = info
                    break
            wire_net_map[wire.id] = found

    # Net of incoming component's pins (for move conflicts where pin has a net).
    pin_net_incoming: dict[int, tuple[uuid.UUID, str]] = {}
    for net in schematic.nets.values():
        for ref in net.pins:
            if ref.component_id == incoming_instance_id:
                pin_net_incoming[ref.pin_number] = (net.id, net.name)

    conflicts: list[PadConflict] = []
    incoming_fp = footprint_of(incoming_defn, incoming_instance)
    n_pins = incoming_fp.pin_count()
    _sentinel = uuid.UUID(int=0)

    for pin_num in range(1, n_pins + 1):
        p = pin_world_position(incoming_fp, incoming_placement, pin_num)
        key = (_round(p.x), _round(p.y))
        if key in already_conflicted:
            continue
        wire_id: uuid.UUID | None
        wire_net_info: tuple[uuid.UUID, str] | None
        if pad_index is None:
            wire_id = wire_points.get(key)
            if wire_id is None:
                continue
            wire_net_info = wire_net_map.get(wire_id)
        else:
            hit = _wire_hit_via_pad_index(
                pad_index, p, incoming_placement.side, net_name_by_id
            )
            if hit is None:
                continue
            wire_id, wire_net_info = hit

        meta_pin_index = meta_pin_for_package_pin(
            incoming_defn, incoming_instance, pin_num
        )
        in_net = (
            None if meta_pin_index is None else pin_net_incoming.get(meta_pin_index)
        )

        if wire_net_info is not None and in_net is not None:
            if wire_net_info[0] == in_net[0]:
                kind = ConflictKind.PIN_ON_WIRE_SAME_NET
            else:
                kind = ConflictKind.PIN_ON_WIRE_DIFFERENT_NET
        elif wire_net_info is not None:
            # Wire carries a net, incoming pin does not.
            kind = ConflictKind.PIN_ON_WIRE_WITH_NET
        elif in_net is not None:
            # Incoming pin has a net, wire does not -> wire is the unconnected side.
            # Treat as WITH_NET using incoming's net as "existing" so
            # _update_schematic adds the pin to that net (it already belongs to it).
            kind = ConflictKind.PIN_ON_WIRE_WITH_NET
            wire_net_info = in_net
            in_net = None
        else:
            kind = ConflictKind.PIN_ON_WIRE_UNCONNECTED

        conflicts.append(
            PadConflict(
                incoming_pin=pin_num,
                existing_comp_id=_sentinel,
                existing_pin=0,
                position=p,
                kind=kind,
                existing_net_id=wire_net_info[0] if wire_net_info else None,
                existing_net_name=wire_net_info[1] if wire_net_info else None,
                incoming_net_id=in_net[0] if in_net else None,
                incoming_net_name=in_net[1] if in_net else None,
                wire_id=wire_id,
            )
        )

    return conflicts


# Module-level alias keeping the legacy ``_round`` callsites within
# this file working — single implementation now lives in geometry.py.
from protoboard.geometry import round_coord as _round  # noqa: E402


def _wire_hit_via_pad_index(
    pad_index: PadIndex,
    position: Point,
    side: str,
    net_name_by_id: dict[uuid.UUID, str],
) -> tuple[uuid.UUID, tuple[uuid.UUID, str] | None] | None:
    """Find a wire whose path touches *position* on *side*.

    Returns (wire_id, wire_net_info) where wire_net_info is
    (net_id, net_name) when the wire carries a known net, else None.
    Returns None when no wire item sits on this pad on this side.

    "Touches" covers both path-vertex items (BareWireItem,
    InsulatedWireItem) and *mid-segment crossings*
    (BareWireCrossing, InsulatedWireCrossing).  The crossing case
    matters for drag-conflict detection: when a user drops a
    component pin onto the middle of an existing bare wire's segment
    (no vertex coincides with the pad — the wire just passes over),
    the pad still hosts a BareWireCrossing item and a same-pad pin
    of a foreign net would short it.  The pre-2026-05-31 implementation
    matched only vertex items and silently missed every such case.
    """
    from protoboard.domain.board.pad_state import (
        SHORT_CIRCUIT,
        BareWireCrossing,
        BareWireItem,
        InsulatedWireCrossing,
        InsulatedWireItem,
    )

    pad = pad_index.pad_at(position)
    if pad is None:
        _log.debug(
            "wire_hit.no_pad",
            position=(position.x, position.y),
            side=side,
        )
        return None
    # Pin penetrates the board (rule 5) — a foreign wire on EITHER side
    # of the pad creates a conflict with the incoming pin. Scan both
    # physical layers; prefer a hit on the pin's own side but fall back
    # to the opposite side. Without this, dropping a top-side pin onto
    # a bottom-side foreign wire produces no conflict.
    own_side: str = "top" if side == "top" else "bottom"
    opposite: str = "bottom" if own_side == "top" else "top"
    wire_item = None
    hit_side: str = own_side
    items_own = pad.physically_blocked_on(own_side)  # type: ignore[arg-type]
    items_opp = pad.physically_blocked_on(opposite)  # type: ignore[arg-type]
    for it in items_own:
        if isinstance(
            it,
            (
                BareWireItem,
                BareWireCrossing,
                InsulatedWireItem,
                InsulatedWireCrossing,
            ),
        ):
            wire_item = it
            hit_side = own_side
            break
    if wire_item is None:
        for it in items_opp:
            if isinstance(
                it,
                (
                    BareWireItem,
                    BareWireCrossing,
                    InsulatedWireItem,
                    InsulatedWireCrossing,
                ),
            ):
                wire_item = it
                hit_side = opposite
                break
    if _dbg_enabled():
        _log.debug(
            "wire_hit.lookup",
            position=(position.x, position.y),
            side=side,
            items_own=[type(it).__name__ for it in items_own],
            items_opp=[type(it).__name__ for it in items_opp],
            wire_item=type(wire_item).__name__ if wire_item else None,
            hit_side=hit_side if wire_item else None,
        )
    if wire_item is None:
        return None
    net_id = pad.electrical_net_on(hit_side)  # type: ignore[arg-type]
    if net_id is None or net_id is SHORT_CIRCUIT:
        return wire_item.id, None
    name = net_name_by_id.get(net_id)  # type: ignore[arg-type]
    if name is None:
        return wire_item.id, None
    return wire_item.id, (net_id, name)  # type: ignore[return-value]


def _pin_hit_via_pad_index(
    pad_index: PadIndex,
    position: Point,
    exclude_comp_id: uuid.UUID,
) -> tuple[uuid.UUID, int] | None:
    """Return (other_comp_id, pin_number) of a PinItem on *position*
    belonging to a component other than *exclude_comp_id*, else None.

    If multiple alien PinItems sit on the same pad (multi-shared), an
    arbitrary one wins — matches legacy `existing[key] = ...` dict
    semantics where the last-written iteration order decided.
    """
    from protoboard.domain.board.pad_state import PinItem

    pad = pad_index.pad_at(position)
    if pad is None:
        return None
    for it in pad.iter_items():
        if isinstance(it, PinItem) and it.id != exclude_comp_id:
            return it.id, it.pin_number
    return None


# ---------------------------------------------------------------------------
# Wire-segment crossing detection
# ---------------------------------------------------------------------------

_CROSS_EPS = 0.5  # half-raster tolerance — pad radius in raster units


def _seg_point_dist(
    px: float, py: float, ax: float, ay: float, bx: float, by: float
) -> float:
    """Shortest distance from point (px, py) to segment (ax, ay)→(bx, by)."""
    dx, dy = bx - ax, by - ay
    if dx == 0.0 and dy == 0.0:
        return math.hypot(px - ax, py - ay)
    t = max(0.0, min(1.0, ((px - ax) * dx + (py - ay) * dy) / (dx * dx + dy * dy)))
    return math.hypot(px - ax - t * dx, py - ay - t * dy)


def find_wires_crossing_pads(
    wires: list[Wire],
    moved_comp_ids: set[uuid.UUID],
    layout_placements: PlacementMap,
    instances: dict[uuid.UUID, ComponentInstance],
    components: dict[str, ComponentDefinition],
    schematic: Schematic,
    moved_wire_ids: set[uuid.UUID] | None = None,
    pad_index: PadIndex | None = None,
    ignored_pad_keys: set[tuple[float, float]] | None = None,
) -> list[uuid.UUID]:
    """Return IDs of wires whose path passes within _CROSS_EPS of an alien pad.

    Checks every *line segment* of wire.path — not only the vertices.
    A wire routed diagonally from (1,1) to (3,3) that passes within
    half-raster of a pad at (2, 2.1) is considered conflicting even though
    no vertex coincides with that pad.

    "Alien pad" means a pad belonging to a net different from the wire's net,
    or a pad with a net when the wire is unconnected (and vice-versa).
    Pads belonging to `moved_comp_ids` are the new (post-move) positions.

    `moved_wire_ids`: wire IDs that were translated together with the moved
    components.  These are skipped — they are internal to the moved group and
    their new positions are already consistent with the new pad positions.

    `ignored_pad_keys`: rounded ``(x, y)`` pad positions exempt from the
    alien-pad check (they still participate in wire-net inference).  RouteTool
    passes its candidate route's two terminal endpoints — intended contacts
    whose net implications are resolved later by ``resolve_wire_nets``.

    Returns a list of wire IDs that should be removed.
    """
    _moved_wires: set[uuid.UUID] = moved_wire_ids or set()

    # Build pad_pos → net_id | None for ALL pins (moved comps already updated
    # in layout_placements before this function is called).  When pad_index
    # is provided, the same map is derived from it (PinItem.net) to skip
    # the layout/instances walk entirely.
    pad_net: dict[tuple[float, float], uuid.UUID | None] = {}
    if pad_index is None:
        pin_net_map: dict[tuple[uuid.UUID, int], uuid.UUID] = {}
        for net in schematic.nets.values():
            for ref in net.pins:
                pin_net_map[(ref.component_id, ref.pin_number)] = net.id
        for comp_id, pl in layout_placements.items():
            inst = instances.get(comp_id)
            if inst is None:
                continue
            defn = components.get(inst.definition_ref)
            if defn is None:
                continue
            fp = footprint_of(defn, inst)
            for pin_num in range(1, fp.pin_count() + 1):
                p = pin_world_position(fp, pl, pin_num)
                meta_pin_index = meta_pin_for_package_pin(defn, inst, pin_num)
                net_id = (
                    None
                    if meta_pin_index is None
                    else pin_net_map.get((comp_id, meta_pin_index))
                )
                pad_net[(_round(p.x), _round(p.y))] = net_id
    else:
        from protoboard.domain.board.pad_state import PinItem

        for pad in pad_index.iter_pads():
            pin_item: PinItem | None = None
            for it in pad.iter_items():
                if isinstance(it, PinItem):
                    pin_item = it
                    break
            if pin_item is None:
                continue
            key = (_round(pad.position.x), _round(pad.position.y))
            pad_net[key] = pin_item.net

    if not pad_net:
        return []

    # Build a spatial grid for fast candidate lookup.
    # Key = (int(floor(px)), int(floor(py))), value = list of (px, py, net_id).
    # For a segment (ax,ay)→(bx,by) we only check grid cells that overlap the
    # segment's bounding box expanded by _CROSS_EPS (= 0.5 raster).  This
    # reduces the inner loop from O(all_pads) to O(pads_near_segment) — typically
    # 0–4 pads per cell × a handful of cells vs. ~1100 pads for every segment.
    from collections import defaultdict

    pad_grid: dict[tuple[int, int], list[tuple[float, float, uuid.UUID | None]]] = (
        defaultdict(list)
    )
    for (kx, ky), net_id in pad_net.items():
        px_f, py_f = float(kx), float(ky)
        pad_grid[(int(math.floor(px_f)), int(math.floor(py_f)))].append(
            (px_f, py_f, net_id)
        )

    def _pads_near_segment(
        ax: float, ay: float, bx: float, by: float
    ) -> list[tuple[float, float, uuid.UUID | None]]:
        x0 = int(math.floor(min(ax, bx) - _CROSS_EPS))
        x1 = int(math.floor(max(ax, bx) + _CROSS_EPS))
        y0 = int(math.floor(min(ay, by) - _CROSS_EPS))
        y1 = int(math.floor(max(ay, by) + _CROSS_EPS))
        result: list[tuple[float, float, uuid.UUID | None]] = []
        for cx in range(x0, x1 + 1):
            for cy in range(y0, y1 + 1):
                result.extend(pad_grid.get((cx, cy), []))
        return result

    # Build wire → net map: first pad in wire.path that has a net wins.
    wire_net: dict[uuid.UUID, uuid.UUID | None] = {}
    for wire in wires:
        found: uuid.UUID | None = None
        for pt in wire.path:
            info = pad_net.get((_round(pt.x), _round(pt.y)))
            if info is not None:
                found = info
                break
        wire_net[wire.id] = found

    conflicting: list[uuid.UUID] = []
    for wire in wires:
        if wire.id in _moved_wires:
            continue  # translated with the group — consistent with new pad positions
        w_net = wire_net.get(wire.id)
        path = wire.path
        if len(path) < 2:
            continue
        bad = False
        for i in range(len(path) - 1):
            ax, ay = path[i].x, path[i].y
            bx, by = path[i + 1].x, path[i + 1].y
            # For insulated wires only the two endpoints make physical contact
            # (where the wire is soldered to a pad). The body of the wire
            # floats above the board and cannot short-circuit a pad it passes
            # over — but it CAN conflict if an endpoint lands on an alien pad.
            # We therefore only check pads near the endpoints, not mid-segments.
            is_endpoint_seg = wire.insulated and (i == 0 or i == len(path) - 2)
            if wire.insulated and not is_endpoint_seg:
                continue
            for px, py, p_net in _pads_near_segment(ax, ay, bx, by):
                if ignored_pad_keys and (px, py) in ignored_pad_keys:
                    continue
                if _seg_point_dist(px, py, ax, ay, bx, by) < _CROSS_EPS:
                    # Same net on both sides → OK (intentional contact).
                    if w_net is not None and p_net is not None and w_net == p_net:
                        continue
                    # Both unconnected → no conflict.
                    if w_net is None and p_net is None:
                        continue
                    # Alien pad: bare wire or insulated wire endpoint on alien pad.
                    bad = True
                    break
            if bad:
                break
        if bad:
            conflicting.append(wire.id)

    return conflicting


# ---------------------------------------------------------------------------
# Group-move conflict classification
# ---------------------------------------------------------------------------


class MoveConflictSeverity(Enum):
    CLEAR = "clear"  # no conflict
    REMOVABLE = "removable"  # only wire conflicts removable without asking
    BLOCKED = "blocked"  # hard block (pad-pad different-net or subgroup wire)


@dataclass(frozen=True)
class MoveConflictResult:
    severity: MoveConflictSeverity
    blocking_pad_positions: list[Point]  # positions of hard-blocked pads (for overlay)
    removable_wire_ids: list[uuid.UUID]  # wire IDs safe to remove


def check_group_move_conflicts(
    moved_ids: set[uuid.UUID],
    active_group_id: uuid.UUID,
    layout_placements: PlacementMap,
    layout_wires: list[Wire],
    layout_groups: dict[uuid.UUID, object],
    instances: dict[uuid.UUID, ComponentInstance],
    components: dict[str, ComponentDefinition],
    schematic: Schematic,
    moved_wire_ids: set[uuid.UUID] | None = None,
    pad_index: PadIndex | None = None,
    extra_protected_group_ids: set[uuid.UUID] | None = None,
) -> MoveConflictResult:
    """Classify conflicts that arise when *moved_ids* are at their current (post-drag) positions.

    Returns a MoveConflictResult describing:
    - CLEAR: move is fine as-is.
    - REMOVABLE: only wires belonging to the active group or its ancestors conflict;
      those wire IDs are listed in removable_wire_ids.
    - BLOCKED: a hard pad-pad conflict exists, or a wire belonging to a subgroup
      of the active group conflicts (user chose "block" for that case).

    *pad_index*: caller may pass a PadIndex that already reflects the
    post-move state (e.g. via ``PadIndex.clone()`` + simulated moves).
    When provided, it is forwarded to the underlying find_conflicts /
    find_wires_crossing_pads calls.  When None — the default — the
    legacy from-layout path is used, which already operates on the
    post-move *layout_placements* dict the caller passed in.

    *extra_protected_group_ids* (Phase 3): additional group IDs whose
    sub-groups should be treated as protected monoliths (their internal
    wires are HARD-blocking just like the *active_group_id*'s subgroups
    would be).  Used by :func:`check_multi_group_move_conflicts` for
    drag sets that move several groups atomically — each group in the
    drag set contributes its own subgroup-bbox set so an intra-drag-set
    wire crossing still hard-blocks when it cuts through a moving
    subgroup's interior.  ``None`` preserves single-group semantics.
    """  # noqa: E501
    from protoboard.domain.layout.group import ComponentGroup, all_component_ids

    blocking_pads: list[Point] = []
    removable_ids: list[uuid.UUID] = []

    _log.debug(
        "group_move.check.start",
        moved_ids=[str(i) for i in moved_ids],
        active_group_id=str(active_group_id),
    )

    # --- 1. Pad-on-pad AND bbox conflicts for every moved component ---
    for comp_id in moved_ids:
        pl = layout_placements.get(comp_id)
        inst = instances.get(comp_id)
        if pl is None or inst is None:
            continue
        defn = components.get(inst.definition_ref)
        if defn is None:
            continue
        fp = footprint_of(defn, inst)
        # Bbox overlap with external (non-moved) components — same logic as
        # _move_blocked() in component_cmds.py.
        new_bbox = component_bbox(fp, pl, defn.component_class)
        moved_pin_positions: set[tuple[float, float]] = set()
        for pn in range(1, fp.pin_count() + 1):
            p2 = pin_world_position(fp, pl, pn)
            moved_pin_positions.add((_round(p2.x), _round(p2.y)))

        for other_id, other_pl in layout_placements.items():
            if other_id in moved_ids:
                continue  # skip fellow group members
            other_inst = instances.get(other_id)
            if other_inst is None:
                continue
            other_defn = components.get(other_inst.definition_ref)
            if other_defn is None:
                continue
            other_fp = footprint_of(other_defn, other_inst)
            other_bbox = component_bbox(other_fp, other_pl, other_defn.component_class)
            if new_bbox.intersects(other_bbox):
                other_pins: set[tuple[float, float]] = set()
                for pn in range(1, other_fp.pin_count() + 1):
                    p2 = pin_world_position(other_fp, other_pl, pn)
                    other_pins.add((_round(p2.x), _round(p2.y)))
                if moved_pin_positions.isdisjoint(other_pins):
                    # Pure body overlap — blocking (mirrors _move_blocked behaviour).
                    _log.debug(
                        "group_move.bbox_conflict",
                        comp_id=str(comp_id),
                        other_id=str(other_id),
                        bbox=str(new_bbox),
                        other_bbox=str(other_bbox),
                    )
                    # Use any pin of the moved component as blocking position indicator.
                    first_pin = pin_world_position(fp, pl, 1)
                    blocking_pads.append(first_pin)

        conflicts = find_conflicts(
            incoming_placement=pl,
            incoming_defn=defn,
            incoming_instance_id=comp_id,
            incoming_instance=inst,
            layout_placements=layout_placements,
            instances=instances,
            components=components,
            schematic=schematic,
            pad_index=pad_index,
        )
        for c in conflicts:
            # Phase 3 (spec rev 3 dec 4): intra-drag-set collisions are
            # SOFT — when two drag-set members happen to land on the
            # same pad they're "the user's stuff" moving together;
            # `find_conflicts` doesn't know about the wider moved_ids
            # set so we filter here.  Wire conflicts (existing_comp_id
            # == UUID(int=0)) are unaffected — they're handled by the
            # wire-crossing pass below.
            if (
                c.existing_comp_id != uuid.UUID(int=0)
                and c.existing_comp_id in moved_ids
            ):
                _log.debug(
                    "group_move.intra_drag_set_skip",
                    comp_id=str(comp_id)[:8],
                    other=str(c.existing_comp_id)[:8],
                    pos=(c.position.x, c.position.y),
                )
                continue
            _log.debug(
                "group_move.pad_conflict",
                comp_id=str(comp_id),
                kind=c.kind.value,
                pos=(c.position.x, c.position.y),
                in_net=c.incoming_net_name,
                ex_net=c.existing_net_name,
            )
            # Block on ANY pin overlap during group drag — same-net is already
            # excluded by find_conflicts (it only returns conflicting pins).
            if c.kind != ConflictKind.SAME_NET:
                blocking_pads.append(c.position)

    if blocking_pads:
        _log.info(
            "group_move.blocked.pad",
            blocking_positions=[(p.x, p.y) for p in blocking_pads],
        )
        return MoveConflictResult(
            severity=MoveConflictSeverity.BLOCKED,
            blocking_pad_positions=blocking_pads,
            removable_wire_ids=[],
        )

    # --- 2. Wire crossings after the move ---
    crossing_ids = find_wires_crossing_pads(
        wires=layout_wires,
        moved_comp_ids=moved_ids,
        layout_placements=layout_placements,
        instances=instances,
        components=components,
        schematic=schematic,
        moved_wire_ids=moved_wire_ids,
        pad_index=pad_index,
    )
    _log.debug(
        "group_move.wire_crossings",
        count=len(crossing_ids),
        wire_ids=[str(i) for i in crossing_ids],
    )

    # --- 2b. Skewed wires (dragged-along wires that ended up neither ortho
    # nor diagonal after the component move).  Treated identically to crossing
    # wires for severity purposes: they are REMOVABLE (auto-deleted).
    from protoboard.domain.layout.wire_utils import find_skewed_wires

    skewed_ids = find_skewed_wires(
        layout_wires, moved_wire_ids=set(moved_wire_ids or set())
    )
    if skewed_ids:
        _log.debug(
            "group_move.skewed_wires",
            count=len(skewed_ids),
            wire_ids=[str(i) for i in skewed_ids],
        )
        # Merge into crossing_ids dedupe-wise so the downstream subgroup
        # classification handles them.
        crossing_ids = list({*crossing_ids, *skewed_ids})

    if not crossing_ids:
        _log.debug("group_move.result", severity="CLEAR")
        return MoveConflictResult(
            severity=MoveConflictSeverity.CLEAR,
            blocking_pad_positions=[],
            removable_wire_ids=[],
        )

    # --- 3. Classify each crossing wire ---
    # Collect subgroup component IDs (direct child groups of active group +
    # any extra protected groups passed by the multi-group caller).
    protected_parent_gids: set[uuid.UUID] = {active_group_id}
    if extra_protected_group_ids:
        protected_parent_gids |= extra_protected_group_ids

    subgroup_comp_ids: set[uuid.UUID] = set()
    subgroup_bboxes: list[tuple[float, float, float, float]] = []
    for parent_gid in protected_parent_gids:
        parent_group = layout_groups.get(parent_gid)
        if not isinstance(parent_group, ComponentGroup):
            continue
        for child_gid in parent_group.child_group_ids:
            child_ids = all_component_ids(
                layout_groups,  # type: ignore[arg-type]
                child_gid,
            )
            subgroup_comp_ids |= set(child_ids)
            bbox = _compute_bbox(child_ids, layout_placements, instances, components)
            if bbox is not None:
                subgroup_bboxes.append(bbox)
    _log.debug(
        "group_move.subgroup_bboxes",
        count=len(subgroup_bboxes),
        bboxes=subgroup_bboxes,
        protected_parents=[str(g)[:8] for g in protected_parent_gids],
    )

    for wire_id in crossing_ids:
        wire = next((w for w in layout_wires if w.id == wire_id), None)
        if wire is None:
            continue
        # Is this wire internal to any subgroup?
        is_subgroup_wire = any(
            _wire_is_internal_bbox(wire, bbox) for bbox in subgroup_bboxes
        )
        _log.debug(
            "group_move.wire_classify",
            wire_id=str(wire_id),
            is_subgroup_wire=is_subgroup_wire,
            path=[(p.x, p.y) for p in wire.path],
        )
        if is_subgroup_wire:
            _log.info("group_move.blocked.subgroup_wire", wire_id=str(wire_id))
            # Subgroup wire crossing → hard block
            return MoveConflictResult(
                severity=MoveConflictSeverity.BLOCKED,
                blocking_pad_positions=[],
                removable_wire_ids=[],
            )
        removable_ids.append(wire_id)

    if removable_ids:
        _log.info(
            "group_move.result",
            severity="REMOVABLE",
            removable_wire_ids=[str(i) for i in removable_ids],
        )
        return MoveConflictResult(
            severity=MoveConflictSeverity.REMOVABLE,
            blocking_pad_positions=[],
            removable_wire_ids=removable_ids,
        )
    _log.debug("group_move.result", severity="CLEAR")
    return MoveConflictResult(
        severity=MoveConflictSeverity.CLEAR,
        blocking_pad_positions=[],
        removable_wire_ids=[],
    )


def check_multi_group_move_conflicts(
    drag_set: object,  # protoboard.controller.editing.drag_set.DragSet
    active_group_id: uuid.UUID,
    layout_placements: PlacementMap,
    layout_wires: list[Wire],
    layout_groups: dict[uuid.UUID, object],
    instances: dict[uuid.UUID, ComponentInstance],
    components: dict[str, ComponentDefinition],
    schematic: Schematic,
    moved_wire_ids: set[uuid.UUID] | None = None,
    pad_index: PadIndex | None = None,
) -> MoveConflictResult:
    """Multi-group / mixed-drag conflict classifier (Phase 3, spec rev 3).

    A *drag_set* may move several groups + free top-level components +
    explicit wires atomically by one delta.  Spec-rev-3 decision 4
    says intra-drag-set collisions are SOFT — anything inside the
    moving set is "the user's stuff" so a pad-pad overlap between
    two elements both in the drag is not a hard block.  Wires
    crossing into a moving subgroup's interior remain HARD-blocked
    (subgroup is still a sealed monolith).

    Implementation: delegates to :func:`check_group_move_conflicts`
    with ``moved_ids = drag_set.all_component_ids(layout_groups)`` and
    ``extra_protected_group_ids = drag_set.groups - {active_group_id}``.
    The inner function already skips fellow-moved components in its
    pad-pad pass (line ``if other_id in moved_ids: continue``), so
    SOFT-intra is automatic once every drag-set member appears in
    ``moved_ids``.

    Args:
        drag_set: :class:`protoboard.controller.editing.drag_set.DragSet` — the
            structured snapshot built at press time.  Typed as
            ``object`` here to keep the domain module Qt-free.
        active_group_id: The user's current group context (root or a
            sub-group entered via long-press 1 s).  Its child groups
            stay protected.
        Other args: same as :func:`check_group_move_conflicts`.
    """
    # Resolve flat moved_ids from the DragSet.
    moved_ids: set[uuid.UUID] = set(
        drag_set.all_component_ids(layout_groups)  # type: ignore[attr-defined]
    )

    extra_groups: set[uuid.UUID] = set(drag_set.groups) - {
        active_group_id
    }  # type: ignore[attr-defined]

    _log.debug(
        "multi_group_move.check.start",
        n_moved=len(moved_ids),
        active_group_id=str(active_group_id)[:8],
        drag_set_groups=[str(g)[:8] for g in drag_set.groups],  # type: ignore[attr-defined]
        extra_groups=[str(g)[:8] for g in extra_groups],
    )

    return check_group_move_conflicts(
        moved_ids=moved_ids,
        active_group_id=active_group_id,
        layout_placements=layout_placements,
        layout_wires=layout_wires,
        layout_groups=layout_groups,
        instances=instances,
        components=components,
        schematic=schematic,
        moved_wire_ids=moved_wire_ids,
        pad_index=pad_index,
        extra_protected_group_ids=extra_groups if extra_groups else None,
    )


def _compute_bbox(
    comp_ids: frozenset[uuid.UUID],
    placements: PlacementMap,
    instances: dict[uuid.UUID, ComponentInstance],
    components: dict[str, ComponentDefinition],
) -> tuple[float, float, float, float] | None:
    xs: list[float] = []
    ys: list[float] = []
    for cid in comp_ids:
        pl = placements.get(cid)
        inst = instances.get(cid)
        if pl is None or inst is None:
            continue
        defn = components.get(inst.definition_ref)
        if defn is None:
            continue
        fp = footprint_of(defn, inst)
        for n in range(1, fp.pin_count() + 1):
            p = pin_world_position(fp, pl, n)
            xs.append(p.x)
            ys.append(p.y)
    if not xs:
        return None
    return min(xs), min(ys), max(xs), max(ys)


def _wire_is_internal_bbox(
    wire: Wire,
    bbox: tuple[float, float, float, float],
    tol: float = 0.5,
) -> bool:
    x0, y0, x1, y1 = bbox
    return len(wire.path) >= 2 and all(
        x0 - tol <= p.x <= x1 + tol and y0 - tol <= p.y <= y1 + tol for p in wire.path
    )


# ---------------------------------------------------------------------------
# Cohesive scanner — context-object view over one layout
# ---------------------------------------------------------------------------


@dataclass(frozen=True)
class ConflictScanner:
    """Cohesive view for pad/wire-conflict detection over one layout context.

    Collapses the 7–11-parameter sprawl of :func:`find_conflicts`,
    :func:`find_wire_conflicts`, :func:`find_wires_crossing_pads`,
    :func:`check_group_move_conflicts` and
    :func:`check_multi_group_move_conflicts` — every one of which re-passes the
    same ``placements / instances / components / schematic / pad_index``
    bundle — into fields gathered once. The per-call variables
    (``incoming_placement``, ``moved_ids``, ``active_group_id`` …) stay as
    method arguments because they genuinely vary per invocation.

    The methods are thin organisers that delegate to the module's free
    functions — those stay the canonical implementation (the 240-line
    spec-rev-3 drag logic is *not* rewritten here, only wrapped). Build via
    :meth:`from_session` or directly with the context dicts. The wrapped
    functions accept an optional ``pad_index``; when this scanner's
    ``pad_index`` is ``None`` they take their legacy from-layout path, so a
    scanner built without one still works for test stubs.

    Mirrors the :class:`~protoboard.domain.layout.group_ownership.OwnershipResolver`
    pattern — frozen here because, unlike that resolver, no per-batch cache is
    kept (each free function builds and discards its own maps).
    """

    placements: PlacementMap
    instances: dict[uuid.UUID, ComponentInstance]
    components: dict[str, ComponentDefinition]
    schematic: Schematic
    pad_index: PadIndex | None = None

    @classmethod
    def from_session(
        cls, session: object, *, pad_index: PadIndex | None = None
    ) -> ConflictScanner:
        """Build a scanner from an ``EditorSession``-like object.

        Reads ``layout.placements``, ``instances``, ``components``,
        ``schematic``. Kept duck-typed (``object``) so the domain module
        stays Qt-free.

        ``pad_index`` is passed explicitly because conflict callers often need
        a *post-move* index (e.g. ``PadIndex.clone()`` + simulated moves) that
        differs from ``session.pad_index()``; when omitted the legacy
        from-layout paths are used.
        """
        layout = session.layout  # type: ignore[attr-defined]
        return cls(
            placements=layout.placements,
            instances=session.instances,  # type: ignore[attr-defined]
            components=session.components,  # type: ignore[attr-defined]
            schematic=session.schematic,  # type: ignore[attr-defined]
            pad_index=pad_index,
        )

    def find_pad_conflicts(
        self,
        incoming_placement: ComponentPlacement,
        incoming_defn: ComponentDefinition,
        incoming_instance_id: uuid.UUID,
    ) -> list[PadConflict]:
        """All pad-on-pad conflicts for *incoming_placement* (see
        :func:`find_conflicts`)."""
        return find_conflicts(
            incoming_placement=incoming_placement,
            incoming_defn=incoming_defn,
            incoming_instance_id=incoming_instance_id,
            layout_placements=self.placements,
            instances=self.instances,
            components=self.components,
            schematic=self.schematic,
            pad_index=self.pad_index,
        )

    def find_wire_conflicts(
        self,
        incoming_placement: ComponentPlacement,
        incoming_defn: ComponentDefinition,
        incoming_instance_id: uuid.UUID,
        wires: list[Wire],
        point_to_net: dict[tuple[float, float], tuple[uuid.UUID, str]],
        already_conflicted: set[tuple[float, float]],
    ) -> list[PadConflict]:
        """Pin-on-wire conflicts for *incoming_placement* (see
        :func:`find_wire_conflicts`)."""
        return find_wire_conflicts(
            incoming_placement=incoming_placement,
            incoming_defn=incoming_defn,
            incoming_instance_id=incoming_instance_id,
            wires=wires,
            schematic=self.schematic,
            point_to_net=point_to_net,
            already_conflicted=already_conflicted,
            pad_index=self.pad_index,
            # Resolve the incoming geometry from the bound instance — the free
            # function has no instances dict, so without this it raises on a
            # placed component (previously masked by the defn.footprint fallback).
            incoming_instance=self.instances.get(incoming_instance_id),
        )

    def find_wires_crossing_pads(
        self,
        wires: list[Wire],
        moved_comp_ids: set[uuid.UUID],
        moved_wire_ids: set[uuid.UUID] | None = None,
    ) -> list[uuid.UUID]:
        """IDs of wires whose path crosses an alien pad after the move (see
        :func:`find_wires_crossing_pads`)."""
        return find_wires_crossing_pads(
            wires=wires,
            moved_comp_ids=moved_comp_ids,
            layout_placements=self.placements,
            instances=self.instances,
            components=self.components,
            schematic=self.schematic,
            moved_wire_ids=moved_wire_ids,
            pad_index=self.pad_index,
        )

    def check_group_move(
        self,
        moved_ids: set[uuid.UUID],
        active_group_id: uuid.UUID,
        layout_wires: list[Wire],
        layout_groups: dict[uuid.UUID, object],
        moved_wire_ids: set[uuid.UUID] | None = None,
        extra_protected_group_ids: set[uuid.UUID] | None = None,
    ) -> MoveConflictResult:
        """Classify an atomic single-group move (see
        :func:`check_group_move_conflicts`)."""
        return check_group_move_conflicts(
            moved_ids=moved_ids,
            active_group_id=active_group_id,
            layout_placements=self.placements,
            layout_wires=layout_wires,
            layout_groups=layout_groups,
            instances=self.instances,
            components=self.components,
            schematic=self.schematic,
            moved_wire_ids=moved_wire_ids,
            pad_index=self.pad_index,
            extra_protected_group_ids=extra_protected_group_ids,
        )

    def check_multi_group_move(
        self,
        drag_set: object,
        active_group_id: uuid.UUID,
        layout_wires: list[Wire],
        layout_groups: dict[uuid.UUID, object],
        moved_wire_ids: set[uuid.UUID] | None = None,
    ) -> MoveConflictResult:
        """Classify an atomic multi-group / mixed drag-set move (see
        :func:`check_multi_group_move_conflicts`)."""
        return check_multi_group_move_conflicts(
            drag_set=drag_set,
            active_group_id=active_group_id,
            layout_placements=self.placements,
            layout_wires=layout_wires,
            layout_groups=layout_groups,
            instances=self.instances,
            components=self.components,
            schematic=self.schematic,
            moved_wire_ids=moved_wire_ids,
            pad_index=self.pad_index,
        )
