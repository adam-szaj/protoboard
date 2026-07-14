# SPDX-License-Identifier: MIT
"""HoverEvent carries the modifier state (Alt) for hover-reveal branching."""

from __future__ import annotations

import dataclasses

from protoboard.controller.ports.hover import HoverEvent
from protoboard.controller.ports.inputs import Mods
from protoboard.geometry import Point


def test_hover_event_default_mods_empty() -> None:
    ev = HoverEvent(point=Point(0.0, 0.0))
    assert ev.mods == Mods()


def test_hover_event_replace_sets_mods() -> None:
    ev = HoverEvent(point=Point(0.0, 0.0))
    ev2 = dataclasses.replace(ev, mods=Mods(alt=True))
    assert ev2.mods.alt is True
