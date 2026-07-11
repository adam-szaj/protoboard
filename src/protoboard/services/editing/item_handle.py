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
from typing import TYPE_CHECKING, Literal
from uuid import UUID

if TYPE_CHECKING:
    from protoboard.geometry import Point
    from protoboard.services.editing.controller import EditController

ItemTag = Literal["component", "group", "wire"]


@dataclass(frozen=True, slots=True)
class ItemId:
    """A tagged edit target: a ``tag`` (component / group / wire) + its uuid.

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
        return cls("component", item_uuid)

    @classmethod
    def group(cls, item_uuid: UUID) -> ItemId:
        return cls("group", item_uuid)

    @classmethod
    def wire(cls, item_uuid: UUID) -> ItemId:
        return cls("wire", item_uuid)

    @classmethod
    def parse(cls, text: str) -> ItemId:
        """Parse a ``"<tag>:<uuid>"`` string back into an :class:`ItemId`."""
        tag, _, rest = text.partition(":")
        if tag not in ("component", "group", "wire"):
            raise ValueError(f"bad ItemId tag: {tag!r}")
        return cls(tag, UUID(rest))  # type: ignore[arg-type]


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
    ) -> None:
        self._controller = controller
        self._group_ids = group_ids
        self._comp_ids = comp_ids
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


class ComponentHandle:
    """An editable single component — supports rotate / flip / delete."""

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


class WireHandle:
    """An addressed wire — rotate / flip are not defined for it (absent by design)."""

    def __init__(self, controller: EditController, wire_id: UUID) -> None:
        self._controller = controller
        self._wire_id = wire_id


__all__ = [
    "ComponentHandle",
    "GroupHandle",
    "ItemId",
    "ItemTag",
    "SelectionHandle",
    "WireHandle",
]
