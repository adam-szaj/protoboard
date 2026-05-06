# SPDX-License-Identifier: MIT
"""Full-netlist autorouter using A* with rip-up & retry.

Strategy:
1. Sort nets by pin-count (smallest first — easier nets first).
2. For each unrouted connection in a net (Steiner-like spanning route):
   a. Pick the nearest already-connected pad as source.
   b. Run A* to any unconnected target pad in the same net.
   c. If the path passes through a same-net wire segment before reaching
      the target, truncate it there (same-net wire termination).
   d. If no path found, rip-up the lowest-priority previously routed net
      that overlaps the failed path region and retry.
3. Return a list of new WireSegment objects (caller adds them to layout).
4. Return a list of nets that could not be routed (unrouted report).

Multi-layer routing:
  On perfboard (plated-through holes), the router may switch layers at any
  pad.  Each layer change costs `cost_model.layer_change`.  The resulting
  path is split into per-layer WireSegment objects.
"""

from __future__ import annotations

import uuid
from dataclasses import dataclass, field
from typing import Literal

from protoboard.domain.board.board import Board
from protoboard.domain.board.boxes import PadAtom, StripAtom
from protoboard.domain.components.definition import ComponentDefinition
from protoboard.domain.components.instance import ComponentInstance
from protoboard.domain.connectivity.graph import ConnectivityGraph
from protoboard.domain.layout.layout import Layout
from protoboard.domain.layout.placement import component_bbox, pin_world_position
from protoboard.domain.layout.wire import WireSegment
from protoboard.domain.schematic.net import Net, PinRef
from protoboard.domain.schematic.schematic import Schematic
from protoboard.geometry import Point
from protoboard.services.routing.astar import PathStep, find_paths, split_path_by_side
from protoboard.services.routing.cost_model import RoutingCostModel

# Higher direction_change discourages long detours and keeps wires closer
# to straight-line paths, leaving more corridors free for other nets.
_DEFAULT_COST = RoutingCostModel(direction_change=1.0)

# Sentinel net-id used for floating pins (not in any net).
_FLOATING = uuid.UUID("00000000-0000-0000-0000-000000000002")


@dataclass
class AutorouteResult:
    """Result returned by :func:`autoroute`."""

    wires: list[WireSegment] = field(default_factory=list)
    unrouted_nets: list[str] = field(default_factory=list)  # net names


def autoroute(
    board: Board,
    layout: Layout,
    schematic: Schematic,
    components: dict[str, ComponentDefinition],
    instances: dict[uuid.UUID, ComponentInstance],
    side: Literal["top", "bottom"] = "top",
    cost_model: RoutingCostModel = _DEFAULT_COST,
    max_rip_up_passes: int = 3,
    insulated: bool = True,
    allow_layer_change: bool = True,
) -> AutorouteResult:
    """Route all unrouted nets in *schematic* and return new wire segments.

    Parameters
    ----------
    insulated:
        Whether newly created wires should be insulated.  Insulated wires may
        cross bare wires (acting as jumpers) at extra cost; bare wires cannot
        cross other bare wires (short circuit).
    allow_layer_change:
        When True (default), the router may route through the bottom layer when
        it reduces total wire length or avoids obstacles — using plated-through
        pads as vias.  Set to False to restrict to a single layer.

    Already-routed connections (where a physical path already exists) are
    skipped.  Only completely *missing* connections are routed.

    The caller is responsible for adding the returned wires to the layout
    and running DRC.
    """
    result = AutorouteResult()

    # Build pin-position lookup: PinRef → (world_position, side).
    pin_positions = _build_pin_positions(layout, components, instances)

    # Build a map: (world_position, side) → net_id for every placed pin that
    # belongs to a net, registered on BOTH layers (plated-through hole).
    point_side_to_net: dict[tuple[Point, str], uuid.UUID] = {}
    for net in schematic.nets.values():
        for ref in net.pins:
            ps = pin_positions.get(ref)
            if ps is not None:
                pt, _s = ps
                point_side_to_net[(pt, "top")] = net.id
                point_side_to_net[(pt, "bottom")] = net.id

    # Collect ALL placed pin positions — including floating pins — on BOTH layers.
    # A component pin occupies a physical hole; routing through that hole on
    # either layer creates an unintended electrical connection.  Therefore we
    # block (Point, "top") AND (Point, "bottom") for every placed pin.
    all_pin_positions: dict[tuple[Point, str], uuid.UUID] = {}
    for ps in pin_positions.values():
        pt, _side = ps
        net_id = point_side_to_net.get(ps, _FLOATING)
        all_pin_positions[(pt, "top")] = net_id
        all_pin_positions[(pt, "bottom")] = net_id

    # Collect plated-through pad positions from the board for via routing.
    plated_through_pads = _collect_plated_through_pads(board) if allow_layer_change else set()

    # Build a penalty map for pads on the top layer that lie under a component
    # body.  Routing there is legal but physically awkward; bottom is preferred.
    under_component_penalty = _build_under_component_penalty(
        board=board,
        layout=layout,
        components=components,
        instances=instances,
        penalty=cost_model.under_component_top,
    )

    # Work on a *mutable copy* of wires so we can do rip-up without touching
    # the real layout.
    working_wires: list[WireSegment] = list(layout.wires)

    # Sort nets: longest manhattan distance first.  Long nets need more board
    # space and should be planned while the board is still empty; short nets
    # have more alternative paths and are easier to squeeze in last.
    # Within the same distance, sort by descending pin count for stability.
    def _net_sort_key(net: Net) -> tuple[float, int]:
        pts = [pin_positions[r][0] for r in net.pins if r in pin_positions]
        if len(pts) < 2:
            return (0.0, -len(pts))
        max_d = max(
            abs(a.x - b.x) + abs(a.y - b.y)
            for i, a in enumerate(pts)
            for b in pts[i + 1:]
        )
        return (-max_d, -len(pts))

    sorted_nets = sorted(schematic.nets.values(), key=_net_sort_key)

    for net in sorted_nets:
        routed = _route_net(
            net=net,
            pin_positions=pin_positions,
            all_pin_positions=all_pin_positions,
            schematic=schematic,
            board=board,
            layout=layout,
            working_wires=working_wires,
            components=components,
            instances=instances,
            side=side,
            cost_model=cost_model,
            max_rip_up_passes=max_rip_up_passes,
            insulated=insulated,
            plated_through_pads=plated_through_pads,
            under_component_penalty=under_component_penalty,
        )
        if routed is None:
            result.unrouted_nets.append(net.name)
        else:
            result.wires.extend(routed)
            working_wires.extend(routed)

    return result


