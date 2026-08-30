# SPDX-License-Identifier: MIT
"""Soft-conflict wire-drop resolution — shared by drag and float-transform.

When a drag / float-transform lands with *soft* conflicts, the colliding
wires fall into three buckets the user has a preference for: ``free`` wires
(no group), ``stationary-group`` wires, and ``cross-group-disconnect``
wires.  Each bucket has a preference (``auto-remove`` / ``ask-remove`` /
``auto-reroute`` / ``ask-reroute`` / ``block``) resolved through an injected
callback — the callback may pop an ask-dialog.

This was private to :class:`DragController`; it is extracted here unchanged so
the armed transform (:class:`~protoboard.controller.editing.armed_transform.ArmedTransform`)
resolves drops identically (same dialogs, same defaults) without duplicating the
logic.
"""  # noqa: E501

from __future__ import annotations

import uuid
from collections.abc import Callable
from typing import TYPE_CHECKING

if TYPE_CHECKING:
    from protoboard.controller.editing.drag_preferences import DragPreferences
    from protoboard.controller.tools.drag_commit_decision import DragCommitDecision

    ResolveFn = Callable[
        [str, frozenset[uuid.UUID]],
        "tuple[set[uuid.UUID], set[uuid.UUID]] | None",
    ]
    """``(preference, wire_ids) -> (drop, reroute_nets) | None`` (None = cancel)."""


def run_resolver(
    resolver: ResolveFn | None,
    preference: str | None,
    wire_ids: frozenset[uuid.UUID],
) -> tuple[set[uuid.UUID], set[uuid.UUID]] | None:
    """Resolve one bucket; default to dropping all when no resolver wired."""
    if not wire_ids:
        return set(), set()
    if resolver is None or preference is None:
        return set(wire_ids), set()  # headless default: auto-remove
    return resolver(preference, wire_ids)


def resolve_drops(
    decision: DragCommitDecision,
    *,
    preferences: DragPreferences | None,
    resolve_free: ResolveFn | None,
    resolve_sgroup: ResolveFn | None,
    resolve_disconnect: ResolveFn | None,
) -> tuple[set[uuid.UUID], set[uuid.UUID]] | None:
    """Run the three preference resolvers; merge kept drops + reroutes.

    Returns ``(dropped_wire_ids, reroute_net_ids)`` or ``None`` when any
    bucket's resolver cancels (user dismissed an ask-dialog, or a ``block``
    preference) — in which case the caller must abort the commit and stay
    floating.
    """
    free_pref = None if preferences is None else preferences.free_wire
    sgroup_pref = None if preferences is None else preferences.stationary_group_wire
    disc_pref = None if preferences is None else preferences.cross_group_disconnect
    free_ids = frozenset(decision.free_drop_wire_ids)
    sgroup_ids = frozenset(decision.stationary_group_drop_wire_ids)
    disc_ids = frozenset(decision.cross_group_disconnect_wire_ids)

    free = run_resolver(resolve_free, free_pref, free_ids)
    if free is None:
        return None
    sgroup = run_resolver(resolve_sgroup, sgroup_pref, sgroup_ids)
    if sgroup is None:
        return None
    disc = run_resolver(resolve_disconnect, disc_pref, disc_ids)
    if disc is None:
        return None

    dropped = free[0] | sgroup[0] | disc[0]
    reroute = free[1] | sgroup[1] | disc[1]
    return dropped, reroute
