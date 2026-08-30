# SPDX-License-Identifier: MIT
"""LayerState — rendering and hit-test context derived from camera + active tool.

Concepts
--------
camera_side : "top" | "bottom"
    Which physical side of the board the camera points at.
    Drives the z-stack: camera-side elements are in front, opposite side behind.
    Changing camera side also changes the active editing side (RouteTool._side).

view_transform : "normal" | "mirror_h" | "mirror_v" | "mirror_hv"
    Pure visual transformation of the scene, independent of camera side.
    normal    — no flip; right-top of board stays at right-top of screen.
    mirror_h  — flip around vertical axis (left↔right, i.e. negate X scale).
    mirror_v  — flip around horizontal axis (top↔bottom, i.e. negate Y scale).
    mirror_hv — both axes flipped; equivalent to a 180° rotation visually.
    These can be combined with either camera side → 6 distinct views.

overrides : LayerOverrides
    Per-layer manual visibility overrides set from the View → Layers menu.
    FULL respects tool-driven ghosting (back-side dimming).
    GHOST and HIDDEN override the automatic alpha entirely.

Z-value stack (back → front):
    0   back labels          4   front wires
    1   back components      5   front components
    2   back wires           6   front labels
    3   board (pads/strips)  7   overlay (ratsnest, groups)
    3.5 pin-pads (cherry)    9.5 pin-modifiers (top overlay)
                             10  selection highlights
"""

from __future__ import annotations

import uuid
from dataclasses import dataclass, field
from enum import StrEnum
from typing import Literal

from protoboard.types import Side

ViewTransform = Literal["normal", "mirror_h", "mirror_v", "mirror_hv"]

# ---------------------------------------------------------------------------
# Z-value anchors
# ---------------------------------------------------------------------------

Z_BACK_LABELS = 0.0
Z_BACK_COMPONENTS = 1.0
Z_BACK_WIRES = 2.0
Z_BOARD = 3.0
Z_PIN_PADS = 3.5
Z_FRONT_WIRES = 4.0
Z_FRONT_COMPONENTS = 5.0
Z_FRONT_LABELS = 6.0
Z_OVERLAY = 7.0
# Net-pad highlight (the bright-yellow discs drawn on a net's pads when the
# user selects a wire / net).  Sits ABOVE the coloured pads, wires, components
# and the back-side filter wash so the highlight reads cleanly, but BELOW the
# pin-modifier glyph (Z_PIN_MODIFIERS) — the modifier carries shape + colour
# information and is allowed to paint on top of the highlight.
Z_NET_PAD_HIGHLIGHT = 9.4
# Selected-wire highlight (the thick yellow overlay drawn along a selected
# wire's path).  Sits below the pin-modifier glyph so pin markers on the
# wire's route stay visible on top, but above pads/components/back-filter so
# the highlight reads cleanly.  Wire-edit *node* handles stay at Z_SELECTION
# (they must remain on top and clickable).
Z_WIRE_HIGHLIGHT = 9.4
Z_PIN_MODIFIERS = 9.5
# Front-side labels (component refdes/value, pin names, group names) sit above
# the pin-modifier glyphs so an on-demand label is never occluded by a pin
# decorator.  Back-side labels stay low (Z_BACK_LABELS) — they belong under the
# board when viewed from the other side.  See ``LayerState.z_for_label``.
Z_LABELS_TOP = 9.7
Z_SELECTION = 10.0

# Automatic alpha constants (used when override is AUTO).
# These are default values; the active theme may override them.
# Use theme.back_alpha / theme.board_ghost_alpha / theme.ghost_comp_alpha
# when you have a Theme instance.  These module-level names are kept for
# backward compatibility and for code that does not have a theme reference.
BACK_ALPHA = 0.30
# Back-side WIRE fragments only — kept clearly below the front so a fragment
# flipped to the other side reads as recessed (see theme.wire_back_alpha).
WIRE_BACK_ALPHA = 0.45
BOARD_GHOST_ALPHA = 0.55
GHOST_COMP_ALPHA = 0.25

