# SPDX-License-Identifier: MIT
"""Net / connectivity selection queries (pure, Qt-free).

The SelectTool's *relation* axis answers "given the thing I clicked, what set of
components + wires is correlated to it?" — the same-net members highlighted on a
plain click, and the set selected by an Alt (RELATION-basis) click.  The legacy
tool scattered this across ``_resolve_relation`` / ``_resolve_relation_net_ids``
/ ``_resolve_logical_net`` on the 9.6k monolith; this module is the lifted,
headless home for it.

The :class:`RelationClass` axis itself (the enum, labels, render profiles, and
the filter/relation persistence) already lives Qt-free in
:mod:`protoboard.gui.selection_axes`; this module only adds the *correlation*
logic on top, reading nets through the narrow :class:`ConnQuery` seam plus a
small :class:`NetSource` view of the schematic so it stays testable with a fake
instead of a full session.

* :class:`RelationSource` — the click source (a pin, a wire, or a whole
  component), the input to a relation query.
* :func:`relation_correlated` — ``(relation, source) -> (comp_ids, wire_ids)``,
  the verbatim port of ``_resolve_relation``.
* :func:`source_net_ids` — every logical net the source touches (port of
  ``_resolve_relation_net_ids`` / ``_resolve_logical_net``).
* :class:`SessionConnQuery` / :class:`SessionNetSource` — concrete adapters over
  an ``EditorSession`` satisfying the :class:`ConnQuery` / :class:`NetSource`
  protocols; the tool builds these, tests pass fakes.
"""

from __future__ import annotations

import uuid
from dataclasses import dataclass
from typing import TYPE_CHECKING, Protocol

from protoboard.gui.selection_axes import RelationClass

if TYPE_CHECKING:
    from collections.abc import Iterable

    from protoboard.gui.selection.selectable import ConnQuery
    from protoboard.gui.session import EditorSession
    from protoboard.types import NetId

__all__ = [
    "NetSource",
    "RelationSource",
    "SessionConnQuery",
    "SessionNetSource",
    "relation_correlated",
    "source_net_ids",
]


@dataclass(frozen=True, slots=True)
class RelationSource:
    """The clicked object a relation query correlates from.

    Exactly one shape is meaningful per click:

    * a **pin** — ``component_id`` + ``pin_number`` (narrows to one net),
    * a **wire** — ``wire_id`` (narrows to the wire's net),
    * a **component bbox** — ``component_id`` only (unions every net it touches).
    """

    component_id: uuid.UUID | None = None
    pin_number: int | None = None
    wire_id: uuid.UUID | None = None

    @classmethod
    def pin(cls, component_id: uuid.UUID, pin_number: int) -> RelationSource:
        return cls(component_id=component_id, pin_number=pin_number)

    @classmethod
    def wire(cls, wire_id: uuid.UUID) -> RelationSource:
        return cls(wire_id=wire_id)

    @classmethod
    def component(cls, component_id: uuid.UUID) -> RelationSource:
        return cls(component_id=component_id)


class NetSource(Protocol):
    """The schematic + wire-net reads a relation query needs beyond ConnQuery.

    ``ConnQuery`` answers "what nets does this object touch"; a relation query
    additionally needs the reverse — the members of a net — to expand outward.
    Kept narrow so a unit test supplies a minimal fake.
    """

    def net_component_ids(self, net_id: NetId) -> frozenset[uuid.UUID]:
        """Component ids with a pin on *net_id*."""
        ...

    def wire_ids_on_net(self, net_id: NetId) -> frozenset[uuid.UUID]:
        """Wire ids resolved to *net_id* by the wire-net map."""
        ...


def source_net_ids(source: RelationSource, conn: ConnQuery) -> frozenset[NetId]:
    """Every logical net the *source* touches (port of ``_resolve_relation_net_ids``).

    A pin or wire click yields at most one net; a bbox click on a component
    unions every net that component participates in.
    """
    if source.component_id is not None and source.pin_number is not None:
        return conn.nets_for_pin(source.component_id, source.pin_number)
    if source.wire_id is not None:
        nid = conn.net_for_wire(source.wire_id)
        return frozenset({nid}) if nid is not None else frozenset()
    if source.component_id is not None:
        return conn.nets_for_component(source.component_id)
    return frozenset()


