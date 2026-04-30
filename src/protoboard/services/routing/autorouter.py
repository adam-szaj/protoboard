# SPDX-License-Identifier: MIT
"""Full-netlist autorouter using A* with rip-up & retry.

Strategy:
1. Sort nets by pin-count (smallest first — easier nets first).
2. For each unrouted connection in a net (Steiner-like spanning route):
   a. Pick the nearest already-connected pad as source.
   b. Run A* to any unconnected target pad in the same net.
   c. If no path found, rip-up the lowest-priority previously routed net
      that overlaps the failed path region and retry.
3. Return a list of new WireSegment objects (caller adds them to layout).
4. Return a list of nets that could not be routed (unrouted report).

Limitations of this MVP implementation:
- Top layer only.
- No rip-up cascade beyond one level (rip up the one blocking net, retry).
- Straight-line (4-connected) only — no diagonal.
"""

from __future__ import annotations

import uuid
from dataclasses import dataclass, field
from typing import Literal

from protoboard.domain.board.board import Board
from protoboard.domain.components.definition import ComponentDefinition
from protoboard.domain.components.instance import ComponentInstance
from protoboard.domain.connectivity.graph import ConnectivityGraph
from protoboard.domain.layout.layout import Layout
from protoboard.domain.layout.placement import pin_world_position
from protoboard.domain.layout.wire import WireSegment
from protoboard.domain.schematic.net import Net, PinRef
from protoboard.domain.schematic.schematic import Schematic
from protoboard.geometry import Point
from protoboard.services.routing.astar import find_paths
from protoboard.services.routing.cost_model import RoutingCostModel

