# SPDX-License-Identifier: MIT
"""Layer identifiers for pad model.

The board's physical surface is modelled as a set of named layers. MVP
ships with two: ``"top"`` and ``"bottom"``. The follow-up that adds
kapton tape (per `docs/pad-pin-wire-junction-semantics.md`) extends
this with names like ``"tape:top+1"`` — an arbitrary number of tape
layers stacked above/below the board.

A `LayerId` is just a string. The grammar:

* ``"top"`` / ``"bottom"`` — physical copper layers.
* ``"tape:<base>+<offset>"`` — kapton tape; ``<base>`` is ``top`` or
  ``bottom``, ``<offset>`` is a positive integer (``1`` = first tape
  layer above ``top``). Reserved for the follow-up.

Algorithms must NOT switch on the string content; ask the pad/strategy
whether routing on this layer is allowed.
"""

from __future__ import annotations

from typing import Final

LayerId = str

TOP: Final[LayerId] = "top"
BOTTOM: Final[LayerId] = "bottom"

# Stable iteration order for the two physical layers. The pad model uses
# this when no layer is specified to mean "all known sides" — callers
# adding kapton layers must iterate the pad's own layer list instead.
PHYSICAL_LAYERS: Final[tuple[LayerId, LayerId]] = (TOP, BOTTOM)


__all__ = ["LayerId", "TOP", "BOTTOM", "PHYSICAL_LAYERS"]
