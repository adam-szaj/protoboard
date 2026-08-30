# SPDX-License-Identifier: MIT
"""ItemId + edit handles — the unified View↔Controller addressing seam.

An :class:`ItemId` is a tagged uuid (``component:<uuid>`` / ``group:<uuid>`` /
``wire:<uuid>``) — the first-class boundary form of what the GUI already carries
as ``Hit(kind, id)``. Both the GUI tools and the CLI address an edit target by
``ItemId`` and call the operation on the *handle* the controller hands back:

    controller.get_item(ItemId.group(gid)).rotate(90)

A handle offers only the operations its target supports — a :class:`WireHandle`
has no ``rotate`` / ``flip``, so a wire target simply cannot be asked to rotate
(no empty method, no silent no-op). The :class:`HandleKind` predicates
(``is_group`` / ``is_readonly`` / ``supports_rotate`` / …) let a caller filter a
mixed set without ``isinstance``.

``controller.get_items([...])`` returns a **plain list** of these handles — one
per id, read-only handles included — for inspection and per-item work
(``[h for h in items if h.supports_rotate()]``). It is *not* an aggregate: the
atomic multi-target operation (several groups rotating as one undoable macro)
is the verb :meth:`EditController.rotate` / ``flip`` / ``delete``, which builds
that macro directly.

The handles are thin facades over :class:`EditController` — they translate an
addressed target into the same domain commands the verbs build; value objects
(placements, wires, groups) stay frozen and the command/undo model is unchanged.
"""

from __future__ import annotations

from abc import ABC, abstractmethod
from collections.abc import Callable
from dataclasses import dataclass
from enum import IntEnum
from typing import TYPE_CHECKING
from uuid import UUID

if TYPE_CHECKING:
    from protoboard.controller.editing.controller import EditController
    from protoboard.geometry import Point


def _require_one(*callbacks: object) -> None:
    """Guard: ``.call`` needs at least one non-None callback (else it is a bug)."""
    if all(cb is None for cb in callbacks):
        raise ValueError("call() needs at least one handler callback")


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


class HandleKind:
    """Type / capability predicates shared by every handle.

    ``get_items`` returns a plain ``list`` of these, so a caller filters and
    maps over the selection with ordinary comprehensions — ``[h for h in items
    if h.is_readonly()]``, ``map(rotate_it, filter(...))``. The predicates are
    the seam that keeps that caller from having to ``isinstance`` against four
    concrete handle classes.

    Two axes:

    * ``is_component`` / ``is_group`` / ``is_wire`` — the target's kind (exactly
      one is True). ``is_readonly`` / ``is_mutable`` — whether it offers *any*
      mutating op in the current edit context (a component sealed inside a group
      viewed from outside is read-only).
    * ``supports_rotate`` / ``supports_flip`` / ``supports_delete`` — capability
      sugar. Each is defined as ``isinstance(self, <capability base>)`` so the
      capability *type* (:class:`Transformable` / :class:`Deletable`) is the one
      source of truth: a handle supports rotate iff it is a ``Transformable``,
      full stop. No ``hasattr`` reflection, no per-class boolean to drift.

    The ``is_*`` kind predicates default to False here; each concrete handle
    flips on the ones it satisfies. So a new handle kind is safe-by-default
    (claims no kind until it opts in).
    """

    def is_component(self) -> bool:
        return False

    def is_group(self) -> bool:
        return False

    def is_wire(self) -> bool:
        return False

    def is_readonly(self) -> bool:
        return False

    def is_mutable(self) -> bool:
        return not self.is_readonly()

    def supports_rotate(self) -> bool:
        return isinstance(self, Transformable)

    def supports_flip(self) -> bool:
        return isinstance(self, Transformable)

    def supports_delete(self) -> bool:
        return isinstance(self, Deletable)


class Transformable(HandleKind, ABC):
    """A handle whose target can be rotated and flipped in place.

    The capability *type* the module-level :func:`rotate` / :func:`flip` narrow
    to (``isinstance(handle, Transformable)``) — so those helpers stay total over
    ``HandleKind`` while mypy still proves the call is safe, and a read-only or
    wire handle (neither a ``Transformable``) is simply skipped, no cast needed.
    """

    @abstractmethod
    def rotate(
        self,
        delta_deg: int = 90,
        *,
        pivot: Point | None = None,
        remove_crossing_wires: bool = True,
    ) -> int: ...

    @abstractmethod
    def flip(self, *, remove_crossing_wires: bool = True) -> int: ...


