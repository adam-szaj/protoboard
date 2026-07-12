# SPDX-License-Identifier: MIT
"""Qt-free drag-commit classifier.

:func:`classify_drag_commit` inspects the current (post-drag) layout state and
returns a :class:`DragCommitDecision` describing how the floating-overlay UX in
SelectTool should commit it. It is pure application logic — it never constructs
a Qt dialog — so it lives here in ``services/editing`` rather than under
``gui/``. The GUI re-exports it from :mod:`protoboard.gui.placement_conflict`
for existing callers.
"""

from __future__ import annotations

import uuid
from typing import TYPE_CHECKING

if TYPE_CHECKING:
    from protoboard.domain.board.pad_state import PadIndex
    from protoboard.gui.tools.drag_commit_decision import DragCommitDecision
    from protoboard.services.editing.controller import EditState


def classify_drag_commit(
    session: EditState,
    moved_comp_ids: set[uuid.UUID],
    dragged_wire_ids: set[uuid.UUID] | frozenset[uuid.UUID],
    drag_set_members: frozenset[uuid.UUID] | None = None,
) -> DragCommitDecision:
    """Classify the current (post-drag) layout state for commit.

    Thin wrapper around :class:`DragDetector`. Builds a
    :class:`DragSnapshot` from *session* (including a fresh PadIndex
    when ``session.board`` is available) and delegates classification
    to the detector configured with a :class:`DRCEnforcingStrategy`.

    ``drag_set_members`` (optional, unified drag model 2026-06-02): the
    full set of components moving atomically by one delta — single
    component, multi-selection, or recursive group membership.  When
    supplied, the detector emits a
    :class:`DragWireClassification` and partitions removable wires
    into free / stationary-group / cross-group-disconnect buckets.
    When None (legacy callers), the detector falls back to the
    original single-bucket auto-remove path.

    Pure logic — never constructs a Qt dialog.
    """
    from protoboard.domain.layout.group_tree import GroupTree
    from protoboard.services.drag_detector import DragDetector, DragSnapshot
    from protoboard.services.pad_strategy import DRCEnforcingStrategy

    pad_index = _build_drag_pad_index(session)
    drag_set = drag_set_members or frozenset()

    # Resolve member_groups / stationary_locked_groups from the live
    # session layout.  member_groups overlap drag_set; locked groups
    # are the rest with lock_internal_wires=True.
    member_groups: list[object] = []
    locked_groups: list[object] = []
    layout_groups = getattr(session.layout, "groups", {})
    _tree = GroupTree(layout_groups)
    for gid, group in layout_groups.items():
        gcids = _tree.all_component_ids(gid)
        if gcids & drag_set:
            member_groups.append(group)
        elif getattr(group, "lock_internal_wires", False):
            locked_groups.append(group)

    wire_net_map: dict[uuid.UUID, uuid.UUID] = {}
    if drag_set:
        # Best-effort: session may not expose the helper in test stubs.
        getter = getattr(session, "cached_wire_net_map", None)
        if callable(getter):
            try:
                wire_net_map = dict(getter())
            except Exception:  # noqa: BLE001
                wire_net_map = {}

    snapshot = DragSnapshot(
        pad_index=pad_index,
        placements=session.layout.placements,
        instances=session.instances,
        components=session.components,
        schematic=session.schematic,
        wires=list(session.layout.wires),
        drag_set_members=drag_set,
        wire_net_map=wire_net_map,
        knots=dict(session.layout.knots) if drag_set else {},
        member_groups=member_groups,  # type: ignore[arg-type]
        stationary_locked_groups=locked_groups,  # type: ignore[arg-type]
    )
    detector = DragDetector(DRCEnforcingStrategy())
    return detector.classify(
        snapshot,
        moved_comp_ids=set(moved_comp_ids),
        dragged_wire_ids=frozenset(dragged_wire_ids),
    )


def _build_drag_pad_index(session: EditState) -> PadIndex | None:
    """Build a fresh PadIndex reflecting the live (post-drag) layout.

    Returns None when *session* lacks a ``board`` (test stubs) or
    construction fails — callers fall back to legacy from-layout paths.
    """
    if not hasattr(session, "board"):
        return None
    try:
        from protoboard.domain.board.pad_state import PadIndex
        return PadIndex(
            board=session.board,
            layout=session.layout,
            schematic=session.schematic,
            components=session.components,
            instances=session.instances,
        )
    except Exception:  # noqa: BLE001
        return None
