# SPDX-License-Identifier: MIT
"""Default implementations of controller-owned routing ports."""

from __future__ import annotations

import uuid
from collections.abc import Mapping, Sequence, Set
from typing import TYPE_CHECKING

from protoboard.controller.ports.routing import (
    OptimizerOption,
    RoutePath,
    RoutingServicePorts,
    SimplifyRequest,
    UnweaveResult,
)

if TYPE_CHECKING:
    from protoboard.domain.board.board import Board
    from protoboard.domain.components.definition import ComponentDefinition
    from protoboard.domain.components.instance import ComponentInstance
    from protoboard.domain.connectivity.incremental import ConnectivityGraph
    from protoboard.domain.layout.layout import Layout
    from protoboard.domain.layout.wire import Wire
    from protoboard.domain.routing.cost_model import RoutingCostModel
    from protoboard.domain.routing_settings import RoutingSettings
    from protoboard.domain.schematic.schematic import Schematic
    from protoboard.geometry import Point


class DefaultPathfinder:
    def find_paths(
        self,
        *,
        graph: ConnectivityGraph,
        source: Point,
        targets: Sequence[Point],
        side: str,
        cost_model: RoutingCostModel,
        k: int,
        insulated: bool = True,
        occupied_pads: Mapping[tuple[Point, str], uuid.UUID] | None = None,
        same_net_pads: Mapping[tuple[Point, str], uuid.UUID] | None = None,
        near_pin_pads: Set[tuple[Point, str]] | None = None,
        near_pin_cost: float = 10.0,
        bare_wire_segments: (
            set[tuple[Point, Point, str, uuid.UUID | None]]
            | set[tuple[Point, Point, str]]
            | None
        ) = None,
        plated_through_pads: Set[Point] | None = None,
        penalised_pads: Mapping[tuple[Point, str], float] | None = None,
        extra_budget_ratio: float = 0.5,
        at_pad_wires: Mapping[tuple[Point, str], int] | None = None,
        all_segments: (
            list[tuple[Point, Point, str, bool, uuid.UUID | None]]
            | list[tuple[Point, Point, str, bool]]
            | None
        ) = None,
        active_net_id: uuid.UUID | None = None,
        at_pad_nets: Mapping[tuple[Point, str], frozenset[uuid.UUID]] | None = None,
        same_net_pad_vertices: Set[tuple[Point, str]] | None = None,
        foreign_wire_cells: Set[tuple[Point, str]] | None = None,
        foreign_via_pads: Set[Point] | None = None,
        active_dominant_side: str | None = None,
    ) -> Sequence[RoutePath]:
        from protoboard.services.routing.astar import find_paths

        return find_paths(
            graph,
            source,
            list(targets),
            side,
            cost_model,
            k,
            insulated=insulated,
            occupied_pads=(dict(occupied_pads) if occupied_pads is not None else None),
            same_net_pads=(dict(same_net_pads) if same_net_pads is not None else None),
            near_pin_pads=set(near_pin_pads) if near_pin_pads is not None else None,
            near_pin_cost=near_pin_cost,
            bare_wire_segments=bare_wire_segments,
            plated_through_pads=(
                set(plated_through_pads) if plated_through_pads is not None else None
            ),
            penalised_pads=(
                dict(penalised_pads) if penalised_pads is not None else None
            ),
            extra_budget_ratio=extra_budget_ratio,
            at_pad_wires=(dict(at_pad_wires) if at_pad_wires is not None else None),
            all_segments=all_segments,
            active_net_id=active_net_id,
            at_pad_nets=dict(at_pad_nets) if at_pad_nets is not None else None,
            same_net_pad_vertices=(
                set(same_net_pad_vertices)
                if same_net_pad_vertices is not None
                else None
            ),
            foreign_wire_cells=(
                set(foreign_wire_cells) if foreign_wire_cells is not None else None
            ),
            foreign_via_pads=(
                set(foreign_via_pads) if foreign_via_pads is not None else None
            ),
            active_dominant_side=active_dominant_side,
        )


class DefaultWireSimplifier:
    def simplify(self, request: SimplifyRequest) -> Wire:
        from protoboard.services.routing.postprocess import generic_simplify_wire

        inputs = request.inputs
        return generic_simplify_wire(
            request.wire,
            list(request.other_wires),
            set(inputs.pin_positions),
            dict(inputs.pad_to_net),
            mode=request.mode,
            board_pads=set(inputs.board_pads),
            wire_net_id=inputs.wire_to_net.get(request.wire.id),
            other_wire_nets=dict(inputs.wire_to_net),
            pin_net_map=dict(inputs.pad_to_net),
            pad_index=inputs.pad_index,
        )


class DefaultWireOptimizer:
    def options(self) -> Sequence[OptimizerOption]:
        import protoboard.services.routing.builtin_optimizers  # noqa: F401
        from protoboard.services.routing.optimizer import registry

        options: list[OptimizerOption] = []
        for name in registry.list_available():
            if name == "simplify":
                continue
            optimizer = registry.get(name)
            options.append(
                OptimizerOption(
                    name=name,
                    label=optimizer.display_name,
                    notes=optimizer.capabilities().notes,
                )
            )
        return options

    def optimize(
        self,
        name: str,
        *,
        board: Board,
        layout: Layout,
        schematic: Schematic,
        components: Mapping[str, ComponentDefinition],
        instances: Mapping[uuid.UUID, ComponentInstance],
        settings: RoutingSettings,
        wires: Sequence[Wire],
    ) -> Sequence[Wire]:
        import protoboard.services.routing.builtin_optimizers  # noqa: F401
        from protoboard.services.routing.optimizer import registry
        from protoboard.services.routing.protocol import ProgressSink
        from protoboard.services.routing.results import AutorouteResult
        from protoboard.services.routing.snapshot import RouteRequest

        optimizer = registry.get(name)
        request = RouteRequest.from_session(
            board=board,
            layout=layout,
            schematic=schematic,
            components=dict(components),
            instances=dict(instances),
            routing_settings=settings,
        )
        result = optimizer.optimize(
            request,
            AutorouteResult(wires=list(wires), unrouted_nets=[], pass_stats=[]),
            ProgressSink(),
        )
        return tuple(result.wires)


class DefaultWireUnweaver:
    def unweave(
        self,
        *,
        wires: Sequence[Wire],
        schematic: Schematic,
        graph: ConnectivityGraph,
        allow_layer_change: bool,
        allow_side_flip: bool,
    ) -> UnweaveResult:
        from protoboard.services.routing.unweave import (
            count_mutual_crossings,
            unweave_wires,
        )

        before = count_mutual_crossings(list(wires))
        output = unweave_wires(
            list(wires),
            schematic=schematic,
            graph=graph,
            allow_layer_change=allow_layer_change,
            allow_side_flip=allow_side_flip,
        )
        return UnweaveResult(
            wires=tuple(output),
            before_crossings=before,
            after_crossings=count_mutual_crossings(output),
        )


def default_routing_service_ports() -> RoutingServicePorts:
    return RoutingServicePorts(
        pathfinder=DefaultPathfinder(),
        simplifier=DefaultWireSimplifier(),
        optimizer=DefaultWireOptimizer(),
        unweaver=DefaultWireUnweaver(),
    )


__all__ = [
    "DefaultPathfinder",
    "DefaultWireOptimizer",
    "DefaultWireSimplifier",
    "DefaultWireUnweaver",
    "default_routing_service_ports",
]
