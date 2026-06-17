# SPDX-License-Identifier: MIT
"""Commands for creating, disbanding and modifying component groups."""

from __future__ import annotations

import uuid
from dataclasses import dataclass, field

from protoboard.domain.commands.base import Command, ComponentState
from protoboard.domain.layout.group import ComponentGroup, all_component_ids, would_create_cycle
from protoboard.domain.layout.group_tree import GroupTree
from protoboard.domain.schematic.net import PinRef
from protoboard.domain.schematic.schematic import Schematic
from protoboard.geometry import Point


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


@dataclass
class RotateSnapshot:
    """Undo data for one :func:`rotate_components_around` application.

    Holds the pivot + delta and the pre-rotation placements / wire paths so
    :func:`restore_rotation` can invert the operation exactly. Group metadata
    (rotation/origin) is the caller's concern — not stored here.
    """

    origin: Point
    delta: int
    old_placements: dict[uuid.UUID, object]
    old_wire_paths: dict[uuid.UUID, list[Point]]


def rotate_components_around(
    state: ComponentState,
    comp_ids: frozenset[uuid.UUID],
    origin: Point,
    delta: int,
    *,
    wire_ids: frozenset[uuid.UUID],
) -> RotateSnapshot:
    """Rotate *comp_ids*' placements (and *wire_ids*' geometry) around *origin*.

    The shared core of every rotate operation: each placement's position is
    rotated about ``origin`` and its ``rotation`` field bumped by ``delta``;
    each wire in ``wire_ids`` has its path, fragments and free-knot
    (``JointKnot`` / ``WirePinKnot``) endpoints rotated too. ``PinKnot``
    positions resolve lazily from the (already-rotated) placement, so they
    need no separate update.

    ``origin`` is an arbitrary pivot — a component rotating about its own
    position (``origin == placement.position``) leaves its position put and
    only bumps ``rotation``; a group passes its group origin. The returned
    :class:`RotateSnapshot` is consumed by :func:`restore_rotation` on undo.
    """
    from protoboard.domain.layout.knot import JointKnot, WirePinKnot
    from protoboard.domain.layout.placement import ComponentPlacement

    old_placements: dict[uuid.UUID, object] = {}
    for cid in comp_ids:
        pl = state.layout.placements.get(cid)
        if not isinstance(pl, ComponentPlacement):
            continue
        old_placements[cid] = pl.copy()
        new_pl = pl.copy()
        new_pl.position = _rotate_point_around(pl.position, origin, delta)
        new_pl.rotation = (pl.rotation + delta) % 360
        state.layout.placements[cid] = new_pl

    old_wire_paths: dict[uuid.UUID, list[Point]] = {}
    for wire in state.layout.wires:
        if wire.id not in wire_ids:
            continue
        old_wire_paths[wire.id] = list(wire.path)
        wire.path = [_rotate_point_around(p, origin, delta) for p in wire.path]
        new_fragments_local: list[object] = []
        for frag in wire.fragments:
            new_frag_path = [
                _rotate_point_around(p, origin, delta) for p in frag.path
            ]
            new_fragments_local.append(
                type(frag)(path=new_frag_path, side=frag.side),
            )
        wire.fragments = new_fragments_local  # type: ignore[assignment]
        for ref in (wire.a, wire.b):
            if ref is None:
                continue
            k = state.layout.knots.get(ref.id)
            if isinstance(k, (JointKnot, WirePinKnot)):
                state.layout.knots[ref.id] = k.moved_to(
                    _rotate_point_around(k.position, origin, delta)
                )

    return RotateSnapshot(
        origin=origin,
        delta=delta,
        old_placements=old_placements,
        old_wire_paths=old_wire_paths,
    )


def restore_rotation(state: ComponentState, snap: RotateSnapshot) -> None:
    """Invert a :func:`rotate_components_around` application.

    Restores placements from the snapshot and inverse-rotates the recorded
    wires' fragments and free-knot endpoints by ``-delta`` around the same
    pivot (paths are restored verbatim from the snapshot).
    """
    from protoboard.domain.layout.knot import JointKnot, WirePinKnot
    from protoboard.domain.layout.placement import ComponentPlacement

    for cid, pl in snap.old_placements.items():
        if isinstance(pl, ComponentPlacement):
            state.layout.placements[cid] = pl

    inv_delta = (360 - snap.delta) % 360
    for wire in state.layout.wires:
        if wire.id not in snap.old_wire_paths:
            continue
        wire.path = snap.old_wire_paths[wire.id]
        new_fragments_local: list[object] = []
        for frag in wire.fragments:
            reverted = [
                _rotate_point_around(p, snap.origin, inv_delta)
                for p in frag.path
            ]
            new_fragments_local.append(
                type(frag)(path=reverted, side=frag.side),
            )
        wire.fragments = new_fragments_local  # type: ignore[assignment]
        for ref in (wire.a, wire.b):
            if ref is None:
                continue
            k = state.layout.knots.get(ref.id)
            if isinstance(k, (JointKnot, WirePinKnot)):
                state.layout.knots[ref.id] = k.moved_to(
                    _rotate_point_around(k.position, snap.origin, inv_delta)
                )