# ---------------------------------------------------------------------------
# Internal helpers
# ---------------------------------------------------------------------------

def _collect_plated_through_pads(board: Board) -> set[Point]:
    """Return positions of all plated-through pads on the board."""
    pts: set[Point] = set()
    for atom, pos in board.atoms():
        if isinstance(atom, PadAtom) and atom.layers.plated_through:
            pts.add(pos)
        elif isinstance(atom, StripAtom) and atom.layers.plated_through:
            # Each hole of the strip is a plated-through pad.
            n = int(atom.length) if isinstance(atom.length, int) else 1
            if atom.orientation == "horizontal":
                start_x = pos.x - n / 2.0 + 0.5
                for i in range(n):
                    pts.add(Point(start_x + i, pos.y))
            else:
                start_y = pos.y - n / 2.0 + 0.5
                for i in range(n):
                    pts.add(Point(pos.x, start_y + i))
    return pts


def _build_pin_positions(
    layout: Layout,
    components: dict[str, ComponentDefinition],
    instances: dict[uuid.UUID, ComponentInstance],
) -> dict[PinRef, tuple[Point, str]]:
    """Return PinRef → (world_position, side) for every placed pin."""
    pos: dict[PinRef, tuple[Point, str]] = {}
    for comp_id, placement in layout.placements.items():
        inst = instances.get(comp_id)
        if inst is None:
            continue
        defn = components.get(inst.definition_ref)
        if defn is None:
            continue
        for pin_num in range(1, defn.footprint.pin_count() + 1):
            p = pin_world_position(defn.footprint, placement, pin_num)
            pos[PinRef(component_id=comp_id, pin_number=pin_num)] = (p, placement.side)
    return pos


