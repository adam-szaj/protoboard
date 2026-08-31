# SPDX-License-Identifier: MIT
"""Group-lock guards — is a component / wire sealed inside a group?

A group is a "hand-crafted IC": viewed from outside its active context it is a
sealed package — selecting it (for inspection) is allowed, but layout-mutating
ops (drag, delete, rotate, flip) are not. Stepping *inside* the group (making it
the active context) unlocks its direct members.

These are the pure, Qt-free predicates the :class:`EditController` verbs and the
GUI ``SelectTool`` both gate on. The tool's guards delegate here so there is a
single implementation; the tool supplies its interaction-state active group id,
a controller verb supplies the explicit ``active_group_id`` parameter.
"""

from __future__ import annotations

import uuid
from typing import TYPE_CHECKING

from protoboard.domain.layout.layout import ROOT_GROUP_ID

if TYPE_CHECKING:
    from protoboard.controller.editing.controller import EditingContext


def component_locked_in_group(
    state: EditingContext,
    comp_id: uuid.UUID,
    *,
    active_group_id: uuid.UUID,
) -> bool:
    """True when *comp_id* is a direct member of a group viewed from outside.

    Locked iff the component's *direct* group is NOT the active group.

    NB: do **not** use ``groups_containing_component`` here — it folds in the
    parent chain up to ROOT, so for any grouped component ROOT is always in the
    set, and with the default ROOT context the lock would never fire (the
    v2-rewrite regression this guard was restored to catch). The direct-
    membership check, skipping ROOT, is the correct gate.
    """
    for gid, g in state.layout.groups.items():
        if gid == ROOT_GROUP_ID:
            continue
        if comp_id not in g.component_ids:
            continue
        # Direct member of group `g`: locked unless we're inside `g`.
        return gid != active_group_id
    return False


def wire_locked_in_group(
    state: EditingContext,
    wire_id: uuid.UUID,
    *,
    active_group_id: uuid.UUID,
) -> bool:
    """True iff *wire_id* is structurally owned by a group viewed from outside.

    Mirrors :func:`component_locked_in_group` for wires: selecting a locked wire
    (for highlight) stays allowed, but mutating it (delete/edit) gates here and
    skips it.
    """
    from protoboard.domain.layout.group_ownership import OwnershipResolver

    wire = state.wire_by_id(wire_id)
    if wire is None:
        return False
    groups = state.layout.groups
    tree = state.group_tree()
    resolver = OwnershipResolver.from_session(state)
    for gid, group in groups.items():
        if gid == ROOT_GROUP_ID or gid == active_group_id:
            continue
        comp_ids = frozenset(tree.all_component_ids(gid))
        if comp_ids and resolver.is_owned(wire, group, comp_ids):
            return True
    return False


__all__ = ["component_locked_in_group", "wire_locked_in_group"]
