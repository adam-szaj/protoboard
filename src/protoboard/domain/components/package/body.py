# SPDX-License-Identifier: MIT
"""Body presentation metadata for packages — edge style and disc profile.

These are the non-geometric attributes the legacy ``TwoPinBodySpec`` carried
that the renderer and the variant picker still need: how to round the body's
edges and (for discs) the rim profile. They live here, in the pure-domain
package layer, so the ``Package`` model is a complete replacement for
``TwoPinBodySpec``.

The legacy model named a ``Material`` enum mapped to a theme palette. We do not
reproduce that abstraction: a body carries its **colour directly** (a plain
``#rrggbb`` fill string on the shape), and the renderer derives a border by
darkening it. No material concept, no palette indirection, no domain→theme
coupling.

Pure domain: no Qt, no theme import.
"""

from __future__ import annotations

from enum import StrEnum


class LeadArrangement(StrEnum):
    """How the leads exit the body relative to the board.

    AXIAL — leads exit opposite faces along the body axis and turn to the
            board (resistor laid down, axial diode/inductor).
    RADIAL — leads exit the same face, side by side (electrolytic standing,
            LED, ceramic disc cap).
    """

    AXIAL = "axial"
    RADIAL = "radial"


class EdgeStyle(StrEnum):
    """Edge finish on the body — picks the corner-radius profile."""

    SHARP = "sharp"
    ROUNDED = "rounded"


class DiscProfile(StrEnum):
    """Sub-shape for a disc body.

    FLAT  — uniform thickness, sharp or rounded rim (ceramic disc cap).
    CANDY — domed top, "M&M" silhouette (some film caps, button cells).
    """

    FLAT = "flat"
    CANDY = "candy"


class MountStyle(StrEnum):
    """How a package mounts onto the board.

    THROUGH_HOLE — leads go through holes; pin geometry is fixed (or bends via
                   the lead solver / ``bend``).
    SURFACE      — an SMD chip resting on metallised pads, not in holes.  The
                   body is rigid, but the *mount* is flexible: the part may sit
                   on any pair of adjacent pads (orthogonal or diagonal), so the
                   second terminal picks one of a discrete set of neighbour pads
                   rather than a fixed hole.
    """

    THROUGH_HOLE = "through_hole"
    SURFACE = "surface"
