# SPDX-License-Identifier: MIT
"""Frozen snapshots of mutable domain state for the routing pipeline.

The autorouter runs in a background QThread.  Its inputs (Layout, Schematic,
instances dict) are shared mutable references with the GUI main thread — if
the user moves a component or renames a net while autoroute is in flight,
the worker reads from under itself.  ``RouteRequest`` is the immutable
contract: build it on the main thread *before* the worker starts; the
worker is structurally unable to see later edits.

Snapshots are shallow on geometry (Point, Wire.path, etc.) — those
elements are already immutable-by-convention in the codebase (path is a
list but is never mutated in-place; the codebase always replaces wires
wholesale via ``ReplaceWireSegmentCommand``).  We freeze the *containers*
that the routing code iterates over: dicts become MappingProxyType,
lists/sets become tuple/frozenset.

Adapting back to mutable types
------------------------------
The existing ``autoroute()`` body expects mutable ``Layout`` and
``Schematic`` instances (it calls ``list(layout.wires)``, indexes
``schematic.nets.values()``, etc.).  ``RouteRequest.as_legacy_pair()``
returns ``(Layout, Schematic)`` *fresh copies* — mutating those copies
inside autoroute is now safe because they are not the live session state.
"""

from __future__ import annotations

import uuid
from dataclasses import dataclass
from types import MappingProxyType
from typing import TYPE_CHECKING, Literal, Mapping

if TYPE_CHECKING:
    from protoboard.domain.board.board import Board
    from protoboard.domain.components.definition import ComponentDefinition
    from protoboard.domain.components.instance import ComponentInstance
    from protoboard.domain.layout.layout import Layout
    from protoboard.domain.layout.placement import ComponentPlacement
    from protoboard.domain.layout.wire import Wire
    from protoboard.domain.routing_settings import RoutingSettings
    from protoboard.domain.schematic.net import Net
    from protoboard.domain.schematic.schematic import Schematic


@dataclass(frozen=True)
class LayoutSnapshot:
    """Immutable view of a ``Layout`` for the routing pipeline.

    ``placements`` is a MappingProxyType — keys/values are unchanged
    references to the original ComponentPlacement objects, but the mapping
    itself rejects mutation.  ``wires`` is a tuple of the original
    Wire references in original order.

    For the autorouter this is enough: it only ever iterates these
    containers, never mutates them.  Code that *does* need to mutate
    (e.g. ``_drop_orphan_wires``) builds a fresh ``list`` from
    ``wires`` instead.
    """

    placements: Mapping[uuid.UUID, "ComponentPlacement"]
    wires: tuple["Wire", ...]

    @classmethod
    def freeze(cls, layout: "Layout") -> "LayoutSnapshot":
        return cls(
            placements=MappingProxyType(dict(layout.placements)),
            wires=tuple(layout.wires),
        )

    def to_layout(self) -> "Layout":
        """Return a fresh mutable Layout suitable for in-place work.

        The returned Layout is a fresh object; mutating it does not touch
        the snapshot or the original session Layout.
        """
        from protoboard.domain.layout.layout import Layout
        return Layout(
            placements=dict(self.placements),
            wires=list(self.wires),
        )


@dataclass(frozen=True)
class SchematicSnapshot:
    """Immutable view of a ``Schematic`` for the routing pipeline.

    Each Net is rebuilt with its pin set frozen (``frozenset``) so that
    iteration during routing cannot be invalidated by a concurrent edit
    to ``Net.pins``.  Net ids and names are unchanged.
    """

    nets: Mapping[uuid.UUID, "Net"]

    @classmethod
    def freeze(cls, schematic: "Schematic") -> "SchematicSnapshot":
        from protoboard.domain.schematic.net import Net
        frozen_nets: dict[uuid.UUID, Net] = {}
        for nid, net in schematic.nets.items():
            frozen_nets[nid] = Net(
                id=net.id,
                name=net.name,
                pins=set(net.pins),  # fresh set — disconnected from live net
                color=net.color,
                global_net=net.global_net,
            )
        return cls(nets=MappingProxyType(frozen_nets))

    def to_schematic(self) -> "Schematic":
        from protoboard.domain.schematic.schematic import Schematic
        return Schematic(nets=dict(self.nets))


@dataclass(frozen=True)
class RouteRequest:
    """Complete immutable input for one routing run.

    Build this on the main thread before submitting the AutorouteWorker.
    Inside the worker the request is the *only* source of truth — the
    worker must not reach back into the live session.

    ``board`` is passed through as a reference: Board is built from
    immutable ``BoardGrid``/``PadAtom``/``StripAtom`` (frozen dataclasses)
    so it is already effectively immutable.  ``components`` (definitions)
    are also immutable by convention.  ``instances`` is wrapped in a
    MappingProxyType for symmetry.
    """

    board: "Board"
    layout: LayoutSnapshot
    schematic: SchematicSnapshot
    components: Mapping[str, "ComponentDefinition"]
    instances: Mapping[uuid.UUID, "ComponentInstance"]
    routing_settings: "RoutingSettings"
    net_ids: frozenset[uuid.UUID] | None = None
    side: Literal["top", "bottom"] = "top"
    report_path: object | None = None  # pathlib.Path, deferred to avoid import cycle

    @classmethod
    def from_session(
        cls,
        *,
        board: "Board",
        layout: "Layout",
        schematic: "Schematic",
        components: dict[str, "ComponentDefinition"],
        instances: dict[uuid.UUID, "ComponentInstance"],
        routing_settings: "RoutingSettings",
        net_ids: set[uuid.UUID] | None = None,
        side: Literal["top", "bottom"] = "top",
        report_path: object | None = None,
    ) -> "RouteRequest":
        """Snapshot the live session state.  Call on the main thread."""
        return cls(
            board=board,
            layout=LayoutSnapshot.freeze(layout),
            schematic=SchematicSnapshot.freeze(schematic),
            components=MappingProxyType(dict(components)),
            instances=MappingProxyType(dict(instances)),
            routing_settings=routing_settings,
            net_ids=frozenset(net_ids) if net_ids is not None else None,
            side=side,
            report_path=report_path,
        )


__all__ = [
    "LayoutSnapshot",
    "SchematicSnapshot",
    "RouteRequest",
]