def relation_correlated(
    relation: RelationClass,
    source: RelationSource,
    conn: ConnQuery,
    nets: NetSource,
) -> tuple[frozenset[uuid.UUID], frozenset[uuid.UUID]]:
    """Components + wires correlated to *source* by *relation*.

    Verbatim port of the legacy ``_resolve_relation``.
    The clicked object itself is always included so the dimming pipeline never
    wipes it out.  ``NONE`` correlates nothing.  ``SAME_LOGICAL_NET`` /
    ``SAME_PHYSICAL_NET`` / ``UNCONNECTED_PINS`` expand to the full net members
    (comps *and* wires); the unconnected-vs-connected split is a render-time
    overlay concern, not a different correlated set.  ``NEIGHBORS`` expands only
    the components sharing a net with the clicked component (no wires).
    """
    if relation == RelationClass.NONE:
        return frozenset(), frozenset()

    net_ids = source_net_ids(source, conn)

    comps: set[uuid.UUID] = set()
    wires: set[uuid.UUID] = set()
    # Seed with the clicked object so it never gets dimmed.
    if source.component_id is not None:
        comps.add(source.component_id)
    if source.wire_id is not None:
        wires.add(source.wire_id)

    if relation in (
        RelationClass.SAME_LOGICAL_NET,
        RelationClass.SAME_PHYSICAL_NET,
        RelationClass.UNCONNECTED_PINS,
    ):
        for nid in net_ids:
            comps |= nets.net_component_ids(nid)
            wires |= nets.wire_ids_on_net(nid)
    elif relation == RelationClass.NEIGHBORS and source.component_id is not None:
        for nid in net_ids:
            comps |= nets.net_component_ids(nid)

    return frozenset(comps), frozenset(wires)


# -- concrete session-backed adapters ----------------------------------------


@dataclass(frozen=True, slots=True)
class SessionConnQuery:
    """A :class:`ConnQuery` over an ``EditorSession`` (schematic + wire-net map)."""

    session: EditorSession

    def nets_for_component(self, component_id: object) -> frozenset[NetId]:
        return frozenset(
            n.id
            for n in self.session.schematic.nets.values()
            if any(pr.component_id == component_id for pr in n.pins)
        )

    def nets_for_pin(self, component_id: object, pin_number: int) -> frozenset[NetId]:
        from protoboard.domain.schematic.net import PinRef

        if not isinstance(component_id, uuid.UUID):
            return frozenset()
        net = self.session.schematic.net_for_pin(
            PinRef(component_id=component_id, pin_number=pin_number)
        )
        return frozenset({net.id}) if net is not None else frozenset()

    def net_for_wire(self, wire_id: object) -> NetId | None:
        if not isinstance(wire_id, uuid.UUID):
            return None
        return self.session.cached_wire_net_map().get(wire_id)


@dataclass(frozen=True, slots=True)
class SessionNetSource:
    """A :class:`NetSource` over an ``EditorSession`` (reverse net membership)."""

    session: EditorSession

    def net_component_ids(self, net_id: NetId) -> frozenset[uuid.UUID]:
        net = self.session.schematic.nets.get(net_id)
        if net is None:
            return frozenset()
        return frozenset(pr.component_id for pr in net.pins)

    def wire_ids_on_net(self, net_id: NetId) -> frozenset[uuid.UUID]:
        wnm = self.session.cached_wire_net_map()
        return frozenset(wid for wid, mapped in wnm.items() if mapped == net_id)


def iter_component_nets(
    component_id: uuid.UUID, conn: ConnQuery
) -> Iterable[NetId]:
    """Convenience: nets a component touches (used by adapters' ``net_ids``)."""
    return conn.nets_for_component(component_id)
