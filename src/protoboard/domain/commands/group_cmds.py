# SPDX-License-Identifier: MIT
"""Commands for creating, disbanding and modifying component groups."""

from __future__ import annotations

import uuid
from dataclasses import dataclass, field

from protoboard.domain.commands.base import Command
from protoboard.domain.commands.component_cmds import ComponentState
from protoboard.domain.layout.group import ComponentGroup, ExportedPin, all_component_ids, would_create_cycle
from protoboard.domain.layout.placement import pin_world_position
from protoboard.domain.layout.wire import WireSegment
from protoboard.geometry import Point


def _replace_group(g: ComponentGroup, **kwargs: object) -> ComponentGroup:
    """Return a copy of *g* with the given fields replaced."""
    return ComponentGroup(
        id=kwargs.get("id", g.id),  # type: ignore[arg-type]
        name=kwargs.get("name", g.name),  # type: ignore[arg-type]
        component_ids=kwargs.get("component_ids", g.component_ids),  # type: ignore[arg-type]
        child_group_ids=kwargs.get("child_group_ids", g.child_group_ids),  # type: ignore[arg-type]
        exported_pins=kwargs.get("exported_pins", g.exported_pins),  # type: ignore[arg-type]
        rotation=kwargs.get("rotation", g.rotation),  # type: ignore[arg-type]
        origin=kwargs.get("origin", g.origin),  # type: ignore[arg-type]
    )


def _rotate_point_around(p: Point, origin: Point, delta: int) -> Point:
    """Rotate *p* clockwise by *delta* degrees around *origin*."""
    dx, dy = p.x - origin.x, p.y - origin.y
    d = delta % 360
    if d == 90:
        rx, ry = dy, -dx
    elif d == 180:
        rx, ry = -dx, -dy
    elif d == 270:
        rx, ry = -dy, dx
    else:
        rx, ry = dx, dy
    return Point(origin.x + rx, origin.y + ry)


def _group_origin(
    comp_ids: frozenset[uuid.UUID],
    placements: dict[uuid.UUID, object],
    instances: dict[uuid.UUID, object],
    components: dict[str, object],
) -> Point | None:
    """Return the pad closest to the top-left corner of the group bbox (min x+y)."""
    from protoboard.domain.layout.placement import ComponentPlacement
    from protoboard.domain.components.definition import ComponentDefinition
    from protoboard.domain.components.instance import ComponentInstance
    best: Point | None = None
    best_key = float("inf")
    for cid in comp_ids:
        pl = placements.get(cid)
        inst = instances.get(cid) if pl is not None else None
        defn = components.get(inst.definition_ref) if inst is not None else None  # type: ignore[union-attr]
        if not isinstance(pl, ComponentPlacement) or not isinstance(defn, ComponentDefinition):
            continue
        for n in range(defn.footprint.pin_count()):
            p = pin_world_position(defn.footprint, pl, n + 1)
            key = p.x + p.y
            if key < best_key:
                best_key, best = key, p
    return best


def _group_bbox(
    comp_ids: frozenset[uuid.UUID],
    placements: dict[uuid.UUID, object],
    instances: dict[uuid.UUID, object],
    components: dict[str, object],
) -> tuple[float, float, float, float] | None:
    """Return (x_min, y_min, x_max, y_max) bounding box of all group pads, or None."""
    from protoboard.domain.layout.placement import ComponentPlacement
    from protoboard.domain.components.definition import ComponentDefinition
    xs: list[float] = []
    ys: list[float] = []
    for cid in comp_ids:
        pl = placements.get(cid)
        inst = instances.get(cid) if pl is not None else None
        defn = components.get(inst.definition_ref) if inst is not None else None  # type: ignore[union-attr]
        if not isinstance(pl, ComponentPlacement) or not isinstance(defn, ComponentDefinition):
            continue
        for n in range(defn.footprint.pin_count()):
            p = pin_world_position(defn.footprint, pl, n + 1)
            xs.append(p.x)
            ys.append(p.y)
    if not xs:
        return None
    return min(xs), min(ys), max(xs), max(ys)


def _wire_is_internal(
    wire: WireSegment,
    bbox: tuple[float, float, float, float],
    tol: float = 0.5,
) -> bool:
    """True if ALL points of *wire* lie within the group bounding box (+ tol)."""
    x0, y0, x1, y1 = bbox
    def inside(p: Point) -> bool:
        return x0 - tol <= p.x <= x1 + tol and y0 - tol <= p.y <= y1 + tol
    return len(wire.path) >= 2 and all(inside(p) for p in wire.path)


