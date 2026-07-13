# SPDX-License-Identifier: MIT
"""Visual highlight profile — colours, dim levels, marker dimensions.

Originally part of a larger InteractionMode preset system; the rest of that
system has been replaced by the orthogonal axes in
``protoboard.controller.selection_axes`` (SelectionFilter × RelationClass).

This module is kept for ``HighlightProfile``, which is consumed by
``HighlightStack`` entries (PROFILE_DIMMED_1 / PROFILE_DIMMED_2 in
``highlight_state``) and by ``BoardView._apply_highlight_opacity`` when
reading the dim values off the active entry.
"""

from __future__ import annotations

from dataclasses import dataclass


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
    # Opacity applied to *highlighted* components (the items the user
    # actually wants to see).  Default 1.0 keeps the historic
    # "highlighted = fully opaque" behaviour.  RouteTool overrides this
    # to ~0.4 during an active wire drag so the user can see pin
    # geometry hidden under a body (DIP, SOIC, etc.).
    highlight_alpha: float = 1.0