def _recompute_group_origin(
    state: "ComponentState", group_id: uuid.UUID,
) -> "Point | None":
    """Snapshot the current group's origin, then recompute it from the
    recursive component bbox top-left and replace the group with the new
    value.  Returns the *old* origin so the caller can stash it for
    revert.

    No-op (returns None) when the group is missing or has no resolvable
    pins.  Calling this after every membership/placement mutation keeps
    ``group.origin`` aligned with the bbox top-left invariant.

    Drag follows automatically: every moved component triggers
    recompute, the new top-left bbox is the translated old top-left,
    so the origin moves by the drag delta.  Rotate is NOT a caller —
    rotate must keep the pre-rotation origin as the immutable pivot.
    """
    group = state.layout.groups.get(group_id)
    if group is None:
        return None
    comp_ids = all_component_ids(state.layout.groups, group_id)
    if not comp_ids:
        return group.origin
    # Tests may pass a minimal ComponentState stub that lacks instances
    # or components — in that case we can't resolve pin positions, so
    # the helper is a no-op (returns current origin).  Production callers
    # always have a fully-populated state.
    instances = getattr(state, "instances", None)
    components = getattr(state, "components", None)
    if instances is None or components is None:
        return group.origin
    new_origin = GroupTree.group_origin(
        comp_ids,
        state.layout.placements,
        instances,
        components,
    )
    if new_origin is None:
        # No resolvable pin (placements without instances, broken refs).
        # Leave origin as-is to avoid wiping a value that's still useful
        # for rendering / hit-test.
        return group.origin
    old_origin = group.origin
    if old_origin is not None and (
        old_origin.x == new_origin.x and old_origin.y == new_origin.y
    ):
        # Unchanged — skip the dict mutation entirely to keep
        # CommandStack and downstream caches quiet.
        return old_origin
    state.layout.groups[group_id] = group.replace(origin=new_origin)
    return old_origin


