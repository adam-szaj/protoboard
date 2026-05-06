# SPDX-License-Identifier: MIT
"""LayerState — rendering and hit-test context derived from camera + active tool.

Concepts
--------
camera_side : "top" | "bottom"
    Which physical side of the board the camera points at.
    Drives the z-stack: camera-side elements are in front, opposite side behind.
    Changing camera side also changes the active editing side (RouteTool._side).

view_transform : "normal" | "mirror_h" | "mirror_v"
    Pure visual transformation of the scene, independent of camera side.
    normal   — no flip; right-top of board stays at right-top of screen.
    mirror_h — flip around vertical axis (left↔right, i.e. negate X scale).
    mirror_v — flip around horizontal axis (top↔bottom, i.e. negate Y scale).
    These can be combined with either camera side → 6 distinct views.

overrides : LayerOverrides
    Per-layer manual visibility overrides set from the View → Layers menu.
    AUTO means "let the tool decide"; other states override the automatic alpha.
    Manual overrides take priority over automatic ghost_components logic.

Z-value stack (back → front):
    0   back labels          4   front wires
    1   back components      5   front components
    2   back wires           6   front labels
    3   board (pads/strips)  7   overlay (ratsnest, groups)
                             10  selection highlights
"""

from __future__ import annotations

from dataclasses import dataclass, field
from enum import Enum
from typing import Literal

ViewTransform = Literal["normal", "mirror_h", "mirror_v"]

# ---------------------------------------------------------------------------
# Z-value anchors
# ---------------------------------------------------------------------------

Z_BACK_LABELS      = 0.0
Z_BACK_COMPONENTS  = 1.0
Z_BACK_WIRES       = 2.0
Z_BOARD            = 3.0
Z_FRONT_WIRES      = 4.0
Z_FRONT_COMPONENTS = 5.0
Z_FRONT_LABELS     = 6.0
Z_OVERLAY          = 7.0
Z_SELECTION        = 10.0

# Automatic alpha constants (used when override is AUTO).
# These are default values; the active theme may override them.
# Use theme.back_alpha / theme.board_ghost_alpha / theme.ghost_comp_alpha
# when you have a Theme instance.  These module-level names are kept for
# backward compatibility and for code that does not have a theme reference.
BACK_ALPHA        = 0.30
BOARD_GHOST_ALPHA = 0.55
GHOST_COMP_ALPHA  = 0.25

# Alpha values per LayerVisibility state
_VIS_ALPHA = {
    "full":   1.0,
    "ghost":  GHOST_COMP_ALPHA,
    "hidden": 0.0,
}


# ---------------------------------------------------------------------------
# Per-layer manual visibility
# ---------------------------------------------------------------------------

class LayerVisibility(str, Enum):
    AUTO   = "auto"    # automatic (tool-driven)
    FULL   = "full"    # always 100%
    GHOST  = "ghost"   # always ~25%
    HIDDEN = "hidden"  # 0% (not drawn, not hit-tested)

    def cycle(self) -> LayerVisibility:
        """Cycle: AUTO → FULL → GHOST → HIDDEN → AUTO."""
        order = [LayerVisibility.AUTO, LayerVisibility.FULL,
                 LayerVisibility.GHOST, LayerVisibility.HIDDEN]
        idx = order.index(self)
        return order[(idx + 1) % len(order)]

    def label(self) -> str:
        """Short label for menu display; empty string when AUTO."""
        return "" if self == LayerVisibility.AUTO else f"[{self.value.capitalize()}]"


@dataclass
class LayerOverrides:
    """Manual per-layer visibility overrides.  AUTO = no override (tool decides)."""
    components_top:    LayerVisibility = LayerVisibility.AUTO
    components_bottom: LayerVisibility = LayerVisibility.AUTO
    wires_top:         LayerVisibility = LayerVisibility.AUTO
    wires_bottom:      LayerVisibility = LayerVisibility.AUTO
    labels_top:        LayerVisibility = LayerVisibility.AUTO
    labels_bottom:     LayerVisibility = LayerVisibility.AUTO
    board:             LayerVisibility = LayerVisibility.AUTO

    def reset(self) -> None:
        """Reset all overrides to AUTO."""
        for f in self.__dataclass_fields__:
            setattr(self, f, LayerVisibility.AUTO)

    def all_auto(self) -> bool:
        return all(
            getattr(self, f) == LayerVisibility.AUTO
            for f in self.__dataclass_fields__
        )


