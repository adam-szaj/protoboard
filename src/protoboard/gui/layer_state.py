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

QTransform matrix for each combination:
    scale_x = -1 if mirror_h else +1
    scale_y = -1 if mirror_v else +1
    QTransform(scale_x * base_px, 0, 0, scale_y * base_px, 0, 0)

Z-value stack (back → front):
    0   back labels          4   front wires
    1   back components      5   front components
    2   back wires           6   front labels
    3   board (pads/strips)  7   overlay (ratsnest, groups)
                             10  selection highlights
"""

from __future__ import annotations

from dataclasses import dataclass
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

# Alpha constants
BACK_ALPHA        = 0.30
BOARD_GHOST_ALPHA = 0.55
GHOST_COMP_ALPHA  = 0.25


@dataclass
class LayerState:
    """Single source of truth for layer rendering during one refresh()."""

    camera_side: Literal["top", "bottom"] = "top"
    view_transform: ViewTransform = "normal"
    ghost_components: bool = False

    # ------------------------------------------------------------------
    # Derived helpers
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

    def alpha_for_component(self, side: Literal["top", "bottom"]) -> float:
        if side != self.camera_side:
            return BACK_ALPHA
        return GHOST_COMP_ALPHA if self.ghost_components else 1.0

    def alpha_for_wire(self, side: Literal["top", "bottom"]) -> float:
        return 1.0 if side == self.camera_side else BACK_ALPHA

    def hit_priority_component(self, side: Literal["top", "bottom"]) -> int:
        """Higher = checked first during hit-testing."""
        if side != self.camera_side:
            return 0
        return 1 if self.ghost_components else 2

    def scene_scale_x(self) -> float:
        """X scale factor for QGraphicsView (±1)."""
        return -1.0 if self.view_transform == "mirror_h" else 1.0

    def scene_scale_y(self) -> float:
        """Y scale factor for QGraphicsView (±1)."""
        return -1.0 if self.view_transform == "mirror_v" else 1.0

    def needs_text_counter_transform(self) -> bool:
        """True when the scene is flipped and text items need un-mirroring."""
        return self.view_transform != "normal"


# ---------------------------------------------------------------------------
# Factory
# ---------------------------------------------------------------------------


def layer_state_for_tool(
    tool: object,
    camera_side: Literal["top", "bottom"],
    view_transform: ViewTransform = "normal",
) -> LayerState:
    """Derive LayerState from the active tool, camera side and view transform."""
    from protoboard.gui.tools.route_tool import RouteTool
    from protoboard.gui.tools.place_tool import PlaceTool

    ghost = isinstance(tool, (RouteTool, PlaceTool))
    return LayerState(
        camera_side=camera_side,
        view_transform=view_transform,
        ghost_components=ghost,
    )