def _route_net(
    net: Net,
    pin_positions: dict[PinRef, tuple[Point, str]],
    all_pin_positions: dict[tuple[Point, str], uuid.UUID],
    schematic: Schematic,
    board: Board,
    layout: Layout,
    working_wires: list[WireSegment],
    components: dict[str, ComponentDefinition],
    instances: dict[uuid.UUID, ComponentInstance],
    side: Literal["top", "bottom"],
    cost_model: RoutingCostModel,
    max_rip_up_passes: int,
    insulated: bool,
    plated_through_pads: set[Point],
    under_component_penalty: dict[tuple[Point, str], float],
) -> list[WireSegment] | None:
    """Route all connections for *net*.  Returns new wires or None on failure."""
    placed_refs = [ref for ref in net.pins if ref in pin_positions]
    if len(placed_refs) < 2:
        return []

    net_id = net.id
    # Own-net pad positions on BOTH layers (plated-through hole).
    net_ps: set[tuple[Point, str]] = set()
    for ref in net.pins:
        if ref in pin_positions:
            pt, _s = pin_positions[ref]
            net_ps.add((pt, "top"))
            net_ps.add((pt, "bottom"))
    occupied_pads: dict[tuple[Point, str], uuid.UUID] = {
        ps: nid
        for ps, nid in all_pin_positions.items()
        if ps not in net_ps
    }
    same_net_pads: dict[tuple[Point, str], uuid.UUID] = {
        ps: net_id for ps in net_ps
    }

    new_wires: list[WireSegment] = []

    current_graph = ConnectivityGraph(
        board=board,
        layout=_layout_with_wires(layout, working_wires),
        components=components,
        instances=instances,
    )

    # near_pin_pads: pads adjacent (on both layers) to pins that still need
    # routing.  The penalty discourages stealing corridors from other nets.
    #
    # A pin contributes to near_pin_pads only when:
    #   1. It belongs to a net (not floating) — floating pins have no pending
    #      route so no corridor will ever be needed near them.
    #   2. Its net is not yet fully connected in the current working graph —
    #      once a net is complete its pins no longer need reserved space.
    net_pts = {ps[0] for ps in net_ps}
    near_pin_pads: set[tuple[Point, str]] = set()
    pending_pin_pts: set[Point] = set()
    for (pin_pt, _pin_side), nid in occupied_pads.items():
        if nid == _FLOATING:
            continue
        net_obj = schematic.nets.get(nid)
        if net_obj is None:
            continue
        refs_in_net = [r for r in net_obj.pins if r in pin_positions]
        if len(refs_in_net) < 2:
            continue
        first_node = current_graph.node_for_pin(refs_in_net[0])
        net_complete = first_node is not None and all(
            current_graph.node_for_pin(r) == first_node for r in refs_in_net
        )
        if not net_complete:
            pending_pin_pts.add(pin_pt)
    for pin_pt in pending_pin_pts:
        for dx, dy in ((1.0, 0.0), (-1.0, 0.0), (0.0, 1.0), (0.0, -1.0)):
            nb = Point(pin_pt.x + dx, pin_pt.y + dy)
            if nb not in pending_pin_pts and nb not in net_pts:
                near_pin_pads.add((nb, "top"))
                near_pin_pads.add((nb, "bottom"))

    cur_net_wire_points: set[tuple[Point, str]] = set()

    connected_refs: list[PinRef] = [placed_refs[0]]
    first_pt, first_side = pin_positions[placed_refs[0]]
    connected_pts: list[tuple[Point, str]] = [(first_pt, first_side)]

    for ref in placed_refs[1:]:
        target_pt, _ = pin_positions[ref]
        target_node = current_graph.node_for_pin(ref)

        already_connected = False
        if target_node is not None:
            for conn_ref in connected_refs:
                conn_node = current_graph.node_for_pin(conn_ref)
                if conn_node is not None and conn_node == target_node:
                    already_connected = True
                    break

        if already_connected:
            connected_refs.append(ref)
            target_pt2, target_side2 = pin_positions[ref]
            connected_pts.append((target_pt2, target_side2))
            continue

        routed_wires = _route_segment_with_ripup(
            source_options=connected_pts,
            target=target_pt,
            board=board,
            layout=layout,
            working_wires=working_wires,
            additional_wires=new_wires,
            components=components,
            instances=instances,
            side=side,
            cost_model=cost_model,
            max_rip_up_passes=max_rip_up_passes,
            occupied_pins=occupied_pads,
            same_net_pins=same_net_pads,
            near_pin_pads=near_pin_pads,
            insulated=insulated,
            plated_through_pads=plated_through_pads,
            cur_net_wire_points=cur_net_wire_points,
            under_component_penalty=under_component_penalty,
        )
        if routed_wires is None:
            return None
        new_wires.extend(routed_wires)
        working_wires.extend(routed_wires)
        for w in routed_wires:
            for pt in w.path:
                cur_net_wire_points.add((pt, w.side))

        current_graph = ConnectivityGraph(
            board=board,
            layout=_layout_with_wires(layout, working_wires),
            components=components,
            instances=instances,
        )
        connected_refs.append(ref)
        target_pt3, target_side3 = pin_positions[ref]
        connected_pts.append((target_pt3, target_side3))

    return new_wires


