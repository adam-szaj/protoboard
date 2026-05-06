# SPDX-License-Identifier: MIT
"""ConnectivityGraph — builds an electrical graph from Board + Layout.

Each node represents an electrically distinct point. Nodes are created for:
- Each pad layer (top/bottom); plated-through pads merge top+bottom into one.
- Holes of a StripAtom share one node per layer (strips are conductors).
Wires add edges between the nodes at their path endpoints.
Components are used to map PinRefs to pad nodes.
"""

from __future__ import annotations

import uuid
from collections import defaultdict

import networkx as nx

from protoboard.domain.board.board import Board
from protoboard.domain.board.boxes import PadAtom, StripAtom
from protoboard.domain.components.definition import ComponentDefinition
from protoboard.domain.components.instance import ComponentInstance
from protoboard.domain.layout.layout import Layout
from protoboard.domain.layout.placement import pin_world_position
from protoboard.domain.schematic.net import PinRef
from protoboard.geometry import Point

# A node key is (x, y, side) where (x,y) is the center of the pad and
# side is "top" or "bottom". Plated-through pads collapse both sides into
# a single canonical node (top).
_NodeKey = tuple[float, float, str]


class ConnectivityGraph:
    """Electrical graph for a board + layout combination."""

    def __init__(
        self,
        board: Board,
        layout: Layout,
        components: dict[str, ComponentDefinition],
        instances: dict[uuid.UUID, ComponentInstance] | None = None,
    ) -> None:
        self._g: nx.Graph[_NodeKey] = nx.Graph()
        self._pin_to_node: dict[PinRef, _NodeKey] = {}
        self._instances = instances or {}
        self._components = components
        self._build(board, layout)

    # ------------------------------------------------------------------
    # Public API
    # ------------------------------------------------------------------

    def node_count(self) -> int:
        return int(self._g.number_of_nodes())

    def component_count(self) -> int:
        return int(nx.number_connected_components(self._g))

    def electrical_nets(self) -> list[set[PinRef]]:
        """Return one set of PinRefs per connected component."""
        node_to_pins: dict[_NodeKey, set[PinRef]] = defaultdict(set)
        for ref, node in self._pin_to_node.items():
            # Resolve to the canonical node in the union-find.
            canonical = self._canonical(node)
            node_to_pins[canonical].add(ref)
        result: list[set[PinRef]] = []
        for component in nx.connected_components(self._g):
            pins: set[PinRef] = set()
            for n in component:
                pins |= node_to_pins.get(n, set())
            result.append(pins)
        return result

    def node_for_pin(self, ref: PinRef) -> _NodeKey | None:
        node = self._pin_to_node.get(ref)
        if node is None:
            return None
        return self._canonical(node)  # returns None if node not in graph

    def node_for_pad(self, position: Point, side: str) -> _NodeKey | None:
        key: _NodeKey = (position.x, position.y, side)
        if not self._g.has_node(key):
            return None
        return self._canonical(key)

    # ------------------------------------------------------------------
    # Build
    # ------------------------------------------------------------------

    def _canonical(self, node: _NodeKey) -> _NodeKey | None:
        """Return the representative node in the connected component, or None if absent."""
        if not self._g.has_node(node):
            return None
        for component in nx.connected_components(self._g):
            if node in component:
                result: _NodeKey = min(component)
                return result
        return node

    def _add_node(self, key: _NodeKey) -> None:
        if not self._g.has_node(key):
            self._g.add_node(key)

    def _add_edge(self, a: _NodeKey, b: _NodeKey) -> None:
        self._add_node(a)
        self._add_node(b)
        self._g.add_edge(a, b)

    def _build(self, board: Board, layout: Layout) -> None:
        self._build_board_nodes(board)
        self._build_wire_edges(layout)
        self._build_component_pin_map(layout)

    def _build_board_nodes(self, board: Board) -> None:
        for atom, pos in board.atoms():
            x, y = pos.x, pos.y
            if isinstance(atom, PadAtom):
                layers = atom.layers
                if layers.plated_through:
                    # Plated-through: top and bottom are electrically one node.
                    # Add both side-nodes and connect them so the router can
                    # switch layers here while keeping a single connected component.
                    self._add_edge((x, y, "top"), (x, y, "bottom"))
                else:
                    if layers.top:
                        self._add_node((x, y, "top"))
                    if layers.bottom:
                        self._add_node((x, y, "bottom"))
            elif isinstance(atom, StripAtom):
                if atom.isolated:
                    # Isolated pad — physical hole, no electrical connectivity.
                    continue
                self._build_strip_nodes(atom, pos)

    def _build_strip_nodes(self, atom: StripAtom, center: Point) -> None:
        """Add one node per hole of *atom* and connect them all (same net)."""
        layers = atom.layers
        n = int(atom.length) if isinstance(atom.length, int) else 1
        side = "top" if layers.plated_through or layers.top else "bottom"
        if atom.orientation == "horizontal":
            start_x = center.x - n / 2.0 + 0.5
            positions = [Point(start_x + i, center.y) for i in range(n)]
        else:
            start_y = center.y - n / 2.0 + 0.5
            positions = [Point(center.x, start_y + i) for i in range(n)]
        keys: list[_NodeKey] = [(p.x, p.y, side) for p in positions]
        for key in keys:
            self._add_node(key)
        # Connect consecutive holes — same strip segment = same net.
        for i in range(len(keys) - 1):
            self._add_edge(keys[i], keys[i + 1])

    def _build_wire_edges(self, layout: Layout) -> None:
        for wire in layout.wires:
            side = wire.side
            pts = wire.path
            # Walk consecutive points and connect their pad nodes.
            for i in range(len(pts) - 1):
                a_key: _NodeKey = (pts[i].x, pts[i].y, side)
                b_key: _NodeKey = (pts[i + 1].x, pts[i + 1].y, side)
                # Ensure intermediate points create nodes even if not pads.
                self._add_edge(a_key, b_key)

    def _build_component_pin_map(self, layout: Layout) -> None:
        for comp_id, placement in layout.placements.items():
            inst = self._instances.get(comp_id)
            if inst is None:
                continue
            defn = self._components.get(inst.definition_ref)
            if defn is None:
                continue
            fp = defn.footprint
            for pin_num in range(1, fp.pin_count() + 1):
                world_pos = pin_world_position(fp, placement, pin_num)
                side = placement.side
                node_key: _NodeKey = (world_pos.x, world_pos.y, side)
                ref = PinRef(component_id=comp_id, pin_number=pin_num)
                self._pin_to_node[ref] = node_key