class Deletable(HandleKind, ABC):
    """A handle whose target can be deleted.

    Wider than :class:`Transformable`: a wire is ``Deletable`` but not
    ``Transformable``; a group's ``delete`` disbands it. ``force`` lifts the
    wire-lock guard (ignored by component / group handles, which have no such
    guard). The narrow target for the module-level :func:`delete`.
    """

    @abstractmethod
    def delete(self, *, force: bool = False) -> bool: ...


class ComponentHandleBase(HandleKind):
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

    def is_component(self) -> bool:
        return True

    def call(
        self,
        *,
        component_call: Callable[[ComponentHandleBase], object] | None = None,
        group_call: Callable[[GroupHandle], object] | None = None,
        wire_call: Callable[[WireHandle], object] | None = None,
    ) -> object:
        """Invoke the component callback (this handle's kind); others ignored.

        Per-type visitor: a component handle runs *component_call* with itself
        and returns its result; *group_call* / *wire_call* never fire here. A
        missing (None) *component_call* is a no-op returning None. At least one
        callback must be supplied.
        """
        _require_one(component_call, group_call, wire_call)
        return component_call(self) if component_call is not None else None

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
    def meta_pin_labels(self) -> dict[int, str]:
        """MetaPin ordinal → compact primary label."""
        inst = self._instance
        if inst is None:
            return {}
        defn = self._controller.state.components.get(
            getattr(inst, "definition_ref", "")
        )
        if defn is None:
            return {}
        return {
            index: meta_pin.labels[0]
            for index, meta_pin in enumerate(defn.meta_pins, start=1)
        }


class ReadOnlyComponentHandle(ComponentHandleBase):
    """A component addressed from *outside* the group that seals it.

    Exposes the read-only properties of :class:`ComponentHandleBase` and nothing
    more — rotate / flip / delete are absent by type. Stepping into the group
    (making it the active context) yields an editable :class:`ComponentHandle`
    from ``get_item`` instead.
    """

    def is_readonly(self) -> bool:
        return True


class ComponentHandle(ComponentHandleBase, Transformable, Deletable):
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

    def delete(self, *, force: bool = False) -> bool:
        """Remove this component (subject to the group-lock guard).

        *force* is part of the uniform :class:`Deletable` signature; it lifts the
        *wire*-lock only, so a component ignores it.
        """
        return self._controller._delete_targets(
            comp_ids=frozenset({self._comp_id}),
            wire_ids=frozenset(),
            group_ids=frozenset(),
            active_group_id=self._active_group_id,
        )


class GroupHandle(Transformable, Deletable):
    """An addressed group — rotates / flips as a rigid body (an external op).

    A group turns like a transistor turns: the outer transform changes, its
    internals do not. No edit context / group-lock applies — you are not
    reaching *into* the group, you are moving it as one object.
    """

    def __init__(self, controller: EditController, group_id: UUID) -> None:
        self._controller = controller
        self._group_id = group_id

    def is_group(self) -> bool:
        return True

    def call(
        self,
        *,
        component_call: Callable[[ComponentHandleBase], object] | None = None,
        group_call: Callable[[GroupHandle], object] | None = None,
        wire_call: Callable[[WireHandle], object] | None = None,
    ) -> object:
        """Invoke the group callback (this handle's kind); others ignored."""
        _require_one(component_call, group_call, wire_call)
        return group_call(self) if group_call is not None else None

    @property
    def _group(self) -> object | None:
        return self._controller.state.layout.groups.get(self._group_id)

    @property
    def name(self) -> str | None:
        """The group's name, or None if it is gone."""
        grp = self._group
        return getattr(grp, "name", None) if grp is not None else None

    @property
    def component_ids(self) -> frozenset[UUID]:
        """The group's direct component members (empty if the group is gone)."""
        grp = self._group
        if grp is None:
            return frozenset()
        return frozenset(getattr(grp, "component_ids", frozenset()))

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

    def delete(self, *, force: bool = False) -> bool:
        """Disband this group — dissolve it, keeping its members placed.

        The domain's only "remove a group" op; not a delete-members. No group
        lock applies — a group is addressed as one external object, so *force*
        (the wire-lock override) is irrelevant here.
        """
        return self._controller._delete_targets(
            comp_ids=frozenset(),
            wire_ids=frozenset(),
            group_ids=frozenset({self._group_id}),
            active_group_id=self._group_id,
        )


