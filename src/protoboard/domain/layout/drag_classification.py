# SPDX-License-Identifier: MIT
"""Drag-time wire classification — what happens to each wire under a drag.

This is the pure-logic backbone of the unified drag model
(``docs/archive/bug-drag-skewed-wire-shorts.md``).  Given a drag set (frozenset of
component IDs that will move together by one delta), partition every wire
in the layout into five disjoint buckets:

* ``internal`` — both PinKnot endpoints anchored to pins of components in
  the drag set.  Travels with the drag delta atomically.
* ``cross_group_disconnect`` — exactly one PinKnot in the drag set, the
  other PinKnot is anchored to a component **outside** the set.  Drag
  would disconnect this wire; user must choose drop or reroute.
* ``half_orphan_internal`` — one PinKnot in the drag set + one JointKnot,
  and the wire's net is *internal* to some member group (no exported
  pin on that net, not global).  Travels with the drag delta.
* ``half_orphan_external`` — one PinKnot in the drag set + one JointKnot,
  net is **not** internal (exported, global, or unresolvable).  The
  joint endpoint cannot follow the pin (loose tail), so the wire is
  dropped on commit.
* ``external`` — neither endpoint anchored to a drag-set component.
  Unaffected by the drag.

Companion code paths consume this classification:

* :class:`protoboard.domain.commands.drag_cmds.MoveDragSetCommand`
  translates ``internal ∪ half_orphan_internal`` by the delta and
  removes wires the user opted to drop.
* :class:`protoboard.domain.layout.drag_detector.DragDetector` cross-references
  the classification with conflict detection so commit-time UI can
  surface the right preference dialog per category.

Pure — no Qt, no PadIndex, no side effects.
"""

from __future__ import annotations

import uuid
from dataclasses import dataclass
from typing import TYPE_CHECKING

from protoboard.domain.layout.group_ownership import (
    endpoint_pin_component as _endpoint_pin_component,
)
from protoboard.domain.layout.group_ownership import (
    net_is_internal_to_group,
)
from protoboard.domain.layout.knot import JointKnot

if TYPE_CHECKING:
    from protoboard.domain.layout.group import ComponentGroup
    from protoboard.domain.layout.knot import Knot
    from protoboard.domain.layout.wire import Wire
    from protoboard.domain.schematic.schematic import Schematic


__all__ = ["DragWireClassification", "classify_wires_for_drag"]


@dataclass(frozen=True)
class DragWireClassification:
    """Disjoint partition of layout wires against a hypothetical drag.

    The five fields cover every wire exactly once.  See module docstring
    for the bucket definitions.

    ``travels_with_drag`` is a derived helper combining the two buckets
    whose paths shift by the drag delta — used by callers building the
    ``MoveDragSetCommand.internal_wire_ids`` argument.
    """

    internal: frozenset[uuid.UUID]
    cross_group_disconnect: frozenset[uuid.UUID]
    half_orphan_internal: frozenset[uuid.UUID]
    half_orphan_external: frozenset[uuid.UUID]
    external: frozenset[uuid.UUID]

    @property
    def travels_with_drag(self) -> frozenset[uuid.UUID]:
        """Wires whose path must translate atomically with the drag delta."""
        return self.internal | self.half_orphan_internal


def _net_internal_to_any_member_group(
    net_id: uuid.UUID | None,
    member_groups: list[ComponentGroup],
    schematic: Schematic | None,
) -> bool:
    """True iff *net_id* is internal to at least one of *member_groups*.

    When a drag set spans multiple groups (rare; multi-selection across
    groups), a half-orphan tail counts as internal if any of the
    overlapping groups own the net internally.
    """
    if net_id is None or not member_groups or schematic is None:
        return False
    for group in member_groups:
        if net_is_internal_to_group(net_id, group, schematic):
            return True
    return False


