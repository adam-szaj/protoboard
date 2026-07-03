# SPDX-License-Identifier: MIT
"""Disc renderer — ceramic disc cap, button cell, disc varistor.

A disc is radial by construction, so nearly every draw goes through the base
class's radial branch (the edge-mounted lens side profile).  The non-radial
fallback is the bbox rectangle from the base.
"""

from __future__ import annotations

from protoboard.gui.renderers.packages.base import PackageRenderer


class DiscRenderer(PackageRenderer):
    """Disc package — the radial lens profile is drawn by the base class."""

    renders = "disc"
