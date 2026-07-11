# SPDX-License-Identifier: MIT
"""ItemId + edit handles — the unified View↔Controller addressing seam.

An :class:`ItemId` is a tagged uuid (``component:<uuid>`` / ``group:<uuid>`` /
``wire:<uuid>``) — the first-class boundary form of what the GUI already carries
as ``Hit(kind, id)``. Both the GUI tools and the CLI address an edit target by
``ItemId`` and call the operation on the *handle* the controller hands back:

    controller.get_item(ItemId.group(gid)).rotate(90)
    controller.get_items([ItemId.group(g1), ItemId.group(g2)]).rotate(90)

A handle offers only the operations its target supports — a :class:`WireHandle`
has no ``rotate`` / ``flip``, so a wire target simply cannot be asked to rotate
(no empty method, no silent no-op). A :class:`SelectionHandle` folds a
heterogeneous set (groups + loose components) into ONE atomic macro; that is
what lets a two-group selection rotate as a single undoable step — the bug the
legacy single-``group_id`` verb signature could not express.

The handles are thin facades over :class:`EditController` — they translate an
addressed target into the same domain commands the verbs build; value objects
(placements, wires, groups) stay frozen and the command/undo model is unchanged.
"""

from __future__ import annotations

from dataclasses import dataclass
from enum import IntEnum
from typing import TYPE_CHECKING
from uuid import UUID

if TYPE_CHECKING:
    from protoboard.geometry import Point
    from protoboard.services.editing.controller import EditController


class ItemTag(IntEnum):
    """The kind of edit target an :class:`ItemId` addresses.

    An ``IntEnum`` (not a bare string): its members can index per-kind variant
    tables, and identity comparison (``is``) is exhaustiveness-checkable — while
    the lower-case member names keep ``str(ItemId)`` the human-readable
    ``"<name>:<uuid>"`` form.
    """

    component = 0
    group = 1
    wire = 2

    def __str__(self) -> str:
        return self.name

    @classmethod
    def parse(cls, name: str) -> ItemTag:
        """Map a name (``"component"`` / …) to its member."""
        try:
            return cls[name]
        except KeyError as e:
            raise ValueError(f"bad ItemId tag: {name!r}") from e


@dataclass(frozen=True, slots=True)
class ItemId:
    """A tagged edit target: an :class:`ItemTag` + its uuid.

    The string form is ``"<tag>:<uuid>"`` — a stable, human-readable handle the
    tag makes routable without probing every collection to discover what a bare
    uuid refers to.
    """

    tag: ItemTag
    uuid: UUID

    def __str__(self) -> str:
        return f"{self.tag}:{self.uuid}"

    @classmethod
    def component(cls, item_uuid: UUID) -> ItemId:
        return cls(ItemTag.component, item_uuid)

    @classmethod
    def group(cls, item_uuid: UUID) -> ItemId:
        return cls(ItemTag.group, item_uuid)

    @classmethod
    def wire(cls, item_uuid: UUID) -> ItemId:
        return cls(ItemTag.wire, item_uuid)

    @classmethod
    def parse(cls, text: str) -> ItemId:
        """Parse a ``"<tag>:<uuid>"`` string back into an :class:`ItemId`."""
        tag, _, rest = text.partition(":")
        return cls(ItemTag.parse(tag), UUID(rest))


class SelectionHandle:
    """A handle over a heterogeneous set of addressed items.

    Rotate / flip fold every addressed group (each a rigid-body transform) and
    the loose components (rotated together about a shared pivot, or each in
    place) into ONE macro, so the whole selection transforms as a single
    undoable step. Wires in the set are ignored by rotate / flip — the
    operation is not defined for them.
    """

    def __init__(
        self,
        controller: EditController,
        *,
        group_ids: frozenset[UUID],
        comp_ids: frozenset[UUID],
        active_group_id: UUID,
        wire_ids: frozenset[UUID] = frozenset(),
    ) -> None:
        self._controller = controller
        self._group_ids = group_ids
        self._comp_ids = comp_ids
        self._wire_ids = wire_ids
        self._active_group_id = active_group_id

    def rotate(
        self,
        delta_deg: int = 90,
        *,
        pivot: Point | None = None,
        remove_crossing_wires: bool = True,
    ) -> int:
        """Rotate every addressed group + loose component as one macro.

        Returns the number of wires dropped for crossing an alien pad. An
        explicit *pivot* rotates every affected component about that point;
        otherwise each group turns rigidly about its own origin and the loose
        components turn in place.
        """
        return self._controller._rotate_targets(
            group_ids=self._group_ids,
            comp_ids=self._comp_ids,
            delta_deg=delta_deg,
            pivot=pivot,
            active_group_id=self._active_group_id,
            remove_crossing_wires=remove_crossing_wires,
        )

    def flip(self, *, remove_crossing_wires: bool = True) -> int:
        """Flip every addressed group (rigid body) + loose 2-pin component."""
        return self._controller._flip_targets(
            group_ids=self._group_ids,
            comp_ids=self._comp_ids,
            active_group_id=self._active_group_id,
            remove_crossing_wires=remove_crossing_wires,
        )

    def delete(
        self, *, active_group_id: UUID | None = None, force: bool = False
    ) -> bool:
        """Delete the whole addressed set as one atomic macro.

        Components / wires are removed (subject to the group-lock guard against
        *active_group_id*, wires also honouring *force*); groups are disbanded
        (dissolve, members kept). *active_group_id* overrides the handle's
        context when given.
        """
        return self._controller._delete_targets(
            comp_ids=self._comp_ids,
            wire_ids=self._wire_ids,
            group_ids=self._group_ids,
            active_group_id=(
                active_group_id
                if active_group_id is not None
                else self._active_group_id
            ),
            force=force,
        )


