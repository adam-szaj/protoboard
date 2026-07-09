# SPDX-License-Identifier: MIT
"""SelectTool's modal prompts as injected decision callables (qt-ports D4).

The group-creation flow needs one user decision — "new group or add to
which existing one?".  The *question* and the *decision* are pure data;
only the default prompt implementation opens the Qt ``GroupTargetDialog``
(parented via the provider the tool wires on ``activate()``).  A test
injects a canned callable and the whole flow runs headless.
"""

from __future__ import annotations

import uuid
from dataclasses import dataclass
from typing import TYPE_CHECKING, Literal

if TYPE_CHECKING:
    from collections.abc import Callable

    from protoboard.domain.layout.group import ComponentGroup


@dataclass(frozen=True)
class GroupTargetRequest:
    """The question: group the selection into what?"""

    existing_groups: tuple[ComponentGroup, ...]
    default_name: str


@dataclass(frozen=True)
class GroupTargetChoice:
    """The decision (``None`` from the prompt = user cancelled)."""

    mode: Literal["new", "existing"]
    new_name: str
    target_group_id: uuid.UUID | None
    auto_export_external: bool


if TYPE_CHECKING:
    # The injectable prompt: ask the user, return the choice, None = cancel.
    GroupTargetPrompt = Callable[[GroupTargetRequest], GroupTargetChoice | None]


def qt_group_target_prompt(
    parent: Callable[[], object],
) -> Callable[[GroupTargetRequest], GroupTargetChoice | None]:
    """The default prompt: the modal ``GroupTargetDialog``.

    *parent* resolves the dialog's parent widget lazily (the tool passes
    ``lambda: view.window()``), so no widget is touched until the dialog
    actually opens.
    """

    def _ask(req: GroupTargetRequest) -> GroupTargetChoice | None:
        from typing import cast

        from PySide6.QtWidgets import QWidget

        from protoboard.gui.dialogs.group_target_dialog import (
            GroupTargetDialog,
        )

        dlg = GroupTargetDialog(
            existing_groups=list(req.existing_groups),
            default_name=req.default_name,
            parent=cast("QWidget | None", parent()),
        )
        if dlg.exec() != GroupTargetDialog.DialogCode.Accepted:
            return None
        return GroupTargetChoice(
            mode=dlg.mode,
            new_name=dlg.new_name,
            target_group_id=dlg.target_group_id,
            auto_export_external=dlg.auto_export_external,
        )

    return _ask
