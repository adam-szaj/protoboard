# SPDX-License-Identifier: MIT
"""GUI-composed interaction tools: :class:`ViewToolMixin` + a Qt-free
:class:`~protoboard.controller.tools.base.ControllerTool`.

Each class here supplies the two things a controller tool cannot own itself:
the default Qt preview / prompt construction (``activate()``), and — where
the controller tool doesn't already resolve its keys purely by action id —
the ``on_key`` raw-key-to-command translation.  ``ToolHost`` / ``MainWindow``
import tools from this module, never from ``controller.tools`` directly.
"""

from __future__ import annotations

from typing import TYPE_CHECKING, cast

from protoboard.controller.tools.armed_transform_tool import (
    ArmedTransformTool as _ArmedTransformTool,
)
from protoboard.controller.tools.cut_tool import CutTool as _CutTool
from protoboard.controller.tools.place_tool import PlaceTool as _PlaceTool
from protoboard.controller.tools.route_tool import RouteTool as _RouteTool
from protoboard.controller.tools.select_tool import (
    _ACT_DELETE,
    _ACT_EDIT,
    _ACT_FLIP,
    _ACT_MOVE,
    _ACT_RELATION_CYCLE,
    _ACT_RELATION_CYCLE_BACK,
    _ACT_ROTATE,
)
from protoboard.controller.tools.select_tool import SelectTool as _SelectTool
from protoboard.controller.tools.simplify_tool import SimplifyTool as _SimplifyTool
from protoboard.controller.tools.wire_edit_tool import WireEditTool as _WireEditTool
from protoboard.gui.board_view_tool import ViewToolMixin

if TYPE_CHECKING:
    from collections.abc import Callable

    from protoboard.controller.ports import KeyInput
    from protoboard.controller.selection.mouse_input import Intent
    from protoboard.controller.tools.select_prompts import (
        GroupTargetChoice,
        GroupTargetRequest,
    )
    from protoboard.controller.view_options.theme import Theme
    from protoboard.gui.board_view_tool import AbstractBoardView


def _gui_view(view: object) -> AbstractBoardView:
    """Narrow a tool's ``activate(view)`` argument to the GUI board-view surface.

    The controller tools type the view only as the Qt-free ``_ViewProto`` (which
    lacks ``scene()``); the Qt-composed tools here genuinely receive an
    ``AbstractBoardView``. This cast localises that knowledge to activation.
    """
    return cast("AbstractBoardView", view)


def _theme_fn(tool: ViewToolMixin) -> Callable[[], Theme]:
    """The tool's theme reader typed as ``() -> Theme``.

    ``ViewToolMixin._theme`` really returns :class:`Theme`, but on a concrete
    tool pyright resolves the looser ``ControllerTool._theme`` (``-> object |
    None``) from the MRO. Binding it through ``ViewToolMixin`` restores the
    precise return type for the Qt preview constructors that require it.
    """
    return tool._theme


__all__ = [
    "CutTool",
    "PlaceTool",
    "RouteTool",
    "ArmedTransformTool",
    "SelectTool",
    "SimplifyTool",
    "WireEditTool",
    "qt_group_target_prompt",
]


def _dispatch_key_by_action_id(tool: object, inp: KeyInput) -> bool:
    """Resolve *inp* to one of the active tool's ``command_table()`` action
    ids via the KeyMap, then run that command and return ITS verdict.

    The command-dispatch counterpart of the old ``ControllerTool._dispatch_
    key`` (removed from the controller tools — Task 7): the View now owns the
    key→action-id resolution (``tool._key_matches``, KeyMap-backed); the
    command table itself comes from the Controller facade
    (``EditController.tool_commands()``) so it is read from the same place a
    headless driver would. Mirrors an ``elif`` ladder — the first matching
    action id's handler decides, and its own return value (not just "a
    command ran") is what ``on_key`` reports as consumed — some handlers
    (e.g. PlaceTool's Esc-in-ANCHOR) legitimately decline.
    """
    commands = tool._session.edit_controller.tool_commands()  # type: ignore[attr-defined]
    for action_id, handler in commands.items():
        if tool._key_matches(inp, action_id):  # type: ignore[attr-defined]
            return bool(handler())
    return False


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


