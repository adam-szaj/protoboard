# SPDX-License-Identifier: MIT
"""``HoverEvent`` — the Controller's enriched reply to an ``empty_action``.

The verb layer (docs MVC step 1) translates a throttled pointer move into
``empty_action(point, mods)``; the Controller answers with *what is under the
cursor*, already hit-tested to domain identity.  Kept deliberately narrow —
``point`` plus the uuids hit — so it can ride the hover stream without building
the heavy :class:`~protoboard.controller.ports.interaction_context.InteractionContext`
(actions + menu) that ``notify_context`` carries for a right-click.
"""

from __future__ import annotations

import uuid
from dataclasses import dataclass, field
from typing import TYPE_CHECKING

from protoboard.controller.ports.inputs import Mods

if TYPE_CHECKING:
    from protoboard.geometry import Point

_EMPTY_MODS = Mods()


@dataclass(frozen=True, slots=True)
class HoverEvent:
    """What the Controller reports under the cursor for a hover.

    ``objects`` is empty over bare canvas.  Scene coordinates are board/raster
    coordinates (``geometry.py``), so ``point`` is already in model units — no
    conversion crosses the seam.  ``mods`` is the keyboard modifier state at
    the time of the hover (e.g. so hover-reveal can branch on Alt).
    """

    point: Point
    objects: tuple[uuid.UUID, ...] = field(default_factory=tuple)
    mods: Mods = _EMPTY_MODS
