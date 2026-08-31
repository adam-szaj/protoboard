# SPDX-License-Identifier: MIT
"""Controller-owned contracts for interactive routing operations."""

from __future__ import annotations

import uuid
from collections.abc import Mapping, Sequence, Set
from dataclasses import dataclass
from typing import TYPE_CHECKING, Protocol

if TYPE_CHECKING:
    from protoboard.domain.board.board import Board
    from protoboard.domain.board.pad_state import PadIndex
    from protoboard.domain.components.definition import ComponentDefinition
    from protoboard.domain.components.instance import ComponentInstance
    from protoboard.domain.connectivity.incremental import ConnectivityGraph
    from protoboard.domain.layout.layout import Layout
    from protoboard.domain.layout.wire import Wire
    from protoboard.domain.routing.cost_model import RoutingCostModel
    from protoboard.domain.routing.simplify_mode import SimplifyMode
    from protoboard.domain.routing_settings import RoutingSettings
    from protoboard.domain.schematic.schematic import Schematic
    from protoboard.geometry import Point


class RoutePath(Protocol):
    """One path candidate returned by a pathfinder implementation."""

    @property
    def path(self) -> Sequence[tuple[Point, str]]: ...


class PathfinderPort(Protocol):
    def find_paths(
        self,
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
    ) -> Sequence[RoutePath]: ...


class FindPaths(Protocol):
    def __call__(
        self,
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
    ) -> Sequence[RoutePath]: ...


@dataclass(frozen=True)
class SimplifyInputs:
    pin_positions: Set[Point]
    pad_to_net: Mapping[Point, uuid.UUID]
    board_pads: Set[Point]
    wire_to_net: Mapping[uuid.UUID, uuid.UUID | None]
    pad_index: PadIndex

    def request(
        self,
        wire: Wire,
        other_wires: Sequence[Wire],
        mode: SimplifyMode,
    ) -> SimplifyRequest:
        return SimplifyRequest(
            wire=wire,
            other_wires=tuple(other_wires),
            inputs=self,
            mode=mode,
        )


@dataclass(frozen=True)
class SimplifyRequest:
    wire: Wire
    other_wires: Sequence[Wire]
    inputs: SimplifyInputs
    mode: SimplifyMode


class WireSimplifierPort(Protocol):
    def simplify(self, request: SimplifyRequest) -> Wire: ...


class SimplifyWire(Protocol):
    def __call__(self, request: SimplifyRequest) -> Wire: ...


@dataclass(frozen=True)
class OptimizerOption:
    name: str
    label: str
    notes: str


class WireOptimizerPort(Protocol):
    def options(self) -> Sequence[OptimizerOption]: ...

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
    ) -> Sequence[Wire]: ...


@dataclass(frozen=True)
class UnweaveResult:
    wires: Sequence[Wire]
    before_crossings: int
    after_crossings: int


class WireUnweaverPort(Protocol):
    def unweave(
        self,
        *,
        wires: Sequence[Wire],
        schematic: Schematic,
        graph: ConnectivityGraph,
        allow_layer_change: bool,
        allow_side_flip: bool,
    ) -> UnweaveResult: ...


@dataclass(frozen=True)
class RoutingServicePorts:
    pathfinder: PathfinderPort
    simplifier: WireSimplifierPort
    optimizer: WireOptimizerPort
    unweaver: WireUnweaverPort


__all__ = [
    "FindPaths",
    "OptimizerOption",
    "PathfinderPort",
    "RoutePath",
    "RoutingServicePorts",
    "SimplifyInputs",
    "SimplifyRequest",
    "SimplifyWire",
    "UnweaveResult",
    "WireOptimizerPort",
    "WireSimplifierPort",
    "WireUnweaverPort",
]
