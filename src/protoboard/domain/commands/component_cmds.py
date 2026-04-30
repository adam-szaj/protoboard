# SPDX-License-Identifier: MIT
"""Commands for placing, moving and removing components.

All commands operate on a ComponentState protocol so that both
Layout.placements and session.instances stay in sync through undo/redo,
without creating a hard dependency on the GUI layer.
"""

from __future__ import annotations

import uuid
from dataclasses import dataclass, field
from typing import Protocol

from protoboard.domain.board.board import Board
from protoboard.domain.commands.base import Command
from protoboard.domain.components.definition import ComponentDefinition
from protoboard.domain.components.instance import ComponentInstance
from protoboard.domain.layout.layout import Layout
from protoboard.domain.layout.placement import ComponentPlacement, pin_world_position
from protoboard.domain.layout.wire import WireSegment
from protoboard.geometry import Point


class ComponentState(Protocol):
    """Minimal state needed by all board/component commands."""

    board: Board
    layout: Layout
    instances: dict[uuid.UUID, ComponentInstance]
    components: dict[str, ComponentDefinition]


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

    def revert(self, state: ComponentState) -> None:
        del state.layout.placements[self.component_id]
        state.instances.pop(self.component_id, None)


@dataclass
class MoveComponentCommand(Command[ComponentState]):
    component_id: uuid.UUID
    new_placement: ComponentPlacement
    old_placement: ComponentPlacement
    # Wires before the move — saved for undo.
    _saved_wires: list[WireSegment] = field(
        default_factory=list, init=False, repr=False
    )
    _applied: bool = field(default=False, init=False, repr=False)

    @property
    def description(self) -> str:  # type: ignore[override]
        return f"Move component {self.component_id}"

    def apply(self, state: ComponentState) -> None:
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
            state, self.component_id, self.old_placement, self.new_placement
        )

    def revert(self, state: ComponentState) -> None:
        if not self._applied:
            return
        state.layout.placements[self.component_id] = self.old_placement
        state.layout.wires[:] = self._saved_wires


@dataclass
class RemoveComponentCommand(Command[ComponentState]):
    component_id: uuid.UUID
    _saved_placement: ComponentPlacement | None = None
    _saved_instance: ComponentInstance | None = None

    @property
    def description(self) -> str:  # type: ignore[override]
        return f"Remove component {self.component_id}"

    def apply(self, state: ComponentState) -> None:
        saved = state.layout.placements.pop(self.component_id)
        object.__setattr__(self, "_saved_placement", saved)
        inst = state.instances.pop(self.component_id, None)
        object.__setattr__(self, "_saved_instance", inst)

    def revert(self, state: ComponentState) -> None:
        if self._saved_placement is not None:
            state.layout.placements[self.component_id] = self._saved_placement
        if self._saved_instance is not None:
            state.instances[self.component_id] = self._saved_instance


@dataclass
class AddWireCommand(Command[ComponentState]):
    wire: WireSegment

    @property
    def description(self) -> str:  # type: ignore[override]
        return "Add wire"

    def apply(self, state: ComponentState) -> None:
        state.layout.wires.append(self.wire)

    def revert(self, state: ComponentState) -> None:
        state.layout.wires.remove(self.wire)


@dataclass
class RemoveWireCommand(Command[ComponentState]):
    wire_id: uuid.UUID
    _saved_wire: WireSegment | None = field(default=None, init=False, repr=False)

    @property
    def description(self) -> str:  # type: ignore[override]
        return "Remove wire"

    def apply(self, state: ComponentState) -> None:
        for wire in state.layout.wires:
            if wire.id == self.wire_id:
                object.__setattr__(self, "_saved_wire", wire)
                state.layout.wires.remove(wire)
                return
        raise KeyError(f"Wire {self.wire_id} not found")

    def revert(self, state: ComponentState) -> None:
        if self._saved_wire is not None:
            state.layout.wires.append(self._saved_wire)


# ---------------------------------------------------------------------------
# Wire-update helper (used by MoveComponentCommand)
# ---------------------------------------------------------------------------

_EPS = 1e-6  # tolerance for point equality (raster units)


def _pts_equal(a: Point, b: Point) -> bool:
    return abs(a.x - b.x) < _EPS and abs(a.y - b.y) < _EPS


def _move_blocked(
    state: ComponentState,
    comp_id: uuid.UUID,
    old_pl: ComponentPlacement,
    new_pl: ComponentPlacement,
) -> bool:
    """Return True if the move would place a pin on top of another component's pin."""
    inst = state.instances.get(comp_id)
    if inst is None:
        return False
    defn = state.components.get(inst.definition_ref)
    if defn is None:
        return False

    n_pins = defn.footprint.pin_count()
    new_pin_positions: set[tuple[float, float]] = set()
    for pin_num in range(1, n_pins + 1):
        p = pin_world_position(defn.footprint, new_pl, pin_num)
        new_pin_positions.add((round(p.x, 4), round(p.y, 4)))

    # Check against other components' pin positions.
    for other_id, other_pl in state.layout.placements.items():
        if other_id == comp_id:
            continue
        other_inst = state.instances.get(other_id)
        if other_inst is None:
            continue
        other_defn = state.components.get(other_inst.definition_ref)
        if other_defn is None:
            continue
        for pin_num in range(1, other_defn.footprint.pin_count() + 1):
            p = pin_world_position(other_defn.footprint, other_pl, pin_num)
            if (round(p.x, 4), round(p.y, 4)) in new_pin_positions:
                return True
    return False


def _update_wires_for_move(
    state: ComponentState,
    comp_id: uuid.UUID,
    old_pl: ComponentPlacement,
    new_pl: ComponentPlacement,
) -> None:
    """Adjust wire endpoints that coincide with pins of the moved component.

    For each pin of the component, if a wire endpoint was at the old pin
    position, move it to the new pin position.  If the resulting path
    collapses (start == end) or has fewer than 2 distinct points, the
    wire is removed.
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

    new_wires: list[WireSegment] = []
    for wire in state.layout.wires:
        new_path: list[Point] = []
        for pt in wire.path:
            key = (round(pt.x, 6), round(pt.y, 6))
            new_path.append(pin_map.get(key, pt))
        # Drop wire if it collapsed (all points identical or < 2 distinct).
        distinct = {(round(p.x, 6), round(p.y, 6)) for p in new_path}
        if len(distinct) >= 2:
            if any(
                not _pts_equal(new_path[i], wire.path[i])
                for i in range(len(new_path))
            ):
                from dataclasses import replace as _replace
                wire = _replace(wire, path=new_path)
        new_wires.append(wire)

    state.layout.wires[:] = new_wires
