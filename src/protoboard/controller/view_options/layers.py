# SPDX-License-Identifier: MIT
"""Stable layer visibility and view-transform options."""

from __future__ import annotations

from dataclasses import dataclass
from enum import StrEnum
from typing import Literal

ViewTransform = Literal["normal", "mirror_h", "mirror_v", "mirror_hv"]


class LayerVisibility(StrEnum):
    FULL = "full"
    GHOST = "ghost"
    HIDDEN = "hidden"

    def cycle(self) -> LayerVisibility:
        order = [LayerVisibility.FULL, LayerVisibility.GHOST, LayerVisibility.HIDDEN]
        idx = order.index(self)
        return order[(idx + 1) % len(order)]

    def label(self) -> str:
        return f"[{self.value.capitalize()}]"


@dataclass
class LayerOverrides:
    """Manual per-layer visibility overrides."""

    board: LayerVisibility = LayerVisibility.FULL
    components_top: LayerVisibility = LayerVisibility.FULL
    components_bottom: LayerVisibility = LayerVisibility.FULL
    wires_top: LayerVisibility = LayerVisibility.FULL
    wires_bottom: LayerVisibility = LayerVisibility.FULL
    labels: LayerVisibility = LayerVisibility.FULL
    meta_pin_labels: LayerVisibility = LayerVisibility.FULL
    ratsnest: LayerVisibility = LayerVisibility.FULL
    axis_labels: LayerVisibility = LayerVisibility.FULL
    pin_pads: LayerVisibility = LayerVisibility.FULL
    pin_modifiers: LayerVisibility = LayerVisibility.FULL

    def reset(self) -> None:
        for name in self.__dataclass_fields__:
            setattr(self, name, LayerVisibility.FULL)

    def all_full(self) -> bool:
        return all(
            getattr(self, name) == LayerVisibility.FULL
            for name in self.__dataclass_fields__
        )
