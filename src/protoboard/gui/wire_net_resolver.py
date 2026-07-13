# SPDX-License-Identifier: MIT
"""GUI-level wire-to-net resolver seam.

The resolution logic itself is Qt-free and lives in
``controller.editing.wire_nets`` (:func:`resolve_wire_nets` +
:data:`NetMergePrompt`). This module keeps the Qt default prompt
(:func:`qt_net_merge_prompt`, the modal ``NetMergeDialog``) and a thin
GUI-facing :func:`resolve_wire_nets` wrapper that falls back to it, so existing
GUI callers keep the "parentless dialog when no prompt injected" behaviour while
the headless controller gets the loud-by-default library function directly.

The private helpers ``_create_new_net`` / ``_extend_net`` / ``_merge_nets`` are
re-exported for the tests that assert them against the canonical implementation.
"""

from __future__ import annotations

from typing import TYPE_CHECKING

from protoboard.domain.schematic.net import Net
from protoboard.controller.editing.wire_nets import (
    NetMergePrompt,
    _create_new_net,
    _extend_net,
    _merge_nets,
    _snapshot,
)
from protoboard.controller.editing.wire_nets import (
    resolve_wire_nets as _resolve_wire_nets,
)

if TYPE_CHECKING:
    from collections.abc import Callable

    from protoboard.domain.commands.component_cmds import AddWireWithNetsCommand
    from protoboard.domain.layout.wire import Wire
    from protoboard.gui.session import EditorSession

__all__ = [
    "NetMergePrompt",
    "qt_net_merge_prompt",
    "resolve_wire_nets",
    "_create_new_net",
    "_extend_net",
    "_merge_nets",
    "_snapshot",
]


def qt_net_merge_prompt(parent: Callable[[], object]) -> NetMergePrompt:
    """The default prompt: the modal ``NetMergeDialog``.

    *parent* resolves the dialog's parent widget lazily (RouteTool passes
    ``lambda: view.window()``), so no widget is touched until the two-nets
    case actually occurs.
    """

    def _ask(net_a: Net, net_b: Net) -> str | None:
        from PySide6.QtWidgets import QDialog

        from protoboard.gui.dialogs.net_merge_dialog import NetMergeDialog

        dlg = NetMergeDialog(net_a=net_a, net_b=net_b, parent=parent())
        if dlg.exec() != QDialog.DialogCode.Accepted:
            return None
        return dlg.chosen_name or net_a.name

    return _ask


def resolve_wire_nets(
    session: EditorSession,
    wire: Wire,
    cmd: AddWireWithNetsCommand,
    merge_prompt: NetMergePrompt | None = None,
) -> bool:
    """GUI wrapper over :func:`controller.editing.wire_nets.resolve_wire_nets`.

    Defaults *merge_prompt* to the parentless Qt ``NetMergeDialog`` so existing
    GUI callers keep their behaviour; the Qt-free library function raises when no
    prompt is injected.
    """
    if merge_prompt is None:
        merge_prompt = qt_net_merge_prompt(lambda: None)
    return _resolve_wire_nets(session, wire, cmd, merge_prompt)