class CutTool(
    _CutTool, ViewToolMixin
):  # pyright: ignore[reportIncompatibleMethodOverride, reportIncompatibleVariableOverride]
    """Cut tool — no ``on_key``; click-only, already fully generic."""

    make_key_map = ViewToolMixin.make_key_map  # pyright: ignore[reportAssignmentType]
    _theme = ViewToolMixin._theme  # pyright: ignore[reportAssignmentType]

    def activate(self, session: object, view: object) -> None:
        # Same ``object``-typed signature as every sibling tool's ``activate``
        # (PlaceTool/RouteTool/…): the View seam (``_Tool``) types activate as
        # ``(EditorSession, AbstractBoardView)``, which ``ControllerTool``'s
        # Qt-free ``(EditingContext, _ViewProto)`` does not match — so the concrete
        # tools re-declare it wide.  No extra activation work for CutTool.
        from protoboard.controller.tools.base import ControllerTool

        ControllerTool.activate(self, session, view)  # type: ignore[arg-type]


class PlaceTool(
    _PlaceTool, ViewToolMixin
):  # pyright: ignore[reportIncompatibleMethodOverride, reportIncompatibleVariableOverride]
    """PlaceTool with the default ghost/reach Qt preview + Qt conflict prompts.

    ``on_key`` is the raw-key translation a GUI subclass owns: the controller
    tool knows nothing of keys (Task 7) — its ``command_table()`` maps every
    action id to its command handler, so this just resolves the key to an id
    via the KeyMap and dispatches it through the Controller.
    """

    make_key_map = ViewToolMixin.make_key_map  # pyright: ignore[reportAssignmentType]
    _theme = ViewToolMixin._theme  # pyright: ignore[reportAssignmentType]

    def activate(self, session: object, view: object) -> None:
        # Bind session/view first (ControllerTool.activate — no ghost build
        # yet), inject the default Qt collaborators, THEN finish activation:
        # finish_activate() bakes the initial ghost, which needs self.preview
        # to already exist.
        from protoboard.controller.tools.base import ControllerTool

        ControllerTool.activate(self, session, view)  # type: ignore[arg-type]
        gui_view = _gui_view(view)
        if not self._preview_injected:
            from protoboard.gui.tools.place_preview import QtPlacePreview

            self._preview = QtPlacePreview(gui_view.scene, _theme_fn(self))
        if not self._prompts_injected:
            from protoboard.gui.placement_conflict import qt_placement_prompts

            self._prompts = qt_placement_prompts(lambda: gui_view.window())
        self.finish_activate()

    def on_key(self, inp: KeyInput) -> bool:
        return _dispatch_key_by_action_id(self, inp)


class RouteTool(
    _RouteTool, ViewToolMixin
):  # pyright: ignore[reportIncompatibleMethodOverride, reportIncompatibleVariableOverride]
    """RouteTool with the default segment/hint Qt preview + net-merge prompt.

    ``on_key`` is the one raw-key translation left for a GUI subclass to own:
    the controller tool's ``command_table()`` already maps every action id to
    its command handler — this just resolves the key through it.
    """

    make_key_map = ViewToolMixin.make_key_map  # pyright: ignore[reportAssignmentType]
    _theme = ViewToolMixin._theme  # pyright: ignore[reportAssignmentType]

    def activate(self, session: object, view: object) -> None:
        super().activate(session, view)  # type: ignore[arg-type]
        gui_view = _gui_view(view)
        window = gui_view.window()
        set_insulated = getattr(window, "_set_wire_insulated", None)
        set_side = getattr(window, "_set_wire_side", None)
        update_status = getattr(window, "_update_route_status", None)
        self.bind_route_options(
            set_insulated=set_insulated if callable(set_insulated) else None,
            set_side=set_side if callable(set_side) else None,
            update_status=update_status if callable(update_status) else None,
        )
        if not self._preview_injected:
            from protoboard.gui.tools.route_preview import QtRoutePreview

            self._preview = QtRoutePreview(gui_view.scene, _theme_fn(self))
        if not self._merge_prompt_injected:
            from protoboard.gui.wire_net_resolver import qt_net_merge_prompt

            self._merge_prompt = qt_net_merge_prompt(lambda: gui_view.window())

    def on_key(self, inp: KeyInput) -> bool:
        return _dispatch_key_by_action_id(self, inp)