class ComponentHandleBase:
    """Read-only view of a single component — value / part-number / pins / refdes.

    The shared base of :class:`ReadOnlyComponentHandle` (this and no more) and
    :class:`ComponentHandle` (this plus the mutating ops). Properties resolve
    against the live state each access, so they reflect the current instance.
    """

    def __init__(
        self,
        controller: EditController,
        comp_id: UUID,
        *,
        active_group_id: UUID,
    ) -> None:
        self._controller = controller
        self._comp_id = comp_id
        self._active_group_id = active_group_id

    @property
    def _instance(self) -> object | None:
        return self._controller.state.instances.get(self._comp_id)

    @property
    def value(self) -> str | None:
        """The instance value (e.g. ``"10k"``, ``"NE555"``), or None if gone."""
        inst = self._instance
        return getattr(inst, "value", None) if inst is not None else None

    @property
    def refdes(self) -> str | None:
        """The user-facing reference designator (e.g. ``"R1"``), or None."""
        inst = self._instance
        return getattr(inst, "refdes", None) if inst is not None else None

    @property
    def part_number(self) -> str | None:
        """The component definition reference (the part number), or None."""
        inst = self._instance
        return getattr(inst, "definition_ref", None) if inst is not None else None

    @property
    def pin_names(self) -> dict[int, str]:
        """Pin-number → name for the instance's chosen pin variant (may be empty)."""
        inst = self._instance
        if inst is None:
            return {}
        defn = self._controller.state.components.get(
            getattr(inst, "definition_ref", "")
        )
        if defn is None:
            return {}
        return dict(defn.effective_pin_names(getattr(inst, "pin_variant", None)))


class ReadOnlyComponentHandle(ComponentHandleBase):
    """A component addressed from *outside* the group that seals it.

    Exposes the read-only properties of :class:`ComponentHandleBase` and nothing
    more — rotate / flip / delete are absent by type. Stepping into the group
    (making it the active context) yields an editable :class:`ComponentHandle`
    from ``get_item`` instead.
    """


class ComponentHandle(ComponentHandleBase):
    """An editable single component — the read-only base plus rotate / flip / delete."""

    def rotate(
        self,
        delta_deg: int = 90,
        *,
        pivot: Point | None = None,
        remove_crossing_wires: bool = True,
    ) -> int:
        return self._controller._rotate_targets(
            group_ids=frozenset(),
            comp_ids=frozenset({self._comp_id}),
            delta_deg=delta_deg,
            pivot=pivot,
            active_group_id=self._active_group_id,
            remove_crossing_wires=remove_crossing_wires,
        )

    def flip(self, *, remove_crossing_wires: bool = True) -> int:
        return self._controller._flip_targets(
            group_ids=frozenset(),
            comp_ids=frozenset({self._comp_id}),
            active_group_id=self._active_group_id,
            remove_crossing_wires=remove_crossing_wires,
        )

    def delete(self) -> bool:
        """Remove this component (subject to the group-lock guard)."""
        return self._controller._delete_targets(
            comp_ids=frozenset({self._comp_id}),
            wire_ids=frozenset(),
            group_ids=frozenset(),
            active_group_id=self._active_group_id,
        )


class GroupHandle:
    """An addressed group — rotates / flips as a rigid body (an external op).

    A group turns like a transistor turns: the outer transform changes, its
    internals do not. No edit context / group-lock applies — you are not
    reaching *into* the group, you are moving it as one object.
    """

    def __init__(self, controller: EditController, group_id: UUID) -> None:
        self._controller = controller
        self._group_id = group_id

    def rotate(
        self,
        delta_deg: int = 90,
        *,
        pivot: Point | None = None,
        remove_crossing_wires: bool = True,
    ) -> int:
        return self._controller._rotate_targets(
            group_ids=frozenset({self._group_id}),
            comp_ids=frozenset(),
            delta_deg=delta_deg,
            pivot=pivot,
            active_group_id=self._group_id,
            remove_crossing_wires=remove_crossing_wires,
        )

    def flip(self, *, remove_crossing_wires: bool = True) -> int:
        return self._controller._flip_targets(
            group_ids=frozenset({self._group_id}),
            comp_ids=frozenset(),
            active_group_id=self._group_id,
            remove_crossing_wires=remove_crossing_wires,
        )

    def delete(self) -> bool:
        """Disband this group — dissolve it, keeping its members placed.

        The domain's only "remove a group" op; not a delete-members. No group
        lock applies — a group is addressed as one external object.
        """
        return self._controller._delete_targets(
            comp_ids=frozenset(),
            wire_ids=frozenset(),
            group_ids=frozenset({self._group_id}),
            active_group_id=self._group_id,
        )


class WireHandle:
    """An addressed wire — rotate / flip are not defined for it (absent by design).

    A wire supports :meth:`delete` only.
    """

    def __init__(self, controller: EditController, wire_id: UUID) -> None:
        self._controller = controller
        self._wire_id = wire_id

    def delete(
        self, *, active_group_id: UUID | None = None, force: bool = False
    ) -> bool:
        """Remove this wire (subject to the wire-lock guard, liftable by *force*)."""
        from protoboard.domain.layout.layout import ROOT_GROUP_ID

        return self._controller._delete_targets(
            comp_ids=frozenset(),
            wire_ids=frozenset({self._wire_id}),
            group_ids=frozenset(),
            active_group_id=(
                active_group_id if active_group_id is not None else ROOT_GROUP_ID
            ),
            force=force,
        )


__all__ = [
    "ComponentHandle",
    "ComponentHandleBase",
    "GroupHandle",
    "ItemId",
    "ItemTag",
    "ReadOnlyComponentHandle",
    "SelectionHandle",
    "WireHandle",
]
