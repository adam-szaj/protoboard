# SPDX-License-Identifier: MIT
"""Controller-owned capability contracts for editing sessions."""

from __future__ import annotations

import uuid
from collections.abc import Iterable
from typing import TYPE_CHECKING, Protocol, runtime_checkable

from protoboard.domain.commands.base import SchematicState

if TYPE_CHECKING:
    from protoboard.controller.editing.controller import EditController
    from protoboard.controller.editing.item_handle import ItemId
    from protoboard.controller.highlight_stack import HighlightStackBase
    from protoboard.controller.selection.service import SelectionService
    from protoboard.domain.axis_labels_settings import AxisLabelSettings
    from protoboard.domain.board.pad_state import PadIndex
    from protoboard.domain.commands.base import CommandStack, ComponentState
    from protoboard.domain.components.package import PackageFamily
    from protoboard.domain.connectivity.incremental import ConnectivityGraph
    from protoboard.domain.layout.group_tree import GroupTree
    from protoboard.domain.layout.pad_conflict import ConflictScanner
    from protoboard.domain.layout.wire import Wire
    from protoboard.domain.routing_settings import RoutingSettings
    from protoboard.geometry import Point, Rect


@runtime_checkable
class ProjectModel(SchematicState, Protocol):
    """Project data and configuration read by editing use cases."""

    package_families: dict[str, PackageFamily]
    axis_label_settings: AxisLabelSettings


@runtime_checkable
class SpatialQueries(Protocol):
    """Cached and derived geometry queries."""

    def get_pin_position(self, comp_id: uuid.UUID, pin_number: int) -> Point | None: ...
    def comp_spatial_index(self) -> dict[tuple[int, int], list[uuid.UUID]]: ...
    def wire_spatial_index(self) -> dict[tuple[int, int], list[uuid.UUID]]: ...
    def iter_pin_positions(self, comp_id: uuid.UUID) -> list[tuple[int, Point]]: ...
    def meta_pin_for_package_pin(
        self, comp_id: uuid.UUID, package_pin_number: int
    ) -> int | None: ...
    def component_bbox(self, comp_id: uuid.UUID) -> Rect | None: ...
    def cached_plated_through_pads(self) -> set[Point]: ...


@runtime_checkable
class ConnectivityQueries(Protocol):
    """Connectivity, group ownership, and conflict queries."""

    def wire_by_id(self, wire_id: uuid.UUID) -> Wire | None: ...
    def group_tree(self) -> GroupTree: ...
    def cached_wire_net_map(self) -> dict[uuid.UUID, uuid.UUID]: ...
    def conflict_scanner(
        self, *, pad_index: PadIndex | None = ...
    ) -> ConflictScanner: ...
    def pad_index(self) -> PadIndex: ...
    def connectivity_graph(self) -> ConnectivityGraph: ...


@runtime_checkable
class EditingEffects(Protocol):
    """Command execution and cache/model invalidation effects."""

    stack: CommandStack[ComponentState]

    def notify_changed(self) -> None: ...
    def invalidate_graph(self) -> None: ...


@runtime_checkable
class InteractionState(Protocol):
    """Selection, active-tool, highlight, and routing interaction state."""

    routing_settings: RoutingSettings
    selection_service: SelectionService | None

    def selection(self) -> frozenset[ItemId]: ...
    def select(self, ids: Iterable[ItemId]) -> None: ...
    def notify_active_tool_changed(self) -> None: ...
    def notify_selection(
        self,
        comp_ids: frozenset[uuid.UUID],
        wire_ids: frozenset[uuid.UUID],
        net_id: uuid.UUID | None,
    ) -> None: ...

    @property
    def edit_controller(self) -> EditController: ...

    @property
    def highlight_stack(self) -> HighlightStackBase: ...

    def set_routing_net(self, net_id: uuid.UUID | None) -> None: ...

    @property
    def routing_net_id(self) -> uuid.UUID | None: ...


@runtime_checkable
class EditingContext(
    ProjectModel,
    SpatialQueries,
    ConnectivityQueries,
    EditingEffects,
    InteractionState,
    Protocol,
):
    """Complete session surface accepted at orchestration seams."""


@runtime_checkable
class PreviewProjectContext(ProjectModel, Protocol):
    """Project and pose reads used by snap and drag classification."""

    def comp_spatial_index(self) -> dict[tuple[int, int], list[uuid.UUID]]: ...
    def iter_pin_positions(self, comp_id: uuid.UUID) -> list[tuple[int, Point]]: ...
    def group_tree(self) -> GroupTree: ...
    def cached_wire_net_map(self) -> dict[uuid.UUID, uuid.UUID]: ...


__all__ = [
    "ConnectivityQueries",
    "EditingContext",
    "EditingEffects",
    "InteractionState",
    "PreviewProjectContext",
    "ProjectModel",
    "SpatialQueries",
]