class WireHandle(Deletable):
    """An addressed wire — rotate / flip are not defined for it (absent by design).

    A wire supports :meth:`delete` plus the read properties ``side`` /
    ``insulated``. It is a :class:`Deletable` but not a :class:`Transformable`.
    """

    def __init__(self, controller: EditController, wire_id: UUID) -> None:
        self._controller = controller
        self._wire_id = wire_id

    def is_wire(self) -> bool:
        return True

    def call(
        self,
        *,
        component_call: Callable[[ComponentHandleBase], object] | None = None,
        group_call: Callable[[GroupHandle], object] | None = None,
        wire_call: Callable[[WireHandle], object] | None = None,
    ) -> object:
        """Invoke the wire callback (this handle's kind); others ignored."""
        _require_one(component_call, group_call, wire_call)
        return wire_call(self) if wire_call is not None else None

    @property
    def _wire(self) -> object | None:
        return self._controller.state.wire_by_id(self._wire_id)

    @property
    def side(self) -> str | None:
        """The board side (``"top"`` / ``"bottom"``) the wire lies on, or None."""
        wire = self._wire
        return getattr(wire, "side", None) if wire is not None else None

    @property
    def insulated(self) -> bool | None:
        """Whether the wire is an insulated jumper, or None if it is gone."""
        wire = self._wire
        return getattr(wire, "insulated", None) if wire is not None else None

    def delete(self, *, force: bool = False) -> bool:
        """Remove this wire (subject to the wire-lock guard, liftable by *force*)."""
        from protoboard.domain.layout.layout import ROOT_GROUP_ID

        return self._controller._delete_targets(
            comp_ids=frozenset(),
            wire_ids=frozenset({self._wire_id}),
            group_ids=frozenset(),
            active_group_id=ROOT_GROUP_ID,
            force=force,
        )


# ---------------------------------------------------------------------------
# Capability-narrowing helpers — total over ``HandleKind``, safe under mypy
# ---------------------------------------------------------------------------
#
# ``get_item(id)`` returns a union of four handle types, so ``get_item(id).
# rotate(90)`` cannot type-check — a ``WireHandle`` has no ``rotate``. These
# free functions take *any* ``HandleKind`` and narrow to the capability base
# with ``isinstance``; mypy proves the call safe inside the branch, and a
# handle that lacks the capability is a silent no-op (returns None). The
# untypeable union access stays hidden here, in the module, instead of at
# every call site: ``rotate(get_item(id), 90)``.


def rotate(
    handle: HandleKind,
    delta_deg: int = 90,
    *,
    pivot: Point | None = None,
    remove_crossing_wires: bool = True,
) -> int | None:
    """Rotate *handle* if its target supports it, else do nothing (returns None)."""
    if isinstance(handle, Transformable):
        return handle.rotate(
            delta_deg, pivot=pivot, remove_crossing_wires=remove_crossing_wires
        )
    return None


def flip(handle: HandleKind, *, remove_crossing_wires: bool = True) -> int | None:
    """Flip *handle* if its target supports it, else do nothing (returns None)."""
    if isinstance(handle, Transformable):
        return handle.flip(remove_crossing_wires=remove_crossing_wires)
    return None


def delete(handle: HandleKind, *, force: bool = False) -> bool:
    """Delete *handle* if its target supports it, else do nothing (returns False)."""
    if isinstance(handle, Deletable):
        return handle.delete(force=force)
    return False


__all__ = [
    "ComponentHandle",
    "ComponentHandleBase",
    "Deletable",
    "GroupHandle",
    "HandleKind",
    "ItemId",
    "ItemTag",
    "ReadOnlyComponentHandle",
    "Transformable",
    "WireHandle",
    "delete",
    "flip",
    "rotate",
]
