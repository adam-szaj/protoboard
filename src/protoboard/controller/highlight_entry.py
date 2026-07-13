# SPDX-License-Identifier: MIT
"""``HighlightEntry`` — one entry on the highlight stack, and its profiles.

Pure data (Qt-free): the stack mechanism itself (``HighlightStack``, a
``QObject`` emitting ``changed``) stays in ``gui.highlight_state``, which
re-exports the names below for existing GUI importers.  See that module's
docstring for the stack's precedence/tier architecture.
"""

from __future__ import annotations

import uuid
from dataclasses import dataclass, field

from protoboard.controller.interaction_mode import HighlightProfile

# ---------------------------------------------------------------------------
# Pre-defined profiles
# ---------------------------------------------------------------------------

PROFILE_DIMMED_1 = HighlightProfile(
    pad_marker_rgba=(255, 220, 0, 160),
    pad_marker_radius=0.28,
    unconnected_rgba=(255, 140, 0, 180),
    dim_components=0.45,
    dim_wires=0.45,
)

PROFILE_DIMMED_2 = HighlightProfile(
    pad_marker_rgba=(255, 220, 0, 160),
    pad_marker_radius=0.28,
    unconnected_rgba=(255, 140, 0, 180),
    dim_components=0.12,
    dim_wires=0.12,
)

# RouteTool — active wire drag (after first click).  Mirrors the
# net-members highlight profile (strong dim on everything except the
# routed net) but lowers ``highlight_alpha`` so the routed components
# themselves go semi-transparent — the wire being drawn under a DIP /
# SOIC body would otherwise be hidden under it.  Matches
# ``theme.ghost_comp_alpha``.
PROFILE_ROUTE_ACTIVE = HighlightProfile(
    pad_marker_rgba=(255, 255, 0, 255),
    pad_marker_radius=0.28,
    unconnected_rgba=(255, 255, 0, 255),
    dim_components=0.50,
    dim_wires=0.50,
    highlight_alpha=0.35,
)


# ---------------------------------------------------------------------------
# HighlightEntry
# ---------------------------------------------------------------------------

@dataclass
class HighlightEntry:
    """One entry on the highlight stack."""

    # UUIDs of highlighted components and/or wires.
    comp_ids: frozenset[uuid.UUID] = field(default_factory=frozenset)
    wire_ids: frozenset[uuid.UUID] = field(default_factory=frozenset)
    # Net ID used for opacity calculation (which comps/wires belong to the net).
    net_id: uuid.UUID | None = None
    # Visual parameters.  HighlightProfile here covers Dimmed #1/#2 etc.;
    # SelectTool wraps a selection_axes.RelationProfile by mapping its
    # dim_components/dim_wires onto this object via _profile_for_relation().
    profile: HighlightProfile = field(default_factory=HighlightProfile)
    # Human-readable label for debugging / future UI.
    label: str = ""
    # Transient entries are pushed by tools and popped on tool deactivation.
    # They do NOT persist across tool switches.
    transient: bool = False
    # Auto entries reflect derived state (e.g. SelectTool's current relation
    # class).  They are addressed by source_id and are the weakest tier.
    auto: bool = False
    # Stable identifier for the pusher, used by clear_auto/replace.  Only
    # meaningful for auto entries.
    source_id: str = ""
