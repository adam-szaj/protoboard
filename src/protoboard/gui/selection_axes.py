# SPDX-License-Identifier: MIT
"""Orthogonal selection axes — filter (which object types) × relation (which
correlated set).

Replaces the old InteractionMode preset system.  These primitives are consumed
by SelectTool and the status-bar widgets (SelectionFilterWidget /
HighlightModeWidget).  No Qt imports — pure domain types.

LPM (no Shift)
    selection = clicked object only
    auto-highlight = full relation set (filter ignored — show wide context)

Shift+LPM
    selection = relation ∩ filter
    auto-highlight = full relation set (same context as LPM)

RelationClass.NONE disables auto-highlight entirely.
"""

from __future__ import annotations

from dataclasses import dataclass
from enum import Enum, Flag, auto


class SelectionFilter(Flag):
    """Which object types may enter the selection on a click."""

    NONE = 0
    COMPONENTS = auto()
    WIRES = auto()
    GROUPS = auto()
    ALL = COMPONENTS | WIRES | GROUPS


class RelationClass(Enum):
    """Which set of objects is correlated to the clicked object.

    Drives both Shift+LPM selection (intersected with the filter) and the
    auto-highlight that follows any LPM click.
    """

    NONE = "none"
    SAME_LOGICAL_NET = "same_logical_net"        # default
    SAME_PHYSICAL_NET = "same_physical_net"
    UNCONNECTED_PINS = "unconnected_pins"        # logical net minus already connected
    NEIGHBORS = "neighbors"                      # comps sharing any net with clicked


# Human-readable labels for status-bar dropdowns.
RELATION_LABELS: dict[RelationClass, str] = {
    RelationClass.NONE: "None",
    RelationClass.SAME_LOGICAL_NET: "Same logical net",
    RelationClass.SAME_PHYSICAL_NET: "Same physical net",
    RelationClass.UNCONNECTED_PINS: "Unconnected pins",
    RelationClass.NEIGHBORS: "Neighbors",
}


@dataclass(frozen=True)
class RelationProfile:
    """Visual parameters used while rendering a relation-driven highlight.

    Mirrors the relevant subset of the legacy HighlightProfile so BoardView's
    dimming code can consume it without branching on profile type.  Kept here
    instead of highlight_state.py because the profile is *per relation class*,
    not per stack entry.
    """

    pad_marker_rgba: tuple[int, int, int, int] = (255, 220, 0, 160)
    pad_marker_radius: float = 0.28
    unconnected_rgba: tuple[int, int, int, int] = (255, 140, 0, 200)
    unconnected_dashed: bool = False
    neighbor_rect_rgba: tuple[int, int, int, int] = (0, 255, 120, 25)
    neighbor_pen_rgba: tuple[int, int, int, int] = (0, 255, 120, 210)
    # Auto-highlight uses Dimmed #1 levels — context visible, focus on the relation.
    dim_components: float = 0.45
    dim_wires: float = 0.45


RELATION_PROFILES: dict[RelationClass, RelationProfile] = {
    RelationClass.NONE: RelationProfile(),  # never read — guarded by NONE check
    RelationClass.SAME_LOGICAL_NET: RelationProfile(
        pad_marker_rgba=(255, 220, 0, 160),
        pad_marker_radius=0.28,
    ),
    RelationClass.SAME_PHYSICAL_NET: RelationProfile(
        pad_marker_rgba=(220, 40, 40, 160),
        pad_marker_radius=0.30,
    ),
    RelationClass.UNCONNECTED_PINS: RelationProfile(
        unconnected_rgba=(255, 140, 0, 200),
        unconnected_dashed=True,
    ),
    RelationClass.NEIGHBORS: RelationProfile(
        neighbor_rect_rgba=(0, 255, 120, 25),
        neighbor_pen_rgba=(0, 255, 120, 210),
        dim_components=1.0,
    ),
}


# Persistence key mapping (filter Flag → list of strings, and back).

_FILTER_TOKENS: dict[SelectionFilter, str] = {
    SelectionFilter.COMPONENTS: "components",
    SelectionFilter.WIRES: "wires",
    SelectionFilter.GROUPS: "groups",
}


def filter_to_tokens(f: SelectionFilter) -> list[str]:
    """Serialise SelectionFilter to a list of stable string tokens."""
    return [tok for flag, tok in _FILTER_TOKENS.items() if flag in f]


def filter_from_tokens(tokens: list[str] | None) -> SelectionFilter:
    """Parse a list of tokens back into SelectionFilter; missing → ALL."""
    if tokens is None:
        return SelectionFilter.ALL
    f = SelectionFilter.NONE
    inv = {tok: flag for flag, tok in _FILTER_TOKENS.items()}
    for t in tokens:
        flag = inv.get(t)
        if flag is not None:
            f |= flag
    return f if f != SelectionFilter.NONE else SelectionFilter.ALL


def relation_from_str(s: str | None) -> RelationClass:
    """Parse a relation enum value string; missing/invalid → SAME_LOGICAL_NET."""
    if not s:
        return RelationClass.SAME_LOGICAL_NET
    try:
        return RelationClass(s)
    except ValueError:
        return RelationClass.SAME_LOGICAL_NET
