# SPDX-License-Identifier: MIT
"""GUI-level placement-conflict seam.

The conflict-resolution logic itself is Qt-free and lives in
``services.editing.placement_conflict``. This module keeps the Qt default
prompts (:func:`qt_placement_prompts`, the modal Pad/Wire conflict dialogs), a
thin GUI :func:`check_and_resolve` wrapper that falls back to them, and the
drag-commit classifier (:func:`classify_drag_commit`). The moved names are
re-exported so existing GUI callers and tests keep importing them from here.
"""

from __future__ import annotations

import uuid
from typing import TYPE_CHECKING

import structlog

from protoboard.geometry import round_coord as _round  # noqa: F401  legacy alias
from protoboard.services.editing.placement_conflict import (
    PadConflictRequest,
    PlacementPrompts,
    PlacementResolution,
    WireConflictAction,
    WireConflictRequest,
    _resolve_wire_conflicts,  # noqa: F401  re-exported for tests
    _try_reroute_wire,  # noqa: F401  re-exported for tests
    build_wire_removal_cmds,
)
from protoboard.services.editing.placement_conflict import (
    check_and_resolve as _check_and_resolve,
)

if TYPE_CHECKING:
    from collections.abc import Callable

    from protoboard.domain.board.pad_state import PadIndex
    from protoboard.domain.components.definition import ComponentDefinition
    from protoboard.domain.components.instance import ComponentInstance
    from protoboard.domain.layout.placement import ComponentPlacement
    from protoboard.gui.session import EditorSession
    from protoboard.gui.tools.drag_commit_decision import DragCommitDecision

__all__ = [
    "PadConflictRequest",
    "PlacementPrompts",
    "PlacementResolution",
    "WireConflictAction",
    "WireConflictRequest",
    "build_wire_removal_cmds",
    "check_and_resolve",
    "classify_drag_commit",
    "qt_placement_prompts",
]

_log = structlog.get_logger(__name__)


def qt_placement_prompts(parent: Callable[[], object]) -> PlacementPrompts:
    """The default prompts: modal Pad/Wire conflict dialogs.

    *parent* resolves the dialogs' parent widget lazily (PlaceTool passes
    ``lambda: view.window()``).
    """

    def _ask_pad(req: PadConflictRequest) -> bool:
        from PySide6.QtWidgets import QDialog

        from protoboard.gui.dialogs.pad_conflict_dialog import (
            PadConflictDialog,
        )

        dlg = PadConflictDialog(
            conflicts=list(req.conflicts),
            incoming_refdes=req.incoming_refdes,
            existing_refdes_map=req.existing_refdes,
            parent=parent(),
        )
        return dlg.exec() == QDialog.DialogCode.Accepted

    def _ask_wire(req: WireConflictRequest) -> WireConflictAction | None:
        from typing import cast

        from PySide6.QtWidgets import QDialog, QWidget

        from protoboard.gui.dialogs.wire_conflict_dialog import (
            WireConflictDialog,
        )

        dlg = WireConflictDialog(
            conflicts=list(req.conflicts),
            incoming_refdes=req.incoming_refdes,
            parent=cast("QWidget | None", parent()),
        )
        if dlg.exec() != QDialog.DialogCode.Accepted:
            return None
        return dlg.chosen_action()

    return PlacementPrompts(pad=_ask_pad, wire=_ask_wire)


def check_and_resolve(
    session: EditorSession,
    placement: ComponentPlacement,
    defn: ComponentDefinition,
    instance_id: uuid.UUID,
    prompts: PlacementPrompts | None = None,
    instance: ComponentInstance | None = None,
) -> PlacementResolution | None:
    """GUI wrapper over the Qt-free ``check_and_resolve``.

    Defaults *prompts* to the parentless Qt dialogs so existing GUI callers keep
    their behaviour; the library function requires an explicit prompt.
    """
    if prompts is None:
        prompts = qt_placement_prompts(lambda: None)
    return _check_and_resolve(
        session, placement, defn, instance_id, prompts, instance,
    )


# ---------------------------------------------------------------------------
# Drag-commit classifier (Qt-free) — drives floating-overlay UX in SelectTool
# ---------------------------------------------------------------------------


def classify_drag_commit(
    session: EditorSession,
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


def _build_drag_pad_index(session: EditorSession) -> PadIndex | None:
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