_DEFAULT_COST = RoutingCostModel()


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
) -> AutorouteResult:
    """Route all unrouted nets in *schematic* and return new wire segments.

    Already-routed connections (where a physical path already exists) are
    skipped.  Only completely *missing* connections are routed.

    The caller is responsible for adding the returned wires to the layout
    and running DRC.
    """
    result = AutorouteResult()

    # Build pin-position lookup: PinRef → world position.
    pin_positions = _build_pin_positions(layout, components, instances)

    # Build a map: world position → net_id for every placed pin that belongs
    # to a net.  Used to mark pads as occupied (other net) or same-net.
    point_to_net: dict[Point, uuid.UUID] = {}
    for net in schematic.nets.values():
        for ref in net.pins:
            pt = pin_positions.get(ref)
            if pt is not None:
                point_to_net[pt] = net.id

    # Also collect ALL placed pin positions — including floating pins that
    # belong to no net.  These must be blocked too (any wire passing through
    # a component pin creates an unintended electrical connection).
    _FLOATING = uuid.UUID("00000000-0000-0000-0000-000000000002")
    all_pin_positions: dict[Point, uuid.UUID] = {}
    for pt in pin_positions.values():
        # Use net id if known, otherwise mark as floating (still blocked).
        all_pin_positions[pt] = point_to_net.get(pt, _FLOATING)

    # Work on a *mutable copy* of wires so we can do rip-up without touching
    # the real layout.
    working_wires: list[WireSegment] = list(layout.wires)

    # Sort nets: fewer pins first (simpler to route).
    sorted_nets = sorted(schematic.nets.values(), key=lambda n: len(n.pins))

    for net in sorted_nets:
        routed = _route_net(
            net=net,
            pin_positions=pin_positions,
            all_pin_positions=all_pin_positions,
            board=board,
            layout=layout,
            working_wires=working_wires,
            components=components,
            instances=instances,
            side=side,
            cost_model=cost_model,
            max_rip_up_passes=max_rip_up_passes,
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

def _build_pin_positions(
    layout: Layout,
    components: dict[str, ComponentDefinition],
    instances: dict[uuid.UUID, ComponentInstance],
) -> dict[PinRef, Point]:
    pos: dict[PinRef, Point] = {}
    for comp_id, placement in layout.placements.items():
        inst = instances.get(comp_id)
        if inst is None:
            continue
        defn = components.get(inst.definition_ref)
        if defn is None:
            continue
        for pin_num in range(1, defn.footprint.pin_count() + 1):
            p = pin_world_position(defn.footprint, placement, pin_num)
            pos[PinRef(component_id=comp_id, pin_number=pin_num)] = p
    return pos


def _route_net(
    net: Net,
    pin_positions: dict[PinRef, Point],
    all_pin_positions: dict[Point, uuid.UUID],
    board: Board,
    layout: Layout,
    working_wires: list[WireSegment],
    components: dict[str, ComponentDefinition],
    instances: dict[uuid.UUID, ComponentInstance],
    side: Literal["top", "bottom"],
    cost_model: RoutingCostModel,
    max_rip_up_passes: int,
) -> list[WireSegment] | None:
    """Route all connections for *net*.  Returns new wires or None on failure."""
    # Collect pin positions for this net (skip unplaced pins).
    placed_refs = [ref for ref in net.pins if ref in pin_positions]
    if len(placed_refs) < 2:
        return []  # nothing to route

    # Build pad occupancy maps for this net:
    # - occupied: ALL placed pin pads except those of *this* net → blocked
    #   (includes other-net pins AND floating pins — routing through any
    #   component pin that is not part of this net creates a short or
    #   unintended connection)
    # - same_net: pads belonging to *this* net → discounted cost
    net_id = net.id
    net_pts: set[Point] = {
        pin_positions[ref] for ref in net.pins if ref in pin_positions
    }
    occupied_pads: dict[Point, uuid.UUID] = {
        pt: nid
        for pt, nid in all_pin_positions.items()
        if pt not in net_pts
    }
    same_net_pads: dict[Point, uuid.UUID] = {
        pt: net_id for pt in net_pts
    }

    new_wires: list[WireSegment] = []

    # Build a connectivity graph from the current working state to detect which
    # pin pairs are already physically connected — skip those.
    current_graph = ConnectivityGraph(
        board=board,
        layout=_layout_with_wires(layout, working_wires),
        components=components,
        instances=instances,
    )

    # Spanning-tree approach: maintain a set of already-connected refs.
    # Start with the first placed pin; add each remaining pin only if it is
    # not yet in the same electrical component as any connected pin.
    connected_refs: list[PinRef] = [placed_refs[0]]
    connected_pts: list[Point] = [pin_positions[placed_refs[0]]]

    for ref in placed_refs[1:]:
        target_pt = pin_positions[ref]
        target_node = current_graph.node_for_pin(ref)

        # Check if *ref* is already connected to any of the connected pins.
        already_connected = False
        if target_node is not None:
            for conn_ref in connected_refs:
                conn_node = current_graph.node_for_pin(conn_ref)
                if conn_node is not None and conn_node == target_node:
                    already_connected = True
                    break

        if already_connected:
            connected_refs.append(ref)
            connected_pts.append(target_pt)
            continue

        routed_wire = _route_segment_with_ripup(
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
        )
        if routed_wire is None:
            return None  # failed to route this net
        new_wires.append(routed_wire)
        working_wires.append(routed_wire)
        # Rebuild graph so next iteration sees the new wire.
        current_graph = ConnectivityGraph(
            board=board,
            layout=_layout_with_wires(layout, working_wires),
            components=components,
            instances=instances,
        )
        connected_refs.append(ref)
        connected_pts.append(target_pt)

    return new_wires


def _route_segment_with_ripup(
    source_options: list[Point],
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
    occupied_pins: dict[Point, uuid.UUID],
    same_net_pins: dict[Point, uuid.UUID],
) -> WireSegment | None:
    """Try to route *target* from any of *source_options*, with rip-up fallback.

    *occupied_pins* — pad positions belonging to other nets (blocked).
    *same_net_pins* — pad positions belonging to this net (discounted).
    Wire path points from other already-routed wires are also blocked.
    """
    for attempt in range(max_rip_up_passes + 1):
        graph = ConnectivityGraph(
            board=board,
            layout=_layout_with_wires(layout, working_wires),
            components=components,
            instances=instances,
        )
        # Merge occupied_pins with wire-path occupancy; same_net_pins take
        # precedence over wire occupancy for this net's own pads.
        wire_occupied = _build_occupied_map(working_wires, exclude_wire_id=None)
        # Wire points that happen to coincide with same-net pins are not blocked.
        combined_occupied: dict[Point, uuid.UUID] = {
            **wire_occupied,
            **occupied_pins,
        }
        for pt in same_net_pins:
            combined_occupied.pop(pt, None)

        for source in source_options:
            results = find_paths(
                graph=graph,
                source=source,
                targets=[target],
                side=side,
                cost_model=cost_model,
                k=1,
                occupied_pads=combined_occupied,
                same_net_pads=same_net_pins,
            )
            if results:
                path = results[0].path
                wire = WireSegment(
                    id=uuid.uuid4(),
                    path=path,
                    insulated=True,
                    side=side,
                )
                return wire

        if attempt >= max_rip_up_passes:
            break

        # Rip-up: find a wire whose path passes through the region between
        # the nearest source and target, then remove it temporarily.
        nearest_source = min(
            source_options,
            key=lambda p: abs(p.x - target.x) + abs(p.y - target.y),
        )
        ripped = _find_wire_to_rip(
            working_wires, nearest_source, target, additional_wires
        )
        if ripped is None:
            break  # nothing to rip up
        working_wires.remove(ripped)

    return None


def _layout_with_wires(layout: Layout, wires: list[WireSegment]) -> Layout:
    """Return a Layout view that uses *wires* instead of layout.wires."""
    from dataclasses import replace
    return replace(layout, wires=wires)


def _build_occupied_map(
    wires: list[WireSegment],
    exclude_wire_id: uuid.UUID | None,
) -> dict[Point, uuid.UUID]:
    """Map each wire path point → a sentinel UUID (just marks as occupied)."""
    occupied: dict[Point, uuid.UUID] = {}
    _sentinel = uuid.UUID("00000000-0000-0000-0000-000000000001")
    for wire in wires:
        if wire.id == exclude_wire_id:
            continue
        for pt in wire.path:
            occupied[pt] = _sentinel
    return occupied


def _find_wire_to_rip(
    wires: list[WireSegment],
    source: Point,
    target: Point,
    protected: list[WireSegment],
) -> WireSegment | None:
    """Find a wire in *wires* (not in *protected*) that lies roughly between
    source and target (within the bounding box expanded by 1 unit)."""
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
