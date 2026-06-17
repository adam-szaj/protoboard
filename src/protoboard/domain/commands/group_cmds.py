# SPDX-License-Identifier: MIT
"""Commands for creating, disbanding and modifying component groups."""

from __future__ import annotations

import uuid
from dataclasses import dataclass, field

import structlog

from protoboard.domain.commands.base import Command, ComponentState
from protoboard.domain.layout.group import ComponentGroup, all_component_ids, would_create_cycle
from protoboard.domain.layout.group_tree import GroupTree
from protoboard.domain.schematic.net import PinRef
from protoboard.domain.schematic.schematic import Schematic
from protoboard.geometry import Point

_log = structlog.get_logger(__name__)


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

        # Snapshot placements for undo.
        old_pls: dict[uuid.UUID, object] = {}
        for cid in comp_ids:
            pl = state.layout.placements.get(cid)
            if pl is not None:
                old_pls[cid] = pl.copy()
        object.__setattr__(self, "_old_placements", old_pls)

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

        # Rotate each placement.
        for cid in comp_ids:
            pl = state.layout.placements.get(cid)
            if not isinstance(pl, ComponentPlacement):
                continue
            new_pl = pl.copy()
            new_pl.position = _rotate_point_around(pl.position, origin, self.delta)
            new_pl.rotation = (pl.rotation + self.delta) % 360
            state.layout.placements[cid] = new_pl

        # Rotate internal wires.
        old_wire_paths: dict[uuid.UUID, list[Point]] = {}
        from protoboard.domain.layout.knot import JointKnot, WirePinKnot
        joint_resync_log: list[dict[str, object]] = []
        for wire in state.layout.wires:
            if wire.id in internal_wire_ids:
                old_wire_paths[wire.id] = list(wire.path)
                wire.path = [
                    _rotate_point_around(p, origin, self.delta)
                    for p in wire.path
                ]
                # Also rotate the wire's fragments so wire.path and
                # wire.fragments don't drift apart (later
                # replace_wire_fragments builds new_fragments from path —
                # mismatch with knot.position is then unavoidable).
                new_fragments_local: list[object] = []
                for frag in wire.fragments:
                    new_frag_path = [
                        _rotate_point_around(p, origin, self.delta)
                        for p in frag.path
                    ]
                    new_fragments_local.append(
                        type(frag)(path=new_frag_path, side=frag.side),
                    )
                wire.fragments = new_fragments_local  # type: ignore[assignment]
                # Rotate JointKnot / WirePinKnot endpoints together with
                # the wire.  Without this, a follow-up drag's
                # replace_wire_fragments would see path[0/-1] rotated
                # while the knot.position is still pre-rotation, raising
                # ValueError(replace_wire_fragments: new path[0]
                # disagrees with knot position).  PinKnot positions are
                # resolved lazily from placement (already rotated
                # above), so they need no separate update here.
                for ref in (wire.a, wire.b):
                    if ref is None:
                        continue
                    k = state.layout.knots.get(ref.id)
                    if isinstance(k, (JointKnot, WirePinKnot)):
                        old_kp = k.position
                        moved = k.moved_to(
                            _rotate_point_around(k.position, origin, self.delta)
                        )
                        state.layout.knots[ref.id] = moved
                        joint_resync_log.append({
                            "wire_id": str(wire.id)[:8],
                            "knot_kind": type(k).__name__,
                            "knot_id": str(k.id)[:8],
                            "old_pos": (old_kp.x, old_kp.y),
                            "new_pos": (moved.position.x, moved.position.y),
                            "n_owners": len(k.wire_ids),
                        })
        object.__setattr__(self, "_old_wire_paths", old_wire_paths)
        if joint_resync_log:
            _log.info(
                "rotate_group.joint_knots_rotated",
                group_id=str(self.group_id)[:8],
                delta=self.delta,
                n_resynced=len(joint_resync_log),
                details=joint_resync_log[:20],
            )

        # Update group metadata.
        new_rot = (group.rotation + self.delta) % 360
        state.layout.groups[self.group_id] = group.replace(
            rotation=new_rot, origin=origin
        )

    def revert(self, state: ComponentState) -> None:
        from protoboard.domain.layout.placement import ComponentPlacement
        from protoboard.domain.layout.knot import JointKnot, WirePinKnot
        for cid, pl in self._old_placements.items():
            if isinstance(pl, ComponentPlacement):
                state.layout.placements[cid] = pl
        # Rotate wire paths + JointKnot/WirePinKnot positions back by -delta.
        # Cheaper than snapshotting every knot — same _rotate_point_around
        # with delta=(360-self.delta) is the inverse.
        inv_delta = (360 - self.delta) % 360
        # Reconstruct origin same way apply did so the inverse rotation
        # uses the right pivot.
        comp_ids = all_component_ids(state.layout.groups, self.group_id)
        origin = (
            self._old_group.origin if self._old_group is not None else None
        ) or GroupTree.group_origin(
            comp_ids, state.layout.placements, state.instances, state.components,
        )
        for wire in state.layout.wires:
            if wire.id in self._old_wire_paths:
                wire.path = self._old_wire_paths[wire.id]
                # Reverse the fragment rotation too (apply rotated frag.path
                # so revert needs to undo it; otherwise next replace
                # mismatches path vs fragments).
                if origin is not None:
                    new_fragments_local: list[object] = []
                    for frag in wire.fragments:
                        reverted = [
                            _rotate_point_around(p, origin, inv_delta)
                            for p in frag.path
                        ]
                        new_fragments_local.append(
                            type(frag)(path=reverted, side=frag.side),
                        )
                    wire.fragments = new_fragments_local  # type: ignore[assignment]
                # Revert JointKnot/WirePinKnot positions for this wire's
                # endpoints — symmetric to apply().
                if origin is not None:
                    for ref in (wire.a, wire.b):
                        if ref is None:
                            continue
                        k = state.layout.knots.get(ref.id)
                        if isinstance(k, (JointKnot, WirePinKnot)):
                            state.layout.knots[ref.id] = k.moved_to(
                                _rotate_point_around(k.position, origin, inv_delta)
                            )
        if self._old_group is not None:
            state.layout.groups[self.group_id] = self._old_group
