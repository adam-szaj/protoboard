# SPDX-License-Identifier: MIT
"""Commands for creating, disbanding and modifying component groups."""

from __future__ import annotations

import uuid
from dataclasses import dataclass, field
from typing import TYPE_CHECKING, Literal

from protoboard.domain.commands.base import Command, ComponentState
from protoboard.domain.layout.group import ComponentGroup, all_component_ids, would_create_cycle
from protoboard.domain.layout.group_tree import GroupTree
from protoboard.domain.layout.wire import transform_wire_geometry
from protoboard.domain.schematic.net import PinRef
from protoboard.domain.schematic.schematic import Schematic
from protoboard.geometry import Point, Transform2D, Vector

if TYPE_CHECKING:
    from protoboard.domain.layout.placement import ComponentPlacement


def _rotation_about(origin: Point, delta: int) -> Transform2D:
    """Clockwise rotation by *delta* degrees about *origin* as one matrix.

    ``translate(origin) ∘ R(delta) ∘ translate(-origin)`` — a point rotates
    about an arbitrary pivot, wires and knots rotate through the same transform.
    """
    o = Vector(origin.x, origin.y)
    return (
        Transform2D.translation(o)
        .compose(Transform2D.rotation(delta // 90))
        .compose(Transform2D.translation(-o))
    )


# Board-flip axis for a group: 'v' reflects across a VERTICAL line (mirrors
# left↔right, the x coordinate); 'h' across a HORIZONTAL line (mirrors top↔
# bottom, the y coordinate).  Matches the user's [v]/[h] FlipMode choice.
FlipAxis = Literal["v", "h"]


def _reflection_about(pivot: Point, axis: FlipAxis) -> Transform2D:
    """Reflection across the *axis* line through *pivot* as one matrix.

    ``translate(pivot) ∘ M ∘ translate(-pivot)`` where ``M`` negates x (vertical
    axis) or y (horizontal axis).  ``determinant() == -1`` — the chirality flip a
    physical board-side flip introduces, which propagates to every group member
    through the compose-on-read seam.
    """
    p = Vector(pivot.x, pivot.y)
    m = (
        Transform2D.reflection()  # negate x → mirror across a vertical line
        if axis == "v"
        else Transform2D(1.0, 0.0, 0.0, -1.0, 0.0, 0.0)  # negate y
    )
    return (
        Transform2D.translation(p)
        .compose(m)
        .compose(Transform2D.translation(-p))
    )


@dataclass
class RotateSnapshot:
    """Undo data for one :func:`rotate_components_around` application.

    Holds the pivot + delta and the pre-rotation placements / wire paths so
    :func:`restore_rotation` can invert the operation exactly. Group metadata
    (rotation/origin) is the caller's concern — not stored here.

    Shared by three commands. The loose-rotation commands
    (:class:`RotateComponentsCommand`, :class:`RotateAroundPointCommand`) pass a
    non-empty ``comp_ids`` and so populate ``old_placements``. The scene-graph
    :class:`RotateGroupCommand` passes an empty ``comp_ids`` (members rotate via
    the group transform on read, not by baking), so its ``old_placements`` is
    always empty and only ``old_wire_paths`` carries state.
    """

    origin: Point
    delta: int
    old_placements: dict[uuid.UUID, ComponentPlacement]
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

    rotate = _rotation_about(origin, delta)
    old_placements: dict[uuid.UUID, ComponentPlacement] = {}
    for cid in comp_ids:
        pl = state.layout.placements.get(cid)
        if not isinstance(pl, ComponentPlacement):
            continue
        old_placements[cid] = pl.copy()
        new_pl = pl.copy()
        new_pl.position = rotate.apply(pl.position)
        new_pl.rotation = (pl.rotation + delta) % 360
        state.layout.placements[cid] = new_pl

    old_wire_paths: dict[uuid.UUID, list[Point]] = {}
    for wire in state.layout.wires:
        if wire.id not in wire_ids:
            continue
        old_wire_paths[wire.id] = list(wire.path)
        transform_wire_geometry(wire, rotate.apply)
        for ref in (wire.a, wire.b):
            if ref is None:
                continue
            k = state.layout.knots.get(ref.id)
            if isinstance(k, (JointKnot, WirePinKnot)):
                state.layout.knots[ref.id] = k.moved_to(
                    rotate.apply(k.position)
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
    from protoboard.domain.layout.placement import restore_placements

    restore_placements(state.layout.placements, snap.old_placements)

    unrotate = _rotation_about(snap.origin, snap.delta).inverse()
    for wire in state.layout.wires:
        if wire.id not in snap.old_wire_paths:
            continue
        # Path is restored verbatim from the snapshot; fragments are
        # inverse-rotated (their vertices weren't snapshotted).
        transform_wire_geometry(wire, unrotate.apply)
        wire.path = snap.old_wire_paths[wire.id]
        for ref in (wire.a, wire.b):
            if ref is None:
                continue
            k = state.layout.knots.get(ref.id)
            if isinstance(k, (JointKnot, WirePinKnot)):
                state.layout.knots[ref.id] = k.moved_to(
                    unrotate.apply(k.position)
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
            self._orig_group = self.group
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
            self.group = synced

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
        self._saved_group = group
        # Find and patch parent group that references this group.
        for gid, g in state.layout.groups.items():
            if self.group_id in g.child_group_ids:
                self._saved_parent_id = gid
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
class GroupMembershipCommand(Command[ComponentState]):
    """Base for the add/remove-membership commands.

    ``AddToGroupCommand`` and ``RemoveFromGroupCommand`` share one skeleton:
    look the group up (raise if missing), snapshot it for undo, swap in a
    group with recomputed membership, recompute the bbox-top-left origin, and
    on revert restore the snapshot.  They differ only in how the new member
    sets are formed (union ``|`` vs difference ``-``) and in whether a
    pre-mutation check runs (Add rejects cycles; Remove can't create one).

    Subclasses override :meth:`_new_membership` (the only required hook) and
    optionally :meth:`_validate`.  ``_old_group`` is ``init=False`` so it
    stays out of subclass ``__init__`` signatures.
    """

    group_id: uuid.UUID
    component_ids: frozenset[uuid.UUID] = field(default_factory=frozenset)
    child_group_ids: frozenset[uuid.UUID] = field(default_factory=frozenset)
    _old_group: ComponentGroup | None = field(
        default=None, init=False, repr=False
    )

    def _new_membership(
        self, group: ComponentGroup
    ) -> tuple[frozenset[uuid.UUID], frozenset[uuid.UUID]]:
        """Return the (component_ids, child_group_ids) to write into the group."""
        raise NotImplementedError

    def _validate(self, state: ComponentState, group: ComponentGroup) -> None:
        """Pre-mutation check. Default no-op; Add overrides to reject cycles."""

    def apply(self, state: ComponentState) -> None:
        group = state.layout.groups.get(self.group_id)
        if group is None:
            raise KeyError(f"Group {self.group_id} not found")
        self._validate(state, group)
        self._old_group = group
        new_components, new_children = self._new_membership(group)
        state.layout.groups[self.group_id] = group.replace(
            component_ids=new_components,
            child_group_ids=new_children,
        )
        # Membership change ⇒ bbox top-left may move ⇒ origin recompute.
        # Old origin is captured inside _old_group above; revert restores it.
        _recompute_group_origin(state, self.group_id)

    def revert(self, state: ComponentState) -> None:
        if self._old_group is not None:
            state.layout.groups[self.group_id] = self._old_group


@dataclass
class AddToGroupCommand(GroupMembershipCommand):
    """Add component IDs and/or child group IDs to an existing group."""

    @property
    def description(self) -> str:  # type: ignore[override]
        return "Add to group"

    def _validate(self, state: ComponentState, group: ComponentGroup) -> None:
        for child_id in self.child_group_ids:
            if would_create_cycle(state.layout.groups, self.group_id, child_id):
                raise ValueError(
                    f"Adding group {child_id} to {self.group_id} would create a cycle"
                )

    def _new_membership(
        self, group: ComponentGroup
    ) -> tuple[frozenset[uuid.UUID], frozenset[uuid.UUID]]:
        return (
            group.component_ids | self.component_ids,
            group.child_group_ids | self.child_group_ids,
        )


@dataclass
class RemoveFromGroupCommand(GroupMembershipCommand):
    """Remove component IDs and/or child group IDs from a group."""

    @property
    def description(self) -> str:  # type: ignore[override]
        return "Remove from group"

    def _new_membership(
        self, group: ComponentGroup
    ) -> tuple[frozenset[uuid.UUID], frozenset[uuid.UUID]]:
        return (
            group.component_ids - self.component_ids,
            group.child_group_ids - self.child_group_ids,
        )


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
        self._old_name = group.name
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
        self._old_exported = group.exported_pins
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
        self._old_group = group

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

        # Scene-graph rotate: the group's own ``transform`` gains the rotation
        # about ``origin``; member placements stay group-LOCAL and pick up the
        # rotation on read through the compose-on-read seam (verified: the
        # composed world pin positions are bit-identical to the previous
        # bake-into-placements behaviour).  Member ``comp_ids`` is therefore
        # empty here — ``rotate_components_around`` only rotates the owned
        # WIRES, whose ``path``/``fragments``/free-knots are still absolute and
        # do NOT compose through the seam, so they must be rotated in place to
        # stay attached to the (now-rotated) pins.  Wires becoming group-local
        # is a separate follow-on (docs plan, Phase 6+).  Consequence for undo:
        # with empty ``comp_ids`` the returned ``RotateSnapshot.old_placements``
        # is empty — the snapshot holds only wire geometry, and revert restores
        # the group pose by putting back ``_old_group`` (which carries the old
        # transform), not by rewinding baked member coords.
        #
        # ``find_owned_wires`` and ``group_origin`` above read placements
        # through the seam, so they must run BEFORE the transform changes — at
        # that point the seam still returns the pre-rotation world positions,
        # matching the pre-rotation wire paths.
        snapshot = rotate_components_around(
            state, frozenset(), origin, self.delta,
            wire_ids=frozenset(internal_wire_ids),
        )
        self._snapshot = snapshot

        # Fold the rotation into the group transform (the scene-graph source of
        # truth) and keep the legacy ``rotation``/``origin`` metadata in sync —
        # both stay authoritative during the FLATTEN→scene-graph transition
        # (docs plan); readers still consult ``origin`` for hit-test/render.
        new_transform = _rotation_about(origin, self.delta).compose(
            group.transform
        )
        new_rot = (group.rotation + self.delta) % 360
        state.layout.groups[self.group_id] = group.replace(
            transform=new_transform, rotation=new_rot, origin=origin
        )

    def revert(self, state: ComponentState) -> None:
        if self._snapshot is not None:
            restore_rotation(state, self._snapshot)
        if self._old_group is not None:
            state.layout.groups[self.group_id] = self._old_group


def _group_pin_centroid(
    state: ComponentState, comp_ids: frozenset[uuid.UUID]
) -> "Point | None":
    """Arithmetic mean of every resolvable pin world position in *comp_ids*.

    The default flip axis passes through this centroid (the user's "środek
    arytmetyczny pinów grupy").  ``None`` when no pin resolves (empty group /
    unbound instances) — the caller then has no axis and no-ops.
    """
    pins = GroupTree.pin_world_positions(
        comp_ids, state.layout.placements, state.instances, state.components,
    )
    if not pins:
        return None
    n = len(pins)
    return Point(
        sum(x for x, _ in pins) / n,
        sum(y for _, y in pins) / n,
    )


def _flip_group_wires(
    state: ComponentState,
    wire_ids: frozenset[uuid.UUID],
    reflect: Transform2D,
) -> None:
    """Reflect every wire in *wire_ids* through *reflect* and toggle its side.

    A group flip is a physical board flip: owned wires mirror their geometry AND
    move to the other side (:func:`flip_wire_geometry`), and free-knot
    (``JointKnot`` / ``WirePinKnot``) endpoints reflect too.  ``PinKnot``
    positions resolve lazily from the (now-composed) placements, so they need no
    separate update.  Applying twice with the same ``reflect`` is the identity —
    the reflection and the double side-swap both self-cancel — so undo re-applies
    the same call.
    """
    from protoboard.domain.layout.knot import JointKnot, WirePinKnot
    from protoboard.domain.layout.wire import flip_wire_geometry

    for wire in state.layout.wires:
        if wire.id not in wire_ids:
            continue
        flip_wire_geometry(wire, reflect.apply)
        for ref in (wire.a, wire.b):
            if ref is None:
                continue
            k = state.layout.knots.get(ref.id)
            if isinstance(k, (JointKnot, WirePinKnot)):
                state.layout.knots[ref.id] = k.moved_to(reflect.apply(k.position))


@dataclass
class FlipGroupCommand(Command[ComponentState]):
    """Flip an entire group about a horizontal or vertical axis (board-side flip).

    The flip analogue of :class:`RotateGroupCommand`: it folds a reflection
    (``det = -1``) into the group's ``transform`` — every member picks up the
    mirror + a board-side toggle on read through the compose-on-read seam — and
    physically flips the owned wires (geometry mirrored, side swapped) so they
    stay attached to the mirrored pins.

    ``axis`` selects a vertical (``'v'``, mirror left↔right) or horizontal
    (``'h'``, mirror top↔bottom) flip line.  ``pivot`` places that line; when
    ``None`` it defaults to the arithmetic centroid of the group's pins (the
    user's default axis position, overridable in armed FlipMode).
    """

    group_id: uuid.UUID
    axis: FlipAxis
    pivot: "Point | None" = None
    _old_group: ComponentGroup | None = field(
        default=None, init=False, repr=False
    )
    _reflect: Transform2D | None = field(default=None, init=False, repr=False)
    _wire_ids: frozenset[uuid.UUID] = field(
        default_factory=frozenset, init=False, repr=False
    )

    @property
    def description(self) -> str:  # type: ignore[override]
        return "Flip group"

    def apply(self, state: ComponentState) -> None:
        group = state.layout.groups.get(self.group_id)
        if group is None:
            return
        self._old_group = group

        comp_ids = all_component_ids(state.layout.groups, self.group_id)
        pivot = self.pivot or _group_pin_centroid(state, comp_ids)
        if pivot is None:
            return
        reflect = _reflection_about(pivot, self.axis)
        self._reflect = reflect

        # Identify internal wires the same way rotate does — BEFORE the transform
        # changes, so the seam still reports the pre-flip world positions that
        # match the pre-flip wire paths.
        from protoboard.domain.layout.group_ownership import find_owned_wires

        schematic_obj = getattr(state, "schematic", None)
        sch: "Schematic | None" = (
            schematic_obj if isinstance(schematic_obj, Schematic) else None
        )
        self._wire_ids = frozenset(find_owned_wires(
            list(state.layout.wires),
            group,
            wire_net_map={},
            knots=state.layout.knots,
            group_comp_ids=comp_ids,
            schematic=sch,
            placements=state.layout.placements,
            instances=state.instances,
            definitions=state.components,
        ))

        # Physically flip the owned wires (they are absolute — they do not
        # compose through the seam), then fold the reflection into the group
        # transform so members mirror + side-toggle on read.
        _flip_group_wires(state, self._wire_ids, reflect)
        state.layout.groups[self.group_id] = group.replace(
            transform=reflect.compose(group.transform)
        )

    def revert(self, state: ComponentState) -> None:
        # The reflection is its own inverse: re-applying it to the wires undoes
        # the flip (geometry mirrors back, the double side-swap cancels).
        if self._reflect is not None:
            _flip_group_wires(state, self._wire_ids, self._reflect)
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
        self._snapshots = snapshots

    def revert(self, state: ComponentState) -> None:
        for snap in reversed(self._snapshots):
            restore_rotation(state, snap)


@dataclass
class RotateAroundPointCommand(Command[ComponentState]):
    """Rotate a set of components around an arbitrary user-picked pivot.

    Unlike :class:`RotateComponentsCommand` (each component spins around its
    own position), every component here pivots around the same external
    ``origin`` — so their positions move. Owned wires rotate with them
    (a wire soldered to a moved component must follow), classified the same
    way :class:`RotateGroupCommand` classifies its internal wires.

    The thin caller the rotate-around-origin core was designed for: it just
    supplies an arbitrary pivot and the affected component set.
    """

    comp_ids: frozenset[uuid.UUID]
    origin: Point
    delta: int
    _snapshot: RotateSnapshot | None = field(
        default=None, init=False, repr=False
    )

    @property
    def description(self) -> str:  # type: ignore[override]
        return "Rotate around point"

    def apply(self, state: ComponentState) -> None:
        if not self.comp_ids:
            return
        # Classify wires owned by the selected components so they rotate
        # with their parts.  Same net-aware classifier RotateGroupCommand
        # uses; a free selection has no real group, so pass a synthetic
        # ComponentGroup whose member set IS the selection — ownership is
        # decided purely by ``group_comp_ids`` (the ``group`` argument is
        # only carried for diagnostics by find_owned_wires).
        from protoboard.domain.layout.group_ownership import find_owned_wires

        schematic_obj = getattr(state, "schematic", None)
        sch: "Schematic | None" = (
            schematic_obj if isinstance(schematic_obj, Schematic) else None
        )
        synthetic_group = ComponentGroup(
            id=uuid.uuid4(),
            name="<rotate-around-point>",
            component_ids=self.comp_ids,
        )
        owned_wire_ids = find_owned_wires(
            list(state.layout.wires),
            synthetic_group,
            wire_net_map={},
            knots=state.layout.knots,
            group_comp_ids=self.comp_ids,
            schematic=sch,
            placements=state.layout.placements,
            instances=state.instances,
            definitions=state.components,
        )
        snapshot = rotate_components_around(
            state, self.comp_ids, self.origin, self.delta,
            wire_ids=frozenset(owned_wire_ids),
        )
        self._snapshot = snapshot

    def revert(self, state: ComponentState) -> None:
        if self._snapshot is not None:
            restore_rotation(state, self._snapshot)