def _route_segment_with_ripup(
    source_options: list[tuple[Point, str]],
    target: Point,
    board: Board,
    layout: Layout,
    working_wires: list[WireSegment],
    additional_wires: list[WireSegment],
    components: dict[str, ComponentDefinition],
    instances: dict[uuid.UUID, ComponentInstance],
    side: Literal["top", "bottom"],
    cost_model: RoutingCostModel,
    max_rip_up_passes: int,
    occupied_pins: dict[tuple[Point, str], uuid.UUID],
    same_net_pins: dict[tuple[Point, str], uuid.UUID],
    near_pin_pads: set[tuple[Point, str]],
    insulated: bool,
    plated_through_pads: set[Point],
    cur_net_wire_points: set[tuple[Point, str]],
    under_component_penalty: dict[tuple[Point, str], float],
) -> list[WireSegment] | None:
    """Try to route *target* from any of *source_options*, with rip-up fallback.

    Returns a list of WireSegments (may be more than one when path crosses layers).
    """
    for attempt in range(max_rip_up_passes + 1):
        graph = ConnectivityGraph(
            board=board,
            layout=_layout_with_wires(layout, working_wires),
            components=components,
            instances=instances,
        )
        bare_segs = _build_bare_wire_segments(working_wires)

        wire_occupied = _build_occupied_map(working_wires, exclude_wire_id=None)
        if insulated:
            combined_occupied: dict[tuple[Point, str], uuid.UUID] = dict(occupied_pins)
        else:
            # Bare wire: also block all wire path points.
            # wire_occupied uses Point keys; expand to (Point, side) for both layers.
            combined_occupied = dict(occupied_pins)
            for pt, nid in wire_occupied.items():
                for s in ("top", "bottom"):
                    key = (pt, s)
                    if key not in combined_occupied:
                        combined_occupied[key] = nid
        for ps in same_net_pins:
            combined_occupied.pop(ps, None)
        effective_near = {
            ps for ps in near_pin_pads
            if ps not in combined_occupied and ps not in same_net_pins
        }

        # Collect best result across all source options and both starting sides.
        # Try each source from both top and bottom so the router can pick the
        # globally cheapest path (e.g. straight bottom layer vs. top detour).
        best: tuple[float, list[PathStep]] | None = None
        sides_to_try = ["top", "bottom"] if plated_through_pads else [source_options[0][1]]
        for src_pt, _src_side in source_options:
            for start_side in sides_to_try:
                # Skip if this side has no valid node at the source position.
                results = find_paths(
                    graph=graph,
                    source=src_pt,
                    targets=[target],
                    side=start_side,
                    cost_model=cost_model,
                    k=1,
                    insulated=insulated,
                    occupied_pads=combined_occupied,
                    same_net_pads=same_net_pins,
                    near_pin_pads=effective_near,
                    near_pin_cost=10.0,
                    bare_wire_segments=bare_segs,
                    plated_through_pads=plated_through_pads,
                    penalised_pads=under_component_penalty,
                )
                if results and (best is None or results[0].cost < best[0]):
                    best = (results[0].cost, results[0].path)
        if best is not None:
            path = _truncate_at_same_net_wire(best[1], cur_net_wire_points, target)
            wires = _path_to_wires(path, insulated)
            return wires

        if attempt >= max_rip_up_passes:
            break

        nearest_source = min(
            source_options,
            key=lambda ps: abs(ps[0].x - target.x) + abs(ps[0].y - target.y),
        )
        ripped = _find_wire_to_rip(
            working_wires, nearest_source[0], target, additional_wires
        )
        if ripped is None:
            break
        working_wires.remove(ripped)

    return None


def _truncate_at_same_net_wire(
    path: list[PathStep],
    cur_net_wire_points: set[tuple[Point, str]],
    target: Point,
) -> list[PathStep]:
    """Truncate path at the first intermediate point that lies on a same-net wire.

    The start point (index 0) is never a truncation point — it is already
    connected.  We skip it and look from index 1 onwards.  If we find a
    same-net wire point before the explicit target, we stop there — no need
    to extend further since connectivity is already established.
    """
    if len(path) <= 2:
        return path
    for i in range(1, len(path) - 1):
        pt, s = path[i]
        if (pt, s) in cur_net_wire_points:
            return path[: i + 1]
    return path


