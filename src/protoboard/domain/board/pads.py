# SPDX-License-Identifier: MIT
"""Pad layer model.

A pad can exist on the top layer, the bottom layer, or both. When both
sides exist and `plated_through=True`, they are electrically the same
node; otherwise they are two independent nodes.
"""

from __future__ import annotations

from dataclasses import dataclass


@dataclass(frozen=True, slots=True)
class PadLayers:
    """Which copper layers a pad occupies."""

    top: bool = True
    bottom: bool = False
    plated_through: bool = False

    def __post_init__(self) -> None:
        if not self.top and not self.bottom:
            raise ValueError("PadLayers must have at least one of top/bottom")
        if self.plated_through and not (self.top and self.bottom):
            raise ValueError("plated_through requires both top and bottom")