class ArmedTransformTool(
    _ArmedTransformTool, ViewToolMixin
):  # pyright: ignore[reportIncompatibleMethodOverride, reportIncompatibleVariableOverride]
    """ArmedTransformTool with the default pivot-marker preview + conflict overlay.

    ``on_key`` is the raw-key translation a GUI subclass owns: the controller
    tool's ``command_table()`` maps every action id (confirm/cancel/flip/
    rotate/move/step) to its command handler — this resolves the key to an id
    via the KeyMap and dispatches it through the Controller.
    """

    make_key_map = ViewToolMixin.make_key_map  # pyright: ignore[reportAssignmentType]
    _theme = ViewToolMixin._theme  # pyright: ignore[reportAssignmentType]

    def activate(self, session: object, view: object) -> None:
        from protoboard.gui.tools.select_preview import QtSelectPreview
        from protoboard.gui.tools.transform_overlays import TransformOverlays

        super().activate(session, view)  # type: ignore[arg-type]
        gui_view = _gui_view(view)
        self._preview = QtSelectPreview(gui_view.scene)
        self._overlays = TransformOverlays(gui_view)
        # The base activate() already drew pivot markers / published status
        # before this preview existed — redraw now that it does.
        self._draw_pivot_markers()

    def on_key(self, inp: KeyInput) -> bool:
        return _dispatch_key_by_action_id(self, inp)


class WireEditTool(
    _WireEditTool, ViewToolMixin
):  # pyright: ignore[reportIncompatibleMethodOverride, reportIncompatibleVariableOverride]
    """WireEditTool with the default Qt wire-edit / reroute preview.

    ``on_key`` is the raw-key translation a GUI subclass owns: the controller
    tool's ``command_table()`` maps every wire-edit op action id (reroute /
    autoroute / flip / simplify / delete / cancel) to its command handler —
    this resolves the key to an id via the KeyMap and dispatches it through the
    Controller.  The wire-edit + reroute overlays render through ``self.preview``
    (``QtSelectPreview``, the same overlay implementation SelectTool uses).
    """

    make_key_map = ViewToolMixin.make_key_map  # pyright: ignore[reportAssignmentType]
    _theme = ViewToolMixin._theme  # pyright: ignore[reportAssignmentType]

    def key_actions(self) -> dict[str, str | None]:
        """The wire-edit op keys, registered on this tool's own KeyMap layer.

        When ``[E]`` hands off to WireEditTool, SelectTool's layer is gone, so
        the op ids must be (re)declared here for the KeyMap to resolve them —
        otherwise the raw keys would fall through to the global map and the ops
        would be unreachable by keyboard.  ``None`` inherits the generic global
        default (delete / cancel); the reroute / autoroute / flip / simplify
        shortcuts match SelectTool's historical wire-edit bindings.
        """
        return {
            "select-tool.action.reroute": "W",
            "select-tool.action.autoroute": "A",
            "select-tool.action.flip_fragment": "F",
            "select-tool.action.simplify_fragment": "S",
            "select-tool.action.set_insulated_fragment": "I",
            "select-tool.action.delete": None,  # global.action.delete (Del)
            "select-tool.action.cancel": None,  # global.action.cancel (Esc)
        }

    def key_claims(self):  # type: ignore[no-untyped-def]
        """Veto the global shortcuts whose keys the wire-edit ops need while a
        fragment is armed — otherwise ``[W]`` would fire the global Route
        shortcut instead of reroute (the same veto SelectTool used to own for
        wire-edit).  Delete is claimed too so the global delete doesn't shadow
        the fragment delete."""
        armed = lambda: (  # noqa: E731
            self._wire_edit is not None or self._awaiting_repick
        )
        return (
            ("select-tool.action.reroute", armed),
            ("select-tool.action.autoroute", armed),
            ("select-tool.action.flip_fragment", armed),
            ("select-tool.action.simplify_fragment", armed),
            ("select-tool.action.set_insulated_fragment", armed),
            ("select-tool.action.delete", armed),
        )

    def activate(self, session: object, view: object) -> None:
        from protoboard.gui.tools.select_preview import QtSelectPreview

        # Inject the Qt preview BEFORE base activate() (it draws the fragment
        # overlay), matching PlaceTool's inject-then-activate ordering.
        self._preview = QtSelectPreview(view.scene)  # type: ignore[attr-defined]
        super().activate(session, view)  # type: ignore[arg-type]

    def on_key(self, inp: KeyInput) -> bool:
        from PySide6.QtCore import Qt

        from protoboard.controller.selection.mouse_input import Intent

        # A live reroute waypoint pick consumes Enter (commit) / Backspace
        # (drop last waypoint) before the op-keys — the same intent routing
        # gui SelectTool did for wire-edit.  Esc is handled by the host's
        # two-stage exit (cancel_op then can_exit), so it is not translated
        # here (it would double-cancel).
        key = inp.key
        if key in (Qt.Key.Key_Return.value, Qt.Key.Key_Enter.value):
            if self.commit_op():
                return True
        elif key == Qt.Key.Key_Backspace.value:
            if self._dispatch_intent(Intent.UNDO):
                return True
        return _dispatch_key_by_action_id(self, inp)