# Alpha values per LayerVisibility state (GHOST/HIDDEN only; FULL is tool-driven)
_VIS_ALPHA = {
    "full": 1.0,
    "ghost": GHOST_COMP_ALPHA,
    "hidden": 0.0,
}
# Note: AUTO was removed; FULL now means "respect tool-driven ghosting".


# ---------------------------------------------------------------------------
# Per-layer manual visibility
# ---------------------------------------------------------------------------


class LayerVisibility(StrEnum):
    FULL = "full"  # always 100%
    GHOST = "ghost"  # always ~25%
    HIDDEN = "hidden"  # 0% (not drawn, not hit-tested)

    def cycle(self) -> LayerVisibility:
        """Cycle: FULL → GHOST → HIDDEN → FULL."""
        order = [LayerVisibility.FULL, LayerVisibility.GHOST, LayerVisibility.HIDDEN]
        idx = order.index(self)
        return order[(idx + 1) % len(order)]

    def label(self) -> str:
        """Short label for menu display."""
        return f"[{self.value.capitalize()}]"


@dataclass
class LayerOverrides:
    """Manual per-layer visibility overrides."""

    board: LayerVisibility = LayerVisibility.FULL
    components_top: LayerVisibility = LayerVisibility.FULL
    components_bottom: LayerVisibility = LayerVisibility.FULL
    wires_top: LayerVisibility = LayerVisibility.FULL
    wires_bottom: LayerVisibility = LayerVisibility.FULL
    labels: LayerVisibility = LayerVisibility.FULL
    meta_pin_labels: LayerVisibility = LayerVisibility.FULL
    # "ratsnest" is the legacy field name; UI displays it as "Connections".
    ratsnest: LayerVisibility = LayerVisibility.FULL
    axis_labels: LayerVisibility = LayerVisibility.FULL
    # Schematic-aware overlay above the board layer — colours every pad
    # that carries a net.  See ``domain/layout/pin_pad_classification.py``.
    pin_pads: LayerVisibility = LayerVisibility.FULL
    # Top-most modifier glyphs (square / diamond / star) repeated above
    # wires and components so pin 1 / exported markers stay visible even
    # when something sits on top of the pin pad.
    pin_modifiers: LayerVisibility = LayerVisibility.FULL

    def reset(self) -> None:
        """Reset all overrides to FULL."""
        for f in self.__dataclass_fields__:
            setattr(self, f, LayerVisibility.FULL)

    def all_full(self) -> bool:
        return all(
            getattr(self, f) == LayerVisibility.FULL for f in self.__dataclass_fields__
        )


# ---------------------------------------------------------------------------
# LayerState
# ---------------------------------------------------------------------------


