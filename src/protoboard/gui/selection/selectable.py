# SPDX-License-Identifier: MIT
"""``Selectable`` — a uniform protocol over everything the user can select.

The legacy ``SelectTool`` branches on a ``Hit.kind`` string (``component`` /
``wire`` / ``group``) in ~40 places — every highlight, drag and selection
decision re-derives behaviour from that tag.  ``Selectable`` replaces the
tag with polymorphism: each board entity (component, wire, group, pin, and
the pick-only wire-vertex / knot) is wrapped by a thin GUI-side adapter that
knows how to identify, bound, net-resolve, highlight and drag *itself*.

The adapters live in the GUI layer (selection is a GUI concern) and wrap the
pure domain value objects (:class:`ComponentPlacement`, :class:`Wire`,
:class:`ComponentGroup`, :class:`PinRef`) without pulling Qt into the domain.

Only :meth:`Selectable.draw_highlight` touches Qt; the rest of the protocol
is pure so :class:`~protoboard.gui.selection.service.SelectionService` and
its pick machinery stay headless-testable.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import TYPE_CHECKING, Literal, Protocol, runtime_checkable

if TYPE_CHECKING:
    from collections.abc import Callable

    from PySide6.QtWidgets import QGraphicsItem

    from protoboard.geometry import Rect
    from protoboard.gui.selection_fsm import Hit
    from protoboard.types import NetId


# A ``Selectable``'s identity for dedup / equality inside a selection set.
# Two adapters wrapping the same logical entity must produce equal keys.
# Shape: ``(kind, *discriminators)`` — e.g. ``("component", comp_id)`` or
# ``("wire-vertex", wire_id, index)``.
SelectableKey = tuple[object, ...]

# What a ``request_pick`` caller wants the user to click.  A superset of the
# three FSM ``HitKind`` values plus pick-only entities (vertices, pads, free
# points) that are never part of a persistent selection.
PickKind = Literal[
    "component",
    "wire",
    "group",
    "pin",
    "wire-vertex",
    "knot",
    "pad",
    "point",
]


@runtime_checkable
class Selectable(Protocol):
    """A board entity the user can select or pick.

    Implementations are thin adapters in :mod:`gui.selection.adapters`; the
    domain value objects they wrap stay Qt-free.
    """

    def identity(self) -> SelectableKey:
        """Stable dedup/equality key (see :data:`SelectableKey`)."""
        ...

    def to_hit(self) -> Hit:
        """Bridge to the existing pure FSM, which still speaks :class:`Hit`.

        Lets ``SelectionService`` drive ``selection_fsm`` unchanged while the
        rest of the tool moves to ``Selectable``.
        """
        ...

    def bbox(self) -> Rect:
        """Axis-aligned world bounds, for rubber-band inside/intersect tests."""
        ...

    def net_ids(self, conn: ConnQuery) -> frozenset[NetId]:
        """Logical nets this entity participates in (drives relation/highlight).

        Empty when the entity carries no net (a free pin, a bare joint).
        """
        ...

    def draw_highlight(self, ctx: HighlightCtx) -> list[QGraphicsItem]:
        """Create (but do not add) the highlight overlay items for this entity.

        The only Qt-touching member.  :class:`HighlightController` owns adding
        them to the scene and their lifetime.
        """
        ...

    def to_drag_member(self) -> object | None:
        """A drag-set member for this entity, or ``None`` if it can't be dragged."""
        ...


@runtime_checkable
class ConnQuery(Protocol):
    """Read-only connectivity lookup handed to :meth:`Selectable.net_ids`.

    A narrow seam over the session's net sources (schematic + wire-net map)
    so adapters resolve nets without importing the GUI session directly.
    """

    def nets_for_component(self, component_id: object) -> frozenset[NetId]:
        ...

    def nets_for_pin(self, component_id: object, pin_number: int) -> frozenset[NetId]:
        ...

    def net_for_wire(self, wire_id: object) -> NetId | None:
        ...


