# SPDX-License-Identifier: MIT
"""Qt-free command-building core for wire-fragment edits.

:class:`WireEditArm` is the thin, stateful dispatcher a wire-edit tool arms once
per fragment.  It lifts only the *command-building* half of the five SelectTool
wire-edit ops (``_commit_reroute`` / ``_autoroute_fragment`` /
``_flip_wire_fragment`` / ``_simplify_wire_fragment`` / ``_delete_fragment``):
each method BUILDS a :class:`Command` by delegating to the already-Qt-free,
already-session-free range builders in
:mod:`protoboard.controller.selection.wire_edit`, and returns it *without*
executing.  The tool keeps the commit half (``_wire_edit_clear`` +
``_execute_command`` + ``_refresh_after_wire_op``).

The arm's ONLY held state is the injected ``add_factory``.  The range builders
are already session-free; the one thing that must be injected (not built here,
because a net-aware factory needs the live session's ``resolve_wire_nets``) is
the fresh-wire factory.  Per-op geometry (``layout``, ``wires``, the
:class:`WireEditState`/``WireRange``, the routing ``graph``, and the resolved
autoroute endpoints/side) is passed at call time exactly as the builders
require, so this stays Qt-free and session-free — headless testable.
``reroute`` / ``autoroute`` / ``delete`` thread ``self._add_factory`` into their
builders; ``flip`` / ``simplify`` are id-preserving and take no factory.
"""

from __future__ import annotations

from typing import TYPE_CHECKING

from protoboard.controller.selection.wire_edit import (
    AddWireFactory,
    WireEditState,
    WireRangeEdit,
    _default_add_factory,
    autoroute_fragment_path,
    build_delete_command,
    build_delete_range_edit,
    build_flip_fragment_command,
    build_reroute_command,
    build_set_insulated_range_command,
    build_simplify_range_command,
)

if TYPE_CHECKING:
    import uuid
    from collections.abc import Sequence

    from protoboard.controller.ports.routing import FindPaths
    from protoboard.domain.commands.base import Command, ComponentState
    from protoboard.domain.connectivity.incremental import ConnectivityGraph
    from protoboard.domain.layout.wire import Wire
    from protoboard.geometry import Point

__all__ = ["WireEditArm"]


class WireEditArm:
    """Stateful builder over the Qt-free wire-edit range builders.

    Holds a single piece of state — the ``add_factory`` used to mint the
    surviving/replacement wires of cut/reroute ops — and dispatches each edit op
    to the matching builder in
    :mod:`protoboard.controller.selection.wire_edit`, returning a
    ``Command | None`` for the tool to execute.
    """

    def __init__(
        self,
        add_factory: AddWireFactory = _default_add_factory,
        *,
        find_paths: FindPaths | None = None,
    ) -> None:
        self._add_factory = add_factory
        self._find_paths = find_paths

    def reroute(
        self,
        layout: object,
        wires: Sequence[Wire],
        wire_net_map: dict[uuid.UUID, uuid.UUID],
        state: WireEditState,
    ) -> Command[ComponentState] | None:
        """Build the reroute command for *state* (which already carries its
        waypoints).  Cross-wire replacements go through the injected factory."""
        return build_reroute_command(
            layout, wires, wire_net_map, state, self._add_factory
        )

    def autoroute(
        self,
        layout: object,
        wires: Sequence[Wire],
        wire_net_map: dict[uuid.UUID, uuid.UUID],
        state: WireEditState,
        graph: ConnectivityGraph,
        source: Point,
        target: Point,
        side: str,
    ) -> Command[ComponentState] | None:
        """Run A* between *source* and *target*, then build the reroute command
        from the routed interior waypoints.  Returns ``None`` when no path."""
        if self._find_paths is None:
            return None
        routed = autoroute_fragment_path(self._find_paths, graph, source, target, side)
        if routed is None:
            return None
        waypoints = routed[1:-1]
        return build_reroute_command(
            layout,
            wires,
            wire_net_map,
            state.with_waypoints(waypoints),
            self._add_factory,
        )

    def flip(
        self,
        wires: Sequence[Wire],
        state: WireEditState,
        target_side: str | None = None,
    ) -> Command[ComponentState] | None:
        """Build the id-preserving fragment-flip command (no factory)."""
        return build_flip_fragment_command(wires, state, target_side)

    def simplify(
        self,
        layout: object,
        wires: Sequence[Wire],
        state: WireEditState,
        inputs: object,
        mode: object,
    ) -> Command[ComponentState] | None:
        """Build the id-preserving range-simplify command (no factory)."""
        return build_simplify_range_command(layout, wires, state, inputs, mode)

    def delete(
        self,
        wires: Sequence[Wire],
        wire_net_map: dict[uuid.UUID, uuid.UUID],
        state: WireEditState,
    ) -> Command[ComponentState] | None:
        """Build the delete command; surviving halves go through the factory."""
        return build_delete_command(wires, wire_net_map, state, self._add_factory)

    def set_insulated(
        self,
        wires: Sequence[Wire],
        wire_net_map: dict[uuid.UUID, uuid.UUID],
        state: WireEditState,
        insulated: bool,
    ) -> WireRangeEdit | None:
        """Build the set-insulated range edit; split pieces go through the factory."""
        return build_set_insulated_range_command(
            wires, wire_net_map, state, insulated, self._add_factory
        )

    def delete_range(
        self,
        wires: Sequence[Wire],
        wire_net_map: dict[uuid.UUID, uuid.UUID],
        state: WireEditState,
    ) -> WireRangeEdit | None:
        """Delete as a ``WireRangeEdit`` (survivors surfaced for re-arm)."""
        return build_delete_range_edit(wires, wire_net_map, state, self._add_factory)
