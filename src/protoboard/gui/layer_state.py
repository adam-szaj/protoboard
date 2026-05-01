# SPDX-License-Identifier: MIT
"""LayerState — rendering and hit-test context derived from camera + active tool.

Concepts
--------
camera_side : "top" | "bottom"
    Which physical side of the board the camera is pointing at.
    "top"    → normal view (looking from above).
    "bottom" → mirrored view (looking from below, after toggle_mirror()).
    Camera side drives the entire z-stack: the camera-side layer is always
    in front; the opposite side is behind the board.

active_side : "top" | "bottom"
    Which side is being edited (Route Wire, Place Component).
    Always equals camera_side — you edit the side you are looking at.
    RouteTool._side is synchronised with camera_side when mirror changes.

ghost_components : bool
    When True (during routing or placement) components are drawn at reduced
    alpha so wire paths are readable.  Hit-testing treats ghosted components
    as lowest priority.

Z-value stack (bottom of stack → top of stack):
    back_labels          (opposite side labels)
    back_components      (opposite side components)
    back_wires           (opposite side wires)
    board                (pads/strips — separates front from back)
    front_wires          (camera-side wires)
    front_components     (camera-side components)
    front_labels         (camera-side labels)
    overlay              (ratsnest, group outlines)
    selection            (always on top)

Numeric anchors — renderers add ±0.05 for outline/core sub-layers.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Literal

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

# Alpha applied to the "back" side (behind the board).
BACK_ALPHA         = 0.30   # components and wires on the far side
BOARD_GHOST_ALPHA  = 0.55   # board pads when dimmed over back layer
GHOST_COMP_ALPHA   = 0.25   # components during routing / placement


@dataclass
class LayerState:
    """Single source of truth for layer rendering during one refresh()."""

    # Which physical side the camera looks at.  Equals active_side.
    camera_side: Literal["top", "bottom"] = "top"

    # Ghost mode — components rendered at reduced alpha.
    ghost_components: bool = False

    def front_side(self) -> Literal["top", "bottom"]:
        """The side rendered in front (same as camera_side)."""
        return self.camera_side

    def back_side(self) -> Literal["top", "bottom"]:
        return "bottom" if self.camera_side == "top" else "top"

    def z_for_wire(self, side: Literal["top", "bottom"]) -> float:
        """Base z-value for a wire on the given physical side."""
        return Z_FRONT_WIRES if side == self.camera_side else Z_BACK_WIRES

    def z_for_component(self, side: Literal["top", "bottom"]) -> float:
        return Z_FRONT_COMPONENTS if side == self.camera_side else Z_BACK_COMPONENTS

    def z_for_label(self, side: Literal["top", "bottom"]) -> float:
        return Z_FRONT_LABELS if side == self.camera_side else Z_BACK_LABELS

    def alpha_for_component(self, side: Literal["top", "bottom"]) -> float:
        """Effective alpha multiplier for a component on *side*."""
        if side != self.camera_side:
            return BACK_ALPHA
        if self.ghost_components:
            return GHOST_COMP_ALPHA
        return 1.0

    def alpha_for_wire(self, side: Literal["top", "bottom"]) -> float:
        """Effective alpha multiplier for a wire on *side*."""
        return 1.0 if side == self.camera_side else BACK_ALPHA

    def hit_priority_component(self, side: Literal["top", "bottom"]) -> int:
        """Hit-test priority for components: higher = checked first.

        Front-side components have priority 2; back-side have priority 0.
        When ghosted, front-side drops to 1 (wires are checked first).
        """
        if side != self.camera_side:
            return 0
        return 1 if self.ghost_components else 2


# ---------------------------------------------------------------------------
# Factory
# ---------------------------------------------------------------------------


def layer_state_for_tool(
    tool: object,
    camera_side: Literal["top", "bottom"],
) -> LayerState:
    """Derive a LayerState from the active tool and current camera side."""
    from protoboard.gui.tools.route_tool import RouteTool
    from protoboard.gui.tools.place_tool import PlaceTool

    if isinstance(tool, RouteTool):
        return LayerState(camera_side=camera_side, ghost_components=True)
    if isinstance(tool, PlaceTool):
        return LayerState(camera_side=camera_side, ghost_components=True)
    return LayerState(camera_side=camera_side)