@dataclass
class CreateGroupCommand(Command[ComponentState]):
    """Create a new group from a set of component IDs."""

    group: ComponentGroup

    @property
    def description(self) -> str:  # type: ignore[override]
        return f"Create group '{self.group.name}'"

    def apply(self, state: ComponentState) -> None:
        state.layout.groups[self.group.id] = self.group

    def revert(self, state: ComponentState) -> None:
        state.layout.groups.pop(self.group.id, None)


@dataclass
class DisbandGroupCommand(Command[ComponentState]):
    """Remove a group (components remain placed, child groups become top-level)."""

    group_id: uuid.UUID
    _saved_group: ComponentGroup | None = field(
        default=None, init=False, repr=False
    )
    # Parent group that contained this group (if any), saved for undo.
    _saved_parent_id: uuid.UUID | None = field(
        default=None, init=False, repr=False
    )

    @property
    def description(self) -> str:  # type: ignore[override]
        return "Disband group"

    def apply(self, state: ComponentState) -> None:
        group = state.layout.groups.pop(self.group_id, None)
        object.__setattr__(self, "_saved_group", group)
        # Find and patch parent group that references this group.
        for gid, g in state.layout.groups.items():
            if self.group_id in g.child_group_ids:
                object.__setattr__(self, "_saved_parent_id", gid)
                state.layout.groups[gid] = _replace_group(
                    g, child_group_ids=g.child_group_ids - {self.group_id}
                )
                break

    def revert(self, state: ComponentState) -> None:
        if self._saved_group is not None:
            state.layout.groups[self.group_id] = self._saved_group
        if self._saved_parent_id is not None:
            parent = state.layout.groups.get(self._saved_parent_id)
            if parent is not None:
                state.layout.groups[self._saved_parent_id] = _replace_group(
                    parent, child_group_ids=parent.child_group_ids | {self.group_id}
                )


@dataclass
class AddToGroupCommand(Command[ComponentState]):
    """Add component IDs and/or child group IDs to an existing group."""

    group_id: uuid.UUID
    component_ids: frozenset[uuid.UUID] = field(default_factory=frozenset)
    child_group_ids: frozenset[uuid.UUID] = field(default_factory=frozenset)
    _old_group: ComponentGroup | None = field(
        default=None, init=False, repr=False
    )

    @property
    def description(self) -> str:  # type: ignore[override]
        return "Add to group"

    def apply(self, state: ComponentState) -> None:
        group = state.layout.groups.get(self.group_id)
        if group is None:
            raise KeyError(f"Group {self.group_id} not found")
        # Validate no cycles.
        for child_id in self.child_group_ids:
            if would_create_cycle(state.layout.groups, self.group_id, child_id):
                raise ValueError(
                    f"Adding group {child_id} to {self.group_id} would create a cycle"
                )
        object.__setattr__(self, "_old_group", group)
        state.layout.groups[self.group_id] = _replace_group(
            group,
            component_ids=group.component_ids | self.component_ids,
            child_group_ids=group.child_group_ids | self.child_group_ids,
        )

    def revert(self, state: ComponentState) -> None:
        if self._old_group is not None:
            state.layout.groups[self.group_id] = self._old_group


@dataclass
class RemoveFromGroupCommand(Command[ComponentState]):
    """Remove component IDs and/or child group IDs from a group."""

    group_id: uuid.UUID
    component_ids: frozenset[uuid.UUID] = field(default_factory=frozenset)
    child_group_ids: frozenset[uuid.UUID] = field(default_factory=frozenset)
    _old_group: ComponentGroup | None = field(
        default=None, init=False, repr=False
    )

    @property
    def description(self) -> str:  # type: ignore[override]
        return "Remove from group"

    def apply(self, state: ComponentState) -> None:
        group = state.layout.groups.get(self.group_id)
        if group is None:
            raise KeyError(f"Group {self.group_id} not found")
        object.__setattr__(self, "_old_group", group)
        state.layout.groups[self.group_id] = _replace_group(
            group,
            component_ids=group.component_ids - self.component_ids,
            child_group_ids=group.child_group_ids - self.child_group_ids,
        )

    def revert(self, state: ComponentState) -> None:
        if self._old_group is not None:
            state.layout.groups[self.group_id] = self._old_group


@dataclass
class RenameGroupCommand(Command[ComponentState]):
    group_id: uuid.UUID
    new_name: str
    _old_name: str = field(default="", init=False, repr=False)

    @property
    def description(self) -> str:  # type: ignore[override]
        return f"Rename group to '{self.new_name}'"

    def apply(self, state: ComponentState) -> None:
        group = state.layout.groups.get(self.group_id)
        if group is None:
            raise KeyError(f"Group {self.group_id} not found")
        object.__setattr__(self, "_old_name", group.name)
        state.layout.groups[self.group_id] = _replace_group(group, name=self.new_name)

    def revert(self, state: ComponentState) -> None:
        group = state.layout.groups.get(self.group_id)
        if group is not None:
            state.layout.groups[self.group_id] = _replace_group(group, name=self._old_name)