@dataclass
class CreateGroupCommand(Command[ComponentState]):
    """Create a new group from a set of component IDs."""

    group: ComponentGroup
    # Snapshot of self.group at first apply so revert (and redo) put back
    # the same object the caller built — second apply gets origin sync'd
    # in-place but revert restores the pre-sync form.  Marginal but keeps
    # undo bit-identical.
    _orig_group: "ComponentGroup | None" = field(
        default=None, init=False, repr=False
    )

    @property
    def description(self) -> str:  # type: ignore[override]
        return f"Create group '{self.group.name}'"

    def apply(self, state: ComponentState) -> None:
        if self._orig_group is None:
            object.__setattr__(self, "_orig_group", self.group)
        state.layout.groups[self.group.id] = self.group
        # Sync origin to the bbox top-left of the new group's current
        # placements.  Callers (PPM "create group") usually pass origin=
        # None expecting the command to materialise it; this is the
        # canonical place to do so.
        _recompute_group_origin(state, self.group.id)
        # Pick up any in-place mutation so subsequent applies (redo) see
        # the synced origin.
        synced = state.layout.groups.get(self.group.id)
        if synced is not None:
            object.__setattr__(self, "group", synced)

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
                state.layout.groups[gid] = g.replace(
                    child_group_ids=g.child_group_ids - {self.group_id}
                )
                break

    def revert(self, state: ComponentState) -> None:
        if self._saved_group is not None:
            state.layout.groups[self.group_id] = self._saved_group
        if self._saved_parent_id is not None:
            parent = state.layout.groups.get(self._saved_parent_id)
            if parent is not None:
                state.layout.groups[self._saved_parent_id] = parent.replace(
                    child_group_ids=parent.child_group_ids | {self.group_id}
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
        state.layout.groups[self.group_id] = group.replace(
            component_ids=group.component_ids | self.component_ids,
            child_group_ids=group.child_group_ids | self.child_group_ids,
        )
        # Membership change ⇒ bbox top-left moves ⇒ origin recompute.
        # Old origin is captured inside _old_group above; revert restores it.
        _recompute_group_origin(state, self.group_id)

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
        state.layout.groups[self.group_id] = group.replace(
            component_ids=group.component_ids - self.component_ids,
            child_group_ids=group.child_group_ids - self.child_group_ids,
        )
        # Membership change ⇒ bbox top-left may change ⇒ origin recompute.
        # Old origin is captured inside _old_group above; revert restores it.
        _recompute_group_origin(state, self.group_id)

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
        state.layout.groups[self.group_id] = group.replace(name=self.new_name)

    def revert(self, state: ComponentState) -> None:
        group = state.layout.groups.get(self.group_id)
        if group is not None:
            state.layout.groups[self.group_id] = group.replace(name=self._old_name)


@dataclass
class SetExportedPinsCommand(Command[ComponentState]):
    """Atomically replace the exported_pins of a group (supports undo)."""

    group_id: uuid.UUID
    new_exported: frozenset[PinRef]
    _old_exported: frozenset[PinRef] = field(
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
        state.layout.groups[self.group_id] = group.replace(
            exported_pins=self.new_exported
        )

    def revert(self, state: ComponentState) -> None:
        group = state.layout.groups.get(self.group_id)
        if group is not None:
            state.layout.groups[self.group_id] = group.replace(
                exported_pins=self._old_exported
            )


@dataclass
class RotateGroupCommand(Command[ComponentState]):
    """Rotate an entire group as a rigid body around its origin (pad closest to top-left)."""

    group_id: uuid.UUID
    delta: int  # 90 or 270 (= -90)
    _snapshot: RotateSnapshot | None = field(
        default=None, init=False, repr=False
    )
    _old_group: ComponentGroup | None = field(
        default=None, init=False, repr=False
    )

    @property
    def description(self) -> str:  # type: ignore[override]
        return "Rotate group"

    def apply(self, state: ComponentState) -> None:
        group = state.layout.groups.get(self.group_id)
        if group is None:
            return
        object.__setattr__(self, "_old_group", group)

        comp_ids = all_component_ids(state.layout.groups, self.group_id)
        origin = group.origin or GroupTree.group_origin(
            comp_ids, state.layout.placements, state.instances, state.components
        )
        if origin is None:
            return

        # Identify internal wires via the net-aware ownership classifier.
        # Rotate doesn't need T-junction net inheritance (empty
        # wire_net_map); both-PinKnots-in-group covers the practical cases,
        # and the geometric sanity check (placements/instances/definitions)
        # filters stale paths.
        from protoboard.domain.layout.group_ownership import find_owned_wires

        schematic_obj = getattr(state, "schematic", None)
        sch: "Schematic | None" = (
            schematic_obj if isinstance(schematic_obj, Schematic) else None
        )
        internal_wire_ids = find_owned_wires(
            list(state.layout.wires),
            group,
            wire_net_map={},
            knots=state.layout.knots,
            group_comp_ids=comp_ids,
            schematic=sch,
            placements=state.layout.placements,
            instances=state.instances,
            definitions=state.components,
        )

        # Rotate placements + internal wires around the group origin.
        snapshot = rotate_components_around(
            state, comp_ids, origin, self.delta,
            wire_ids=frozenset(internal_wire_ids),
        )
        object.__setattr__(self, "_snapshot", snapshot)

        # Update group metadata.
        new_rot = (group.rotation + self.delta) % 360
        state.layout.groups[self.group_id] = group.replace(
            rotation=new_rot, origin=origin
        )

    def revert(self, state: ComponentState) -> None:
        if self._snapshot is not None:
            restore_rotation(state, self._snapshot)
        if self._old_group is not None:
            state.layout.groups[self.group_id] = self._old_group


@dataclass
class RotateComponentsCommand(Command[ComponentState]):
    """Rotate one or more loose components, each around its OWN position.

    The component analogue of :class:`RotateGroupCommand`: every component
    pivots about its own placement position, so positions stay put and only
    each ``rotation`` field bumps by ``delta`` (the same shared core,
    ``rotate_components_around``, with ``origin = placement.position``).
    Owned wires are not rotated — matching the historic loose-rotation
    behaviour; rotating them is a deliberate future option.
    """

    comp_ids: frozenset[uuid.UUID]
    delta: int
    _snapshots: list[RotateSnapshot] = field(
        default_factory=list, init=False, repr=False
    )

    @property
    def description(self) -> str:  # type: ignore[override]
        n = len(self.comp_ids)
        return f"Rotate {n} component{'s' if n != 1 else ''}"

    def apply(self, state: ComponentState) -> None:
        snapshots: list[RotateSnapshot] = []
        for cid in self.comp_ids:
            pl = state.layout.placements.get(cid)
            if pl is None:
                continue
            snapshots.append(rotate_components_around(
                state, frozenset({cid}), pl.position, self.delta,
                wire_ids=frozenset(),
            ))
        object.__setattr__(self, "_snapshots", snapshots)

    def revert(self, state: ComponentState) -> None:
        for snap in reversed(self._snapshots):
            restore_rotation(state, snap)