class SelectTool(
    _SelectTool, ViewToolMixin
):  # pyright: ignore[reportIncompatibleMethodOverride, reportIncompatibleVariableOverride]
    """SelectTool with the default Qt preview, group-target prompt, overlays.

    ``on_key`` is the raw-key → command translation the controller tool can't
    own itself (KeyMap action ids where bound, headless-safe raw-key
    fallbacks otherwise) — see the module docstring on
    :mod:`protoboard.controller.tools.select_tool` for the full list of
    command methods it dispatches into.
    """

    make_key_map = ViewToolMixin.make_key_map  # pyright: ignore[reportAssignmentType]
    _theme = ViewToolMixin._theme  # pyright: ignore[reportAssignmentType]

    def activate(self, session: object, view: object) -> None:
        gui_view = _gui_view(view)
        window = gui_view.window()
        assign_pin = getattr(window, "_pin_assign_net_for", None)
        autoroute_group = getattr(window, "_group_autoroute", None)
        self.bind_menu_actions(
            assign_pin=assign_pin if callable(assign_pin) else None,
            autoroute_group=autoroute_group if callable(autoroute_group) else None,
        )
        if not self._preview_injected:
            from protoboard.gui.tools.select_preview import QtSelectPreview

            self._preview = QtSelectPreview(gui_view.scene)
        if not self._group_prompt_injected:
            self._group_prompt = qt_group_target_prompt(lambda: gui_view.window())
        if not self._armed_transform_factory_injected:
            self._armed_transform_factory = ArmedTransformTool
        if not self._wire_edit_factory_injected:
            self._wire_edit_factory = WireEditTool
        super().activate(session, view)  # type: ignore[arg-type]
        from protoboard.gui.tools.transform_overlays import TransformOverlays

        self._overlays = TransformOverlays(gui_view)

    @staticmethod
    def _key_to_intent(key: int) -> Intent | None:
        """Map a Qt key to a semantic interaction intent.

        The one place keys become intents, so controllers never see raw
        keycodes.  A future revision can resolve this through the app's
        keybinding registry; the defaults match the conventional bindings.
        """
        from PySide6.QtCore import Qt

        from protoboard.controller.selection.mouse_input import Intent

        if key in (Qt.Key.Key_Return, Qt.Key.Key_Enter):
            return Intent.CONFIRM
        if key == Qt.Key.Key_Escape:
            return Intent.CANCEL
        if key == Qt.Key.Key_Backspace:
            return Intent.UNDO
        return None

    def on_key(self, inp: KeyInput) -> bool:
        from PySide6.QtCore import Qt

        key = inp.key
        ctrl = inp.mods.ctrl
        shift = inp.mods.shift
        alt = inp.mods.alt

        # Pivot mode: Esc clears it first.
        if self._pivot_capture_pending or self._rotate_pivot is not None:
            if key == Qt.Key.Key_Escape:
                self._clear_pivot()
                self._status("Pivot cleared")
                return True

        # Translate the key into a semantic intent and offer it to the active
        # mouse interaction (a floating drag, or a pick via PointPickController:
        # CONFIRM ends it, CANCEL aborts, UNDO drops the last pick).  One path —
        # no controller-type branching; the controller decides.  A wire-vertex
        # or reroute-waypoint pick is live here, so its Enter/Esc/Backspace
        # route to the pick before the op-keys get a turn.
        intent = self._key_to_intent(key)
        if intent is not None and self._dispatch_intent(intent):
            return True

        # Relation-axis cycling: backtick forward, Shift+backtick back.  Match
        # via the KeyMap (rebindable), with a raw Key_QuoteLeft fallback so it
        # works headless where the KeyMap may be absent.
        if self._key_matches(inp, _ACT_RELATION_CYCLE_BACK) or (
            key == Qt.Key.Key_QuoteLeft and shift
        ):
            self._cycle_relation(forward=False)
            return True
        if self._key_matches(inp, _ACT_RELATION_CYCLE) or (
            key == Qt.Key.Key_QuoteLeft and not shift
        ):
            self._cycle_relation(forward=True)
            return True

        if self._key_matches(inp, _ACT_DELETE) or (
            key == Qt.Key.Key_Backspace and not ctrl
        ):
            return self._delete_selection(force=shift)

        if self._key_matches(inp, _ACT_EDIT):
            self._begin_edit_selected()  # edit the selected object
            return True

        if key == Qt.Key.Key_R and ctrl:
            self.begin_redrag()  # Ctrl+R — re-drag the selected lead (legacy parity)
            return True

        if self._key_matches(inp, _ACT_MOVE) and not self._selection.is_empty():
            self.arm_move()  # [G] — keyboard float-move (arrows / WSAD)
            return True

        # Alt+R flips (immediate 2-pin flip, kept as a one-shot until flip joins
        # the armed transform mode); the bare rotate action arms float-rotate.
        if self._key_matches(inp, _ACT_FLIP) or (key == Qt.Key.Key_R and alt):
            self._flip_selected()
            return True

        if self._key_matches(inp, _ACT_ROTATE) and not ctrl:
            self.arm_rotate()
            return True

        if key == Qt.Key.Key_Escape:
            if len(self._group_stack) > 1:
                self.exit_group()
            else:
                self._selection.clear()
                self._context_net_id = None  # deselect drops the net context
                self._clear_free_pad_ring()
                self._refresh_highlight()
            return True
        return False

    def on_key_release(self, inp: KeyInput) -> None:
        pass