# ---------------------------------------------------------------------------
# LayerState
# ---------------------------------------------------------------------------

@dataclass
class LayerState:
    """Single source of truth for layer rendering during one refresh()."""

    camera_side: Literal["top", "bottom"] = "top"
    view_transform: ViewTransform = "normal"
    ghost_components: bool = False
    overrides: LayerOverrides = field(default_factory=LayerOverrides)
    back_alpha: float = BACK_ALPHA
    board_ghost_alpha: float = BOARD_GHOST_ALPHA
    ghost_comp_alpha: float = GHOST_COMP_ALPHA

    # ------------------------------------------------------------------
    # Z-value helpers
    # ------------------------------------------------------------------

    def front_side(self) -> Literal["top", "bottom"]:
        return self.camera_side

    def back_side(self) -> Literal["top", "bottom"]:
        return "bottom" if self.camera_side == "top" else "top"

    def z_for_wire(self, side: Literal["top", "bottom"]) -> float:
        return Z_FRONT_WIRES if side == self.camera_side else Z_BACK_WIRES

    def z_for_component(self, side: Literal["top", "bottom"]) -> float:
        return Z_FRONT_COMPONENTS if side == self.camera_side else Z_BACK_COMPONENTS

    def z_for_label(self, side: Literal["top", "bottom"]) -> float:
        return Z_FRONT_LABELS if side == self.camera_side else Z_BACK_LABELS

    # ------------------------------------------------------------------
    # Alpha helpers — manual override takes priority over automatic logic
    # ------------------------------------------------------------------

    def _override_alpha(self, vis: LayerVisibility, auto_value: float) -> float:
        if vis == LayerVisibility.AUTO:
            return auto_value
        if vis == LayerVisibility.GHOST:
            return self.ghost_comp_alpha
        return _VIS_ALPHA[vis.value]

    def alpha_for_component(self, side: Literal["top", "bottom"]) -> float:
        ov = (self.overrides.components_top
              if side == "top" else self.overrides.components_bottom)
        if side != self.camera_side:
            return self._override_alpha(ov, self.back_alpha)
        return self._override_alpha(
            ov, self.ghost_comp_alpha if self.ghost_components else 1.0
        )

    def alpha_for_wire(self, side: Literal["top", "bottom"]) -> float:
        ov = self.overrides.wires_top if side == "top" else self.overrides.wires_bottom
        auto = 1.0 if side == self.camera_side else self.back_alpha
        return self._override_alpha(ov, auto)

    def alpha_for_label(self, side: Literal["top", "bottom"]) -> float:
        ov = self.overrides.labels_top if side == "top" else self.overrides.labels_bottom
        # Labels follow component alpha by default when AUTO.
        auto = self.alpha_for_component(side)
        return self._override_alpha(ov, auto)

    def alpha_for_board(self) -> float:
        auto = self.board_ghost_alpha if self.ghost_components else 1.0
        return self._override_alpha(self.overrides.board, auto)

    # ------------------------------------------------------------------
    # Hit-testing
    # ------------------------------------------------------------------

    def hit_priority_component(self, side: Literal["top", "bottom"]) -> int:
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
        return -1.0 if self.view_transform == "mirror_h" else 1.0

    def scene_scale_y(self) -> float:
        return -1.0 if self.view_transform == "mirror_v" else 1.0

    def needs_text_counter_transform(self) -> bool:
        return self.view_transform != "normal"


# ---------------------------------------------------------------------------
# Factory
# ---------------------------------------------------------------------------


def layer_state_for_tool(
    tool: object,
    camera_side: Literal["top", "bottom"],
    view_transform: ViewTransform = "normal",
    overrides: LayerOverrides | None = None,
    theme: object = None,
) -> LayerState:
    """Derive LayerState from the active tool, camera side and view transform."""
    from protoboard.gui.tools.route_tool import RouteTool
    from protoboard.gui.tools.place_tool import PlaceTool

    ghost = isinstance(tool, (RouteTool, PlaceTool))
    state = LayerState(
        camera_side=camera_side,
        view_transform=view_transform,
        ghost_components=ghost,
        overrides=overrides if overrides is not None else LayerOverrides(),
    )
    if theme is not None:
        import dataclasses
        from protoboard.gui.theme import Theme
        if isinstance(theme, Theme):
            state = dataclasses.replace(
                state,
                back_alpha=theme.back_alpha,
                board_ghost_alpha=theme.board_ghost_alpha,
                ghost_comp_alpha=theme.ghost_comp_alpha,
            )
    return state
