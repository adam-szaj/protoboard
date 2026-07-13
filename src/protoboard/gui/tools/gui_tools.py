# SPDX-License-Identifier: MIT
"""GUI-composed interaction tools: :class:`ViewToolMixin` + a Qt-free
:class:`~protoboard.controller.tools.base.ControllerTool`.

Each class here supplies the two things a controller tool cannot own itself:
the default Qt preview / prompt construction (``activate()``), and — where
the controller tool doesn't already resolve its keys purely by action id —
the ``on_key`` raw-key-to-command translation.  ``ToolHost`` / ``MainWindow``
import tools from this module, never from ``controller.tools`` directly.

``select_tool`` / ``simplify_tool`` (a ``SelectTool`` subclass) are not
composed here — ``SelectTool`` itself is still Qt-laden (Task 6), so
``SimplifyTool`` stays in :mod:`protoboard.gui.tools.simplify_tool` for now.
"""

from __future__ import annotations

from typing import TYPE_CHECKING

from protoboard.gui.board_view_tool import ViewToolMixin
from protoboard.controller.tools.cut_tool import CutTool as _CutTool
from protoboard.controller.tools.place_tool import PlaceTool as _PlaceTool
from protoboard.controller.tools.route_tool import RouteTool as _RouteTool
from protoboard.controller.tools.armed_transform_tool import (
    ArmedTransformTool as _ArmedTransformTool,
)

if TYPE_CHECKING:
    from collections.abc import Callable

    from protoboard.controller.ports import KeyInput
    from protoboard.controller.tools.select_prompts import (
        GroupTargetChoice,
        GroupTargetRequest,
    )

__all__ = [
    "CutTool",
    "PlaceTool",
    "RouteTool",
    "ArmedTransformTool",
    "qt_group_target_prompt",
]

# Composition order note: every class below lists the controller tool BEFORE
# ViewToolMixin (``_XTool, ViewToolMixin`` — not the reverse).  Both
# ControllerTool and ViewToolMixin define on_press/on_key/on_key_release
# stubs AND make_key_map/_theme, so plain MRO order cannot make "the
# controller subclass's real on_press/on_key win" and "ViewToolMixin's real
# make_key_map/_theme win" hold at once — whichever base comes first also
# wins any name only the *other* base overrides.  Putting the controller tool
# first gets on_press/on_key/on_key_release right (its real bodies, not
# ViewToolMixin's do-nothing default); make_key_map/_theme are then
# explicitly re-pointed at ViewToolMixin's real implementation below.


class CutTool(_CutTool, ViewToolMixin):
    """Cut tool — no ``on_key``; click-only, already fully generic."""

    make_key_map = ViewToolMixin.make_key_map
    _theme = ViewToolMixin._theme


class PlaceTool(_PlaceTool, ViewToolMixin):
    """PlaceTool with the default ghost/reach Qt preview + Qt conflict prompts."""

    make_key_map = ViewToolMixin.make_key_map
    _theme = ViewToolMixin._theme

    def activate(self, session: object, view: object) -> None:
        # Bind session/view first (ControllerTool.activate — no ghost build
        # yet), inject the default Qt collaborators, THEN finish activation:
        # finish_activate() bakes the initial ghost, which needs self.preview
        # to already exist.
        from protoboard.controller.tools.base import ControllerTool

        ControllerTool.activate(self, session, view)  # type: ignore[arg-type]
        if not self._preview_injected:
            from protoboard.gui.tools.place_preview import QtPlacePreview

            self._preview = QtPlacePreview(self._view.scene, self._theme)
        if not self._prompts_injected:
            from protoboard.gui.placement_conflict import qt_placement_prompts

            self._prompts = qt_placement_prompts(lambda: self._view.window())
        self.finish_activate()


class RouteTool(_RouteTool, ViewToolMixin):
    """RouteTool with the default segment/hint Qt preview + net-merge prompt.

    ``on_key`` is the one raw-key translation left for a GUI subclass to own:
    the controller tool's ``key_dispatch()`` already maps every action id to
    its command handler — this just resolves the key through it.
    """

    make_key_map = ViewToolMixin.make_key_map
    _theme = ViewToolMixin._theme

    def activate(self, session: object, view: object) -> None:
        super().activate(session, view)  # type: ignore[arg-type]
        if not self._preview_injected:
            from protoboard.gui.tools.route_preview import QtRoutePreview

            self._preview = QtRoutePreview(self._view.scene, self._theme)
        if not self._merge_prompt_injected:
            from protoboard.gui.wire_net_resolver import qt_net_merge_prompt

            self._merge_prompt = qt_net_merge_prompt(lambda: self._view.window())

    def on_key(self, inp: KeyInput) -> bool:
        return self._dispatch_key(inp)


class ArmedTransformTool(_ArmedTransformTool, ViewToolMixin):
    """ArmedTransformTool with the default pivot-marker preview + conflict overlay."""

    make_key_map = ViewToolMixin.make_key_map
    _theme = ViewToolMixin._theme

    def activate(self, session: object, view: object) -> None:
        from protoboard.gui.tools.select_preview import QtSelectPreview
        from protoboard.gui.tools.transform_overlays import TransformOverlays

        super().activate(session, view)  # type: ignore[arg-type]
        self._preview = QtSelectPreview(view.scene)  # type: ignore[attr-defined]
        self._overlays = TransformOverlays(view)  # type: ignore[attr-defined]
        # The base activate() already drew pivot markers / published status
        # before this preview existed — redraw now that it does.
        self._draw_pivot_markers()  # type: ignore[attr-defined]


# ---------------------------------------------------------------------------
# select_tool's Qt prompt factories (select_tool.py stays gui-side / Task 6,
# but its data types already moved to controller.tools.select_prompts).
# ---------------------------------------------------------------------------


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

        from protoboard.controller.tools.select_prompts import GroupTargetChoice
        from protoboard.gui.dialogs.group_target_dialog import GroupTargetDialog

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