class SimplifyTool(
    _SimplifyTool, ViewToolMixin
):  # pyright: ignore[reportIncompatibleMethodOverride, reportIncompatibleVariableOverride]
    """SimplifyTool with the default Qt draft-overlay preview.

    ``on_key`` is just the ``[Esc]`` clear-draft raw-key check ahead of
    SelectTool's own ``on_key`` (KeyMap action ids + fallbacks).

    ``_SimplifyTool`` extends the *pure controller* ``SelectTool``
    (``controller.tools.select_tool.SelectTool``), not this module's
    Qt-composed one — so ``gui_tools.SelectTool.activate``'s preview/overlays
    injection is not in this class's MRO and has to be repeated here.
    """

    make_key_map = ViewToolMixin.make_key_map  # pyright: ignore[reportAssignmentType]
    _theme = ViewToolMixin._theme  # pyright: ignore[reportAssignmentType]

    def activate(self, session: object, view: object) -> None:
        gui_view = _gui_view(view)
        if not self._preview_injected:
            from protoboard.gui.tools.select_preview import QtSelectPreview

            self._preview = QtSelectPreview(gui_view.scene)
        super().activate(session, view)  # type: ignore[arg-type]
        from protoboard.gui.tools.transform_overlays import TransformOverlays

        self._overlays = TransformOverlays(gui_view)
        if not self._draft_preview_injected:
            from protoboard.gui.tools.simplify_preview import QtSimplifyPreview

            self._draft_preview = QtSimplifyPreview(
                self._view.scene, self._theme  # type: ignore[attr-defined]
            )

    def on_key(self, inp: KeyInput) -> bool:
        from PySide6.QtCore import Qt

        if inp.key == Qt.Key.Key_Escape:
            self._clear_draft()
        return super().on_key(inp)


# ---------------------------------------------------------------------------
# select_tool's Qt prompt factories (data types live in
# controller.tools.select_prompts; the default Qt prompt implementation stays
# gui-side, injected by SelectTool.activate()).
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
