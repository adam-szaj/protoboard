# SPDX-License-Identifier: MIT
"""GUI-level placement-conflict seam.

The conflict-resolution logic itself is Qt-free and lives in
``controller.editing.placement_conflict``; the drag-commit classifier moved to
``controller.editing.drag_commit``. This module keeps the Qt default prompts
(:func:`qt_placement_prompts`, the modal Pad/Wire conflict dialogs) and a thin
GUI :func:`check_and_resolve` wrapper that falls back to them. The moved names
(including :func:`classify_drag_commit`) are re-exported so existing GUI callers
and tests keep importing them from here.
"""

from __future__ import annotations

import uuid
from typing import TYPE_CHECKING

import structlog

from protoboard.controller.editing.placement_conflict import (
    PadConflictRequest,
    PlacementPrompts,
    PlacementResolution,
    WireConflictAction,
    WireConflictRequest,
    _resolve_wire_conflicts,  # noqa: F401  re-exported for tests
    _try_reroute_wire,  # noqa: F401  re-exported for tests
    build_wire_removal_cmds,
)
from protoboard.controller.editing.placement_conflict import (
    check_and_resolve as _check_and_resolve,
)

if TYPE_CHECKING:
    from collections.abc import Callable

    from protoboard.controller.ports.routing import PathfinderPort
    from protoboard.domain.components.definition import ComponentDefinition
    from protoboard.domain.components.instance import ComponentInstance
    from protoboard.domain.layout.placement import ComponentPlacement
    from protoboard.gui.session import EditorSession

__all__ = [
    "PadConflictRequest",
    "PlacementPrompts",
    "PlacementResolution",
    "WireConflictAction",
    "WireConflictRequest",
    "build_wire_removal_cmds",
    "check_and_resolve",
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
    pathfinder: PathfinderPort | None = None,
    instance: ComponentInstance | None = None,
) -> PlacementResolution | None:
    """GUI wrapper over the Qt-free ``check_and_resolve``.

    Defaults *prompts* to the parentless Qt dialogs so existing GUI callers keep
    their behaviour; the library function requires an explicit prompt.
    """
    if prompts is None:
        prompts = qt_placement_prompts(lambda: None)
    if pathfinder is None:
        from protoboard.services.editing.routing_adapters import DefaultPathfinder

        pathfinder = DefaultPathfinder()
    return _check_and_resolve(
        session,
        placement,
        defn,
        instance_id,
        prompts,
        pathfinder,
        instance,
    )
