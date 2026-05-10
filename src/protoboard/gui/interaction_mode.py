# SPDX-License-Identifier: MIT
"""Interaction mode system: defines what gets selected and highlighted on click.

Adding a new mode: add one entry to INTERACTION_MODES. No other code changes needed.
"""
from __future__ import annotations

from dataclasses import dataclass, field
from enum import Enum, auto


class SelectionTarget(Enum):
    """Which objects enter _selected_ids / _selected_wire_ids after a click."""

    NOTHING = auto()          # click does not change selection
    CLICKED_OBJECT = auto()   # select the clicked object (legacy behaviour)
    CORRELATED_COMPS = auto() # select components correlated to the clicked object
    CORRELATED_WIRES = auto() # select wires correlated to the clicked object
    CORRELATED_ALL = auto()   # select all correlated objects (comps + wires)


class HighlightScope(Enum):
    """Which objects are visually highlighted."""

    NONE = auto()                 # no highlight
    CLICKED_OBJECT = auto()       # only the clicked object + dim the rest
    PHYSICAL_NET = auto()         # physical net: pads + wires in the same electric cluster
    LOGICAL_NET_ALL = auto()      # logical net: all pins assigned to the net
    LOGICAL_UNCONNECTED = auto()  # logical net: only pins not yet physically connected
    NEIGHBORS_ALL = auto()        # all comps/wires sharing any net with the source


@dataclass(frozen=True)
class HighlightProfile:
    """Visual parameters for one highlight scope — no Qt imports."""

    pad_marker_rgba: tuple[int, int, int, int] = (255, 220, 0, 160)
    pad_marker_radius: float = 0.28
    unconnected_rgba: tuple[int, int, int, int] = (255, 140, 0, 180)
    unconnected_dashed: bool = False
    neighbor_rect_rgba: tuple[int, int, int, int] = (0, 255, 120, 25)
    neighbor_pen_rgba: tuple[int, int, int, int] = (0, 255, 120, 210)
    dim_components: float = 0.20
    dim_wires: float = 0.20


@dataclass(frozen=True)
class InteractionMode:
    """Full specification of click behaviour: what to select and what to highlight.

    One mode = one coherent policy. Add entries to INTERACTION_MODES to extend.
    """

    id: str
    label: str
    selection_target: SelectionTarget
    highlight_scope: HighlightScope
    highlight_profile: HighlightProfile = field(default_factory=HighlightProfile)
    description: str = ""


INTERACTION_MODES: dict[str, InteractionMode] = {
    "default": InteractionMode(
        id="default",
        label="Default",
        selection_target=SelectionTarget.CLICKED_OBJECT,
        highlight_scope=HighlightScope.NONE,
        description="Select clicked object, no connectivity highlight.",
    ),
    "net_logical_all": InteractionMode(
        id="net_logical_all",
        label="Net (Logical)",
        selection_target=SelectionTarget.CLICKED_OBJECT,
        highlight_scope=HighlightScope.LOGICAL_NET_ALL,
        highlight_profile=HighlightProfile(
            pad_marker_rgba=(255, 220, 0, 160),
            pad_marker_radius=0.28,
            dim_components=0.20,
            dim_wires=0.20,
        ),
        description="Select clicked object; highlight all pins in the same logical net.",
    ),
    "net_physical": InteractionMode(
        id="net_physical",
        label="Net (Physical)",
        selection_target=SelectionTarget.CLICKED_OBJECT,
        highlight_scope=HighlightScope.PHYSICAL_NET,
        highlight_profile=HighlightProfile(
            pad_marker_rgba=(220, 40, 40, 160),
            pad_marker_radius=0.30,
            dim_components=0.20,
            dim_wires=0.20,
        ),
        description="Select clicked object; highlight physically connected pads and wires.",
    ),
    "net_unconnected": InteractionMode(
        id="net_unconnected",
        label="Unconnected Pins",
        selection_target=SelectionTarget.CLICKED_OBJECT,
        highlight_scope=HighlightScope.LOGICAL_UNCONNECTED,
        highlight_profile=HighlightProfile(
            unconnected_rgba=(255, 140, 0, 200),
            unconnected_dashed=True,
            dim_components=0.20,
            dim_wires=0.20,
        ),
        description="Highlight only unconnected pins in the same logical net.",
    ),
    "select_connected_comps": InteractionMode(
        id="select_connected_comps",
        label="Select Neighbors",
        selection_target=SelectionTarget.CORRELATED_COMPS,
        highlight_scope=HighlightScope.NEIGHBORS_ALL,
        highlight_profile=HighlightProfile(
            neighbor_rect_rgba=(0, 255, 120, 25),
            neighbor_pen_rgba=(0, 255, 120, 210),
            dim_components=1.0,
            dim_wires=0.20,
        ),
        description="Clicking a wire selects all components connected to that net.",
    ),
    "inspect_only": InteractionMode(
        id="inspect_only",
        label="Inspect",
        selection_target=SelectionTarget.NOTHING,
        highlight_scope=HighlightScope.LOGICAL_NET_ALL,
        highlight_profile=HighlightProfile(
            pad_marker_rgba=(255, 220, 0, 160),
            dim_components=0.20,
            dim_wires=0.20,
        ),
        description="Highlight net without changing selection (inspection only).",
    ),
}


def get_mode(id_: str) -> InteractionMode:
    """Return mode by id. Raises KeyError for unknown ids."""
    return INTERACTION_MODES[id_]
