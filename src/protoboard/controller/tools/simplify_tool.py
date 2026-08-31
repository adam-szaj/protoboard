# SPDX-License-Identifier: MIT
"""Simplify tool — Select-like behaviour with Shift+LPM = draft / apply.

Workflow:
* Plain LPM / RPM / drag / keyboard: inherited from :class:`SelectTool`
  (normal selection, rubber-band, move, etc.).
* Shift+LPM on a wire:
  - if no draft exists OR the draft is for a different wire → compute the
    simplified path and draw it as a dashed overlay (no command pushed).
  - if a draft already exists for THIS wire → commit it via
    ``ReplaceWireFragmentsCommand`` (atomic, undoable).  Draft cleared.
* ESC, switching tool, clicking off a wire, or any non-Shift click clears
  the draft.

This SHADOWS the parent's Shift+LPM relation-select semantics ONLY when
the hit target is a wire — clicks on components, groups or empty space
fall through to the parent.

Qt-free (controller/tools/): the ``[Esc]`` raw-key clear-draft check lives on
the GUI-composed subclass in :mod:`protoboard.gui.tools.gui_tools`; this
module keeps the draft/apply command methods.
"""

from __future__ import annotations

import uuid
from typing import TYPE_CHECKING

from protoboard.controller.ports import LEFT_BUTTON, PointerInput
from protoboard.controller.tools.select_tool import SelectTool
from protoboard.domain.commands.component_cmds import ReplaceWireFragmentsCommand
from protoboard.domain.layout.wire import Wire
from protoboard.domain.layout.wire_utils import rebuild_fragments_from_path
from protoboard.domain.routing.simplify_mode import SimplifyMode
from protoboard.geometry import Point

if TYPE_CHECKING:
    from protoboard.controller.editing.contexts import RoutingReadContext
    from protoboard.controller.ports.routing import SimplifyWire
    from protoboard.controller.tools.simplify_preview import SimplifyPreview


def compute_simplified_wire(
    session: RoutingReadContext,
    wire_id: uuid.UUID,
    simplify_wire: SimplifyWire,
    *,
    mode: SimplifyMode | None = None,
) -> Wire | None:
    """Compute the simplified path for *wire_id* without applying it.

    Returns the new ``Wire`` (with a fresh id) when a shortening is
    available, or ``None`` when ``generic_simplify_wire`` could not shorten
    the path further.  Callers wanting the raw path use ``result.path``.
    """
    layout = session.layout
    wire = layout.wires.get(wire_id)
    if wire is None:
        return None

    if mode is None:
        mode = session.routing_settings.simplify_mode

    from protoboard.controller.selection.wire_ops import build_simplify_inputs

    inputs = build_simplify_inputs(session)
    others = [w for w in layout.wires if w.id != wire_id]
    new_wire = simplify_wire(inputs.request(wire, others, mode))
    if new_wire is wire or new_wire.path == wire.path:
        return None
    return new_wire


class SimplifyTool(SelectTool):
    """SelectTool extension with Shift+LPM draft/apply on wires."""

    def __init__(self, draft_preview: SimplifyPreview | None = None) -> None:
        super().__init__()
        self._draft_wire_id: uuid.UUID | None = None
        self._draft_path: list[Point] | None = None
        # The dashed draft overlay behind the semantic preview seam.  An
        # injected preview (tests) survives re-activation; the default Qt
        # one binds the current view's scene on activate().
        self._draft_preview: SimplifyPreview | None = draft_preview
        self._draft_preview_injected = draft_preview is not None

    @property
    def draft_preview(self) -> SimplifyPreview:
        assert self._draft_preview is not None  # set by activate() / injection
        return self._draft_preview

    # ------------------------------------------------------------------
    # Lifecycle
    # ------------------------------------------------------------------
    #
    # The default Qt draft-overlay preview is injected by the GUI-composed
    # subclass's ``activate()`` in :mod:`protoboard.gui.tools.gui_tools`
    # (needs the real view's scene + theme); this base ``activate()`` is
    # inherited unchanged from :class:`SelectTool`.

    def deactivate(self) -> None:
        self._clear_draft()
        super().deactivate()

    def on_scene_cleared(self) -> None:
        # Scene items were destroyed under us — drop dangling refs.
        self.draft_preview.on_scene_cleared()
        self._draft_wire_id = None
        self._draft_path = None
        super().on_scene_cleared()

    # ------------------------------------------------------------------
    # Input handling
    # ------------------------------------------------------------------

    def on_press(self, inp: PointerInput) -> bool:
        # Shift+LPM on a wire → draft or apply (depending on whether the
        # draft already exists for this wire).
        if inp.button == LEFT_BUTTON and inp.mods.shift:
            wire_id = self._hit_test_wire(inp.scene_pos)
            if wire_id is not None:
                if self._draft_wire_id == wire_id and self._draft_path is not None:
                    return self._apply_simplify(wire_id)
                return self._preview_simplify(wire_id)
        # Any non-(Shift+LPM-on-wire) interaction discards the draft and
        # falls through to the parent SelectTool.
        self._clear_draft()
        return super().on_press(inp)

    # ``on_key`` (the ``[Esc]`` raw-key clear-draft check, then delegates to
    # SelectTool's ``on_key``) lives on the GUI-composed subclass — see
    # :mod:`protoboard.gui.tools.gui_tools`.

    # ------------------------------------------------------------------
    # Preview + apply
    # ------------------------------------------------------------------

    def _preview_simplify(self, wire_id: uuid.UUID) -> bool:
        new_wire = compute_simplified_wire(
            self._session,
            wire_id,
            self._session.edit_controller.simplify_wire,
        )
        self._clear_draft()
        if new_wire is None:
            self._status("Simplify: nothing to shorten on this wire.")
            return True
        self._draft_wire_id = wire_id
        self._draft_path = list(new_wire.path)
        self.draft_preview.show_draft(self._draft_path)
        self._status(
            "Simplify: draft ready — Shift+LPM again on the wire to apply,"
            " ESC or LPM elsewhere to cancel."
        )
        return True

    def _apply_simplify(self, wire_id: uuid.UUID) -> bool:
        if self._draft_path is None:
            return True
        new_path = list(self._draft_path)
        old_wire = self._session.layout.wires.get(wire_id)
        if old_wire is None:
            self._clear_draft()
            return True
        # Ensure the wire has knots before issuing a v2 fragment edit.  Most
        # runtime wires already have a/b (set on AddWireCommand or by the
        # load_project migration); test-built wires inserted directly into
        # layout.wires don't, so we backfill lazily here.
        self._session.layout.ensure_wire_knots(old_wire)
        # Simplify preserves per-fragment side topology — same-side wires
        # collapse into one fragment; multi-fragment wires keep their split
        # at the same via positions, which simplify never crosses.
        new_fragments = rebuild_fragments_from_path(old_wire, new_path)
        cmd = ReplaceWireFragmentsCommand(
            wire_id=wire_id,
            new_fragments=tuple(new_fragments),
        )
        self._execute_command_silent(cmd)
        self._session.notify_changed()
        self._clear_draft()
        self._status("Simplify: applied.")
        return True

    def _clear_draft(self) -> None:
        if self._draft_preview is not None:
            self._draft_preview.clear_draft()
        self._draft_wire_id = None
        self._draft_path = None