@runtime_checkable
class HighlightCtx(Protocol):
    """Drawing context passed to :meth:`Selectable.draw_highlight`.

    Carries the pens/brushes/Z-values and the geometry resolvers an adapter
    needs to render itself, without each adapter reaching into the view or
    theme singletons.  Concretised by :class:`HighlightController` (Phase 1).
    """

    def pen_for(self, role: str) -> object:
        ...

    def scene_pos(self, world: object) -> object:
        ...


# ---------------------------------------------------------------------------
# Stop — the pick-termination predicate (request_pick(..., stop=Stop.X()))
# ---------------------------------------------------------------------------


@dataclass(frozen=True, slots=True)
class Stop:
    """When an active pick is complete.

    A small strategy object so ``request_pick`` has one ``stop`` parameter
    instead of overloaded ``count`` / ``until`` flags, and new termination
    modes (e.g. "until the path closes a loop") slot in without changing the
    signature.  Construct via the factories, never the raw fields:

    * :meth:`count` — auto-complete once exactly *n* items are collected.
    * :meth:`count_confirm` — collect up to *n*, but the user finalizes (Enter)
      and may revise (:meth:`PickSession.undo`) first; reaching *n* only *arms*
      completion, it does not auto-fire.
    * :meth:`confirm` — collect 0..∞; the user ends it (Enter); Esc cancels.
    * :meth:`until` — a caller predicate over the collected list decides.
    """

    mode: Literal["count", "count_confirm", "confirm", "until"]
    n: int = 0
    predicate: Callable[[list[Selectable]], bool] | None = None

    @classmethod
    def count(cls, n: int) -> Stop:
        """Done once exactly *n* items are picked (e.g. two wire-vertices)."""
        if n < 1:
            raise ValueError(f"Stop.count requires n >= 1; got {n}")
        return cls(mode="count", n=n)

    @classmethod
    def count_confirm(cls, n: int) -> Stop:
        """Collect up to *n*, then wait for the user to confirm (Enter).

        Unlike :meth:`count`, reaching *n* does not auto-complete — the user can
        revise the picks (drop the last via :meth:`PickSession.undo`) and only
        Enter finalizes.  Fixes the "can't correct a pick before it commits"
        gap; collection never exceeds *n* (further clicks are ignored until one
        is undone).
        """
        if n < 1:
            raise ValueError(f"Stop.count_confirm requires n >= 1; got {n}")
        return cls(mode="count_confirm", n=n)

    @classmethod
    def confirm(cls) -> Stop:
        """Open-ended: collect until the user confirms (Enter); Esc cancels."""
        return cls(mode="confirm")

    @classmethod
    def until(cls, predicate: Callable[[list[Selectable]], bool]) -> Stop:
        """Done when *predicate(collected)* is true (e.g. the loop closes)."""
        return cls(mode="until", predicate=predicate)

    def satisfied(self, collected: list[Selectable]) -> bool:
        """Whether *collected* auto-completes this stop condition.

        ``confirm`` and ``count_confirm`` are never auto-satisfied — only the
        user's explicit confirm action ends them (the service checks that
        separately), so this returns ``False`` regardless of how many items are
        gathered.  ``count_confirm`` also *caps* collection at ``n``.
        """
        if self.mode == "count":
            return len(collected) >= self.n
        if self.mode == "until":
            assert self.predicate is not None  # set by the factory
            return self.predicate(collected)
        return False  # "confirm" / "count_confirm" — user-driven only

    def at_capacity(self, collected: list[Selectable]) -> bool:
        """True when no more items should be accepted (``count_confirm`` cap)."""
        return self.mode == "count_confirm" and len(collected) >= self.n

    @property
    def user_confirmable(self) -> bool:
        """True for picks the user ends with Enter (``confirm`` / ``count_confirm``)."""
        return self.mode in ("confirm", "count_confirm")

    @property
    def revisable(self) -> bool:
        """True for picks that allow dropping the last item before confirming."""
        return self.mode in ("confirm", "count_confirm")