def _build_under_component_penalty(
    board: Board,
    layout: Layout,
    components: dict[str, ComponentDefinition],
    instances: dict[uuid.UUID, ComponentInstance],
    penalty: float,
) -> dict[tuple[Point, str], float]:
    """Return a penalty map for top-layer pads that lie under a component body.

    For each placed component, compute its bounding box and mark every board
    pad that falls strictly inside that box (on the top layer) with *penalty*.
    Pin positions themselves are already handled by the occupied/same-net maps,
    so we skip them here — the penalty applies only to the interior pads between
    the pins.
    """
    if penalty <= 0.0:
        return {}

    result: dict[tuple[Point, str], float] = {}
    for comp_id, placement in layout.placements.items():
        inst = instances.get(comp_id)
        if inst is None:
            continue
        defn = components.get(inst.definition_ref)
        if defn is None:
            continue
        if placement.side != "top":
            # Bottom-mounted components penalise bottom pads instead.
            # (Rare case — skip for now, extend if needed.)
            continue
        bbox = component_bbox(defn.footprint, placement)
        # Iterate board atoms and mark those inside the bbox on the top layer.
        for atom, pos in board.atoms():
            if isinstance(atom, (PadAtom, StripAtom)) and bbox.contains(pos):
                key = (pos, "top")
                # Take the maximum penalty if multiple components overlap.
                existing = result.get(key, 0.0)
                if penalty > existing:
                    result[key] = penalty
    return result


def _path_to_wires(path: list[PathStep], insulated: bool) -> list[WireSegment]:
    """Convert a (possibly multi-layer) path into one WireSegment per layer segment."""
    segments = split_path_by_side(path)
    wires: list[WireSegment] = []
    for pts, seg_side in segments:
        if len(pts) < 2:
            continue
        wires.append(WireSegment(
            id=uuid.uuid4(),
            path=pts,
            insulated=insulated,
            side=seg_side,
        ))
    return wires


def _collect_wire_points(
    wires: list[WireSegment],
    net_id: uuid.UUID | None,
    net_wires: set[uuid.UUID] | None,
) -> set[tuple[Point, str]]:
    """Collect all path points from *wires* as (Point, side) pairs."""
    pts: set[tuple[Point, str]] = set()
    for w in wires:
        pts.update((p, w.side) for p in w.path)
    return pts


def _layout_with_wires(layout: Layout, wires: list[WireSegment]) -> Layout:
    """Return a Layout view that uses *wires* instead of layout.wires."""
    from dataclasses import replace
    return replace(layout, wires=wires)


def _build_occupied_map(
    wires: list[WireSegment],
    exclude_wire_id: uuid.UUID | None,
) -> dict[Point, uuid.UUID]:
    """Map every grid point covered by existing wires → sentinel UUID."""
    occupied: dict[Point, uuid.UUID] = {}
    _sentinel = uuid.UUID("00000000-0000-0000-0000-000000000001")
    for wire in wires:
        if wire.id == exclude_wire_id:
            continue
        for i in range(len(wire.path) - 1):
            p1 = wire.path[i]
            p2 = wire.path[i + 1]
            occupied[p1] = _sentinel
            occupied[p2] = _sentinel
            if p1.x == p2.x:
                step = 1.0 if p2.y > p1.y else -1.0
                y = p1.y + step
                while (step > 0 and y < p2.y) or (step < 0 and y > p2.y):
                    occupied[Point(p1.x, y)] = _sentinel
                    y += step
            elif p1.y == p2.y:
                step = 1.0 if p2.x > p1.x else -1.0
                x = p1.x + step
                while (step > 0 and x < p2.x) or (step < 0 and x > p2.x):
                    occupied[Point(x, p1.y)] = _sentinel
                    x += step
    return occupied


def _build_bare_wire_segments(
    wires: list[WireSegment],
) -> set[tuple[Point, Point, str]]:
    """Return the set of (p1, p2, side) triples for all *bare* (non-insulated) wires."""
    segs: set[tuple[Point, Point, str]] = set()
    for wire in wires:
        if wire.insulated:
            continue
        for i in range(len(wire.path) - 1):
            p1, p2 = wire.path[i], wire.path[i + 1]
            if (p1.x, p1.y) <= (p2.x, p2.y):
                segs.add((p1, p2, wire.side))
            else:
                segs.add((p2, p1, wire.side))
    return segs


def _find_wire_to_rip(
    wires: list[WireSegment],
    source: Point,
    target: Point,
    protected: list[WireSegment],
) -> WireSegment | None:
    """Find a wire in *wires* (not in *protected*) that lies roughly between source and target."""
    protected_ids = {w.id for w in protected}
    min_x = min(source.x, target.x) - 1
    max_x = max(source.x, target.x) + 1
    min_y = min(source.y, target.y) - 1
    max_y = max(source.y, target.y) + 1

    for wire in wires:
        if wire.id in protected_ids:
            continue
        for pt in wire.path:
            if min_x <= pt.x <= max_x and min_y <= pt.y <= max_y:
                return wire
    return None