@dataclass
class SetExportedPinsCommand(Command[ComponentState]):
    """Atomically replace the exported_pins of a group (supports undo)."""

    group_id: uuid.UUID
    new_exported: frozenset[ExportedPin]
    _old_exported: frozenset[ExportedPin] = field(
        default_factory=frozenset, init=False, repr=False
    )

    @property
    def description(self) -> str:  # type: ignore[override]
        return "Set exported pins"

    def apply(self, state: ComponentState) -> None:
        group = state.layout.groups.get(self.group_id)
        if group is None:
            raise KeyError(f"Group {self.group_id} not found")
        object.__setattr__(self, "_old_exported", group.exported_pins)
        state.layout.groups[self.group_id] = _replace_group(
            group, exported_pins=self.new_exported
        )

    def revert(self, state: ComponentState) -> None:
        group = state.layout.groups.get(self.group_id)
        if group is not None:
            state.layout.groups[self.group_id] = _replace_group(
                group, exported_pins=self._old_exported
            )


@dataclass
class RotateGroupCommand(Command[ComponentState]):
    """Rotate an entire group as a rigid body around its origin (pad closest to top-left)."""

    group_id: uuid.UUID
    delta: int  # 90 or 270 (= -90)
    _old_placements: dict[uuid.UUID, object] = field(
        default_factory=dict, init=False, repr=False
    )
    _old_wire_paths: dict[uuid.UUID, list[Point]] = field(
        default_factory=dict, init=False, repr=False
    )
    _old_group: ComponentGroup | None = field(
        default=None, init=False, repr=False
    )

    @property
    def description(self) -> str:  # type: ignore[override]
        return "Rotate group"

    def apply(self, state: ComponentState) -> None:
        from protoboard.domain.layout.placement import ComponentPlacement
        from protoboard.domain.components.definition import ComponentDefinition
        from protoboard.domain.components.instance import ComponentInstance

        group = state.layout.groups.get(self.group_id)
        if group is None:
            return
        object.__setattr__(self, "_old_group", group)

        comp_ids = all_component_ids(state.layout.groups, self.group_id)
        origin = group.origin or _group_origin(
            comp_ids, state.layout.placements, state.instances, state.components
        )
        if origin is None:
            return

        # Snapshot placements for undo.
        old_pls: dict[uuid.UUID, object] = {}
        for cid in comp_ids:
            pl = state.layout.placements.get(cid)
            if pl is not None:
                old_pls[cid] = pl.copy()  # type: ignore[union-attr]
        object.__setattr__(self, "_old_placements", old_pls)

        # Build bbox from pre-rotation positions for wire identification.
        bbox = _group_bbox(comp_ids, state.layout.placements, state.instances, state.components)

        # Rotate each placement.
        for cid in comp_ids:
            pl = state.layout.placements.get(cid)
            if not isinstance(pl, ComponentPlacement):
                continue
            new_pl = pl.copy()
            new_pl.position = _rotate_point_around(pl.position, origin, self.delta)
            new_pl.rotation = (pl.rotation + self.delta) % 360
            state.layout.placements[cid] = new_pl

        # Rotate internal wires (all points inside pre-rotation bbox).
        old_wire_paths: dict[uuid.UUID, list[Point]] = {}
        for wire in state.layout.wires:
            if bbox is not None and _wire_is_internal(wire, bbox):
                old_wire_paths[wire.id] = list(wire.path)
                wire.path = [_rotate_point_around(p, origin, self.delta) for p in wire.path]
        object.__setattr__(self, "_old_wire_paths", old_wire_paths)

        # Update group metadata.
        new_rot = (group.rotation + self.delta) % 360
        state.layout.groups[self.group_id] = _replace_group(
            group, rotation=new_rot, origin=origin
        )

    def revert(self, state: ComponentState) -> None:
        from protoboard.domain.layout.placement import ComponentPlacement
        for cid, pl in self._old_placements.items():
            if isinstance(pl, ComponentPlacement):
                state.layout.placements[cid] = pl
        for wire in state.layout.wires:
            if wire.id in self._old_wire_paths:
                wire.path = self._old_wire_paths[wire.id]
        if self._old_group is not None:
            state.layout.groups[self.group_id] = self._old_group