@dataclass
class LayerState:
    """Single source of truth for layer rendering during one refresh()."""

    camera_side: Side = "top"
    view_transform: ViewTransform = "normal"
    ghost_components: bool = False
    overrides: LayerOverrides = field(default_factory=LayerOverrides)
    #: When the meta_pin_labels layer is HIDDEN, the one component id whose pin
    #: names are still drawn (hover+Alt reveal).  None = strict hidden.
    meta_pin_labels_reveal_cid: uuid.UUID | None = None
    back_alpha: float = BACK_ALPHA
    wire_back_alpha: float = WIRE_BACK_ALPHA
    board_ghost_alpha: float = BOARD_GHOST_ALPHA
    ghost_comp_alpha: float = GHOST_COMP_ALPHA
    # Alpha for the mid (second) blit of the board pixmap in the sandwich.
    # 0.30 → pads peek through under front-side items so they visually
    # rest on the board, but back-side items underneath stay readable.
    board_mid_alpha: float = 0.10
    # Alpha of the half-transparent black wash drawn between back-side
    # items and the mid board blit.  See ``alpha_for_back_filter``.
    back_filter_alpha: float = 0.10

    # ------------------------------------------------------------------
    # Z-value helpers
    # ------------------------------------------------------------------

    def front_side(self) -> Side:
        return self.camera_side

    def back_side(self) -> Side:
        return "bottom" if self.camera_side == "top" else "top"

    def z_for_wire(self, side: Side) -> float:
        return Z_FRONT_WIRES if side == self.camera_side else Z_BACK_WIRES

    def z_for_component(self, side: Side) -> float:
        return Z_FRONT_COMPONENTS if side == self.camera_side else Z_BACK_COMPONENTS

    def z_for_label(self, side: Literal["top", "bottom"]) -> float:
        # Front-side labels render on top of everything (above pin modifiers)
        # so an on-demand label is always fully visible; back-side labels stay
        # low, under the board, as before.
        return Z_LABELS_TOP if side == self.camera_side else Z_BACK_LABELS

    # ------------------------------------------------------------------
    # Alpha helpers — manual override takes priority over automatic logic
    # ------------------------------------------------------------------

    def _vis_alpha(self, vis: LayerVisibility, tool_alpha: float) -> float:
        """Resolve final alpha: override takes priority, else tool_alpha."""
        if vis == LayerVisibility.GHOST:
            return self.ghost_comp_alpha
        if vis == LayerVisibility.HIDDEN:
            return 0.0
        # FULL: still respect tool-driven ghosting (back-side dimming).
        return tool_alpha

    def alpha_for_component(self, side: Side) -> float:
        ov = (
            self.overrides.components_top
            if side == "top"
            else self.overrides.components_bottom
        )
        if side != self.camera_side:
            tool_alpha = self.back_alpha
        else:
            tool_alpha = self.ghost_comp_alpha if self.ghost_components else 1.0
        return self._vis_alpha(ov, tool_alpha)

    def alpha_for_wire(self, side: Side) -> float:
        ov = self.overrides.wires_top if side == "top" else self.overrides.wires_bottom
        # Wires use their own back alpha (well below the generic back_alpha)
        # so a fragment on the far side is clearly recessed, not a near-1.0
        # whisper away from the front side.
        tool_alpha = 1.0 if side == self.camera_side else self.wire_back_alpha
        return self._vis_alpha(ov, tool_alpha)

    def _side_tool_alpha(self, side: Side) -> float:
        """Side-only tool alpha: front = 1.0, back = back_alpha.

        Used by label / pin-name / wire helpers that must NOT inherit
        component-layer dimming (ghost-on-tool, components_top/bottom
        HIDDEN, etc.).  Hiding the components layer should not hide the
        labels printed next to them.
        """
        return 1.0 if side == self.camera_side else self.back_alpha

    def alpha_for_label(self, side: Side) -> float:
        # Single shared toggle for labels on both sides — the per-side
        # split (labels_top / labels_bottom) was merged into one.  Labels
        # are independent of the components layer (toggle components HIDDEN
        # still shows the refdes); the only inputs are side and override.
        return self._vis_alpha(self.overrides.labels, self._side_tool_alpha(side))

    def alpha_for_board(self) -> float:
        tool_alpha = self.board_ghost_alpha if self.ghost_components else 1.0
        return self._vis_alpha(self.overrides.board, tool_alpha)

    def alpha_for_board_mid(self) -> float:
        """Alpha for the second (mid) blit of the board pixmap.

        The board sandwich draws the board pixmap twice: once on the
        bottom (alpha_for_board), once in the middle of the Z stack so
        front-side items visually sit on the pads.  The mid blit is
        always partially transparent (board_mid_alpha) so it doesn't
        obscure back-side items, and it disappears in GHOST / HIDDEN
        modes so the user can study routing without pad clutter.
        """
        ov = self.overrides.board
        if ov == LayerVisibility.GHOST or ov == LayerVisibility.HIDDEN:
            return 0.0
        return self.board_mid_alpha

    def alpha_for_back_filter(self) -> float:
        """Alpha for the half-transparent back-side filter.

        The filter sits between back-side items (back wires, back
        components) and the mid board blit.  It darkens the back side
        independently of camera-side ghosting so the user can still
        tell front from back even when both sides are at high opacity.

        Active in FULL and GHOST modes; off in HIDDEN.
        """
        if self.overrides.board == LayerVisibility.HIDDEN:
            return 0.0
        return self.back_filter_alpha

    def alpha_for_meta_pin_labels(
        self,
        side: Side,
        cid: uuid.UUID | None = None,
    ) -> float:
        # Pin names are independent of labels and components — toggling
        # either of those does not hide pin names.  A per-component reveal
        # (hover+Alt) forces this one component's names on while the layer
        # is globally HIDDEN.
        if (
            self.overrides.meta_pin_labels == LayerVisibility.HIDDEN
            and cid is not None
            and cid == self.meta_pin_labels_reveal_cid
        ):
            return self._side_tool_alpha(side)
        return self._vis_alpha(
            self.overrides.meta_pin_labels, self._side_tool_alpha(side)
        )

    def alpha_for_axis_labels(self) -> float:
        return self._vis_alpha(self.overrides.axis_labels, 1.0)

    def alpha_for_pin_pads(self, side: Side) -> float:
        # Pin-pads are a schematic-aware overlay over board pads; they
        # follow the side-tool alpha (back side dimmed when looking at
        # top, and vice versa) but respect the manual override.
        return self._vis_alpha(self.overrides.pin_pads, self._side_tool_alpha(side))

    def alpha_for_pin_modifiers(self, side: Side) -> float:
        # Top-overlay glyphs.  Like pin_pads but a separate toggle so the
        # user can hide modifiers while keeping the coloured pads, or
        # vice versa.
        return self._vis_alpha(
            self.overrides.pin_modifiers, self._side_tool_alpha(side)
        )

    # ------------------------------------------------------------------
    # Hit-testing
    # ------------------------------------------------------------------

    def hit_priority_component(self, side: Side) -> int:
        """Higher = checked first.  Hidden components return -1 (skip)."""
        if self.alpha_for_component(side) == 0.0:
            return -1
        if side != self.camera_side:
            return 0
        return 1 if self.ghost_components else 2

    # ------------------------------------------------------------------
    # View transform helpers
    # ------------------------------------------------------------------

    def scene_scale_x(self) -> float:
        return -1.0 if self.view_transform in ("mirror_h", "mirror_hv") else 1.0

    def scene_scale_y(self) -> float:
        return -1.0 if self.view_transform in ("mirror_v", "mirror_hv") else 1.0

    def needs_text_counter_transform(self) -> bool:
        return self.view_transform != "normal"


# ---------------------------------------------------------------------------
# Factory
# ---------------------------------------------------------------------------


def layer_state_for_tool(
    tool: object,
    camera_side: Side,
    view_transform: ViewTransform = "normal",
    overrides: LayerOverrides | None = None,
    theme: object = None,
) -> LayerState:
    """Derive LayerState from the active tool, camera side and view transform."""
    from protoboard.gui.tools.gui_tools import PlaceTool, RouteTool

    ghost = isinstance(tool, (RouteTool, PlaceTool))
    state = LayerState(
        camera_side=camera_side,
        view_transform=view_transform,
        ghost_components=ghost,
        overrides=overrides if overrides is not None else LayerOverrides(),
    )
    if theme is not None:
        import dataclasses

        from protoboard.controller.theme import Theme

        if isinstance(theme, Theme):
            state = dataclasses.replace(
                state,
                back_alpha=theme.back_alpha,
                wire_back_alpha=theme.wire_back_alpha,
                board_ghost_alpha=theme.board_ghost_alpha,
                ghost_comp_alpha=theme.ghost_comp_alpha,
                board_mid_alpha=theme.board_mid_alpha,
                back_filter_alpha=theme.back_filter_alpha,
            )
    return state