def classify_wires_for_drag(
    wires: list[Wire],
    drag_set_members: frozenset[uuid.UUID],
    knots: dict[uuid.UUID, Knot],
    wire_net_map: dict[uuid.UUID, uuid.UUID],
    schematic: Schematic | None,
    member_groups: list[ComponentGroup],
) -> DragWireClassification:
    """Partition *wires* into the five drag-time buckets.

    Args:
        wires: All wires in the layout.
        drag_set_members: Component IDs that will move atomically by one
            delta (single comp, multi-selection, or recursive group
            membership).
        knots: Live ``layout.knots`` dict — resolves endpoint flavour
            (PinKnot vs JointKnot) and PinKnot's pinned component.
        wire_net_map: ``wire.id → net.id`` (typically
            ``EditorSession.cached_wire_net_map()``).  Used to resolve
            net membership for half-orphan tails.
        schematic: Schematic for net lookup (global flag, exported pin
            check via ``net_is_internal_to_group``).  May be ``None`` —
            in that case every half-orphan falls into
            ``half_orphan_external``.
        member_groups: Groups whose ``component_ids`` overlap
            ``drag_set_members``.  Empty when dragging a free
            component / pure-free multi-selection.  Used only for
            internal-net resolution of half-orphan tails — does not
            affect ``internal`` / ``cross_group_disconnect`` buckets.

    Returns:
        A :class:`DragWireClassification` with disjoint buckets covering
        every wire.  Wires without resolvable endpoints fall into
        ``external`` (treated as untouched).
    """
    internal: set[uuid.UUID] = set()
    cross_group: set[uuid.UUID] = set()
    half_orphan_int: set[uuid.UUID] = set()
    half_orphan_ext: set[uuid.UUID] = set()
    external: set[uuid.UUID] = set()

    for wire in wires:
        if wire.a is None or wire.b is None:
            external.add(wire.id)
            continue
        ka = knots.get(wire.a.id)
        kb = knots.get(wire.b.id)
        if ka is None or kb is None:
            external.add(wire.id)
            continue

        a_comp = _endpoint_pin_component(ka)
        b_comp = _endpoint_pin_component(kb)

        a_in = a_comp is not None and a_comp in drag_set_members
        b_in = b_comp is not None and b_comp in drag_set_members
        a_pin_foreign = a_comp is not None and a_comp not in drag_set_members
        b_pin_foreign = b_comp is not None and b_comp not in drag_set_members

        if a_in and b_in:
            # Both PinKnots in drag set — wire is rigid between drag-set
            # pins.  Travel with delta regardless of net (loopback,
            # internal-to-group, exported, even cross-group at runtime —
            # the drag treats it as one body).
            internal.add(wire.id)
            continue

        if (a_in and b_pin_foreign) or (b_in and a_pin_foreign):
            # Exactly one PinKnot in drag set, other PinKnot anchored to
            # a foreign component.  Drag = disconnect (rigid wire cannot
            # bend; user must choose drop or reroute).
            cross_group.add(wire.id)
            continue

        if a_in or b_in:
            # Half-orphan: one PinKnot in drag set + other endpoint is a
            # JointKnot (foreign-pin case handled above).  Verify the
            # other endpoint really is a JointKnot before classifying —
            # any other shape (None, unknown subclass) falls through to
            # external.
            other_knot = kb if a_in else ka
            if not isinstance(other_knot, JointKnot):
                external.add(wire.id)
                continue
            net_id = wire_net_map.get(wire.id)
            if _net_internal_to_any_member_group(
                net_id,
                member_groups,
                schematic,
            ):
                half_orphan_int.add(wire.id)
            else:
                half_orphan_ext.add(wire.id)
            continue

        # Neither endpoint anchors to a drag-set component.  Untouched.
        external.add(wire.id)

    return DragWireClassification(
        internal=frozenset(internal),
        cross_group_disconnect=frozenset(cross_group),
        half_orphan_internal=frozenset(half_orphan_int),
        half_orphan_external=frozenset(half_orphan_ext),
        external=frozenset(external),
    )
