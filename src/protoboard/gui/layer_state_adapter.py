# SPDX-License-Identifier: MIT
"""GUI adapter that derives layer rendering state from the active tool."""

from __future__ import annotations

import dataclasses

from protoboard.controller.view_options.layer_state import LayerState
from protoboard.controller.view_options.layers import LayerOverrides, ViewTransform
from protoboard.controller.view_options.theme import Theme
from protoboard.gui.tools.gui_tools import PlaceTool, RouteTool
from protoboard.types import Side

__all__ = ["layer_state_for_tool"]


def layer_state_for_tool(
    tool: object,
    camera_side: Side,
    view_transform: ViewTransform = "normal",
    overrides: LayerOverrides | None = None,
    theme: Theme | None = None,
) -> LayerState:
    """Derive layer state from GUI tool, camera, transform, and theme."""
    state = LayerState(
        camera_side=camera_side,
        view_transform=view_transform,
        ghost_components=isinstance(tool, (RouteTool, PlaceTool)),
        overrides=overrides if overrides is not None else LayerOverrides(),
    )
    if theme is None:
        return state
    return dataclasses.replace(
        state,
        back_alpha=theme.back_alpha,
        wire_back_alpha=theme.wire_back_alpha,
        board_ghost_alpha=theme.board_ghost_alpha,
        ghost_comp_alpha=theme.ghost_comp_alpha,
        board_mid_alpha=theme.board_mid_alpha,
        back_filter_alpha=theme.back_filter_alpha,
    )
