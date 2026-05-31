# SPDX-License-Identifier: MIT
"""Pad layer model.

A pad lives on one or more named layers (`top`, `bottom`, and later
kapton tape layers like `tape:top+1`). The MVP keeps the public surface
identical to the legacy two-boolean form so existing tests, YAML
fixtures, and call-sites are untouched:

    PadLayers(top=True, bottom=True, plated_through=True)

The same instance exposes a layer-id view via :meth:`layer_ids`,
:meth:`has_layer`, and :meth:`plated_neighbours` for code that wants
to iterate layers generically. Subsequent work (kapton tape) adds new
layer entries on top of this facade without touching the existing
``top``/``bottom``/``plated_through`` accessors.
"""

from __future__ import annotations

from dataclasses import dataclass

from protoboard.domain.board.layer import BOTTOM, TOP, LayerId


@dataclass(frozen=True, slots=True)
class PadLayers:
    """Which copper layers a pad occupies.

    Backward-compatible facade: the three boolean fields remain the
    canonical state; layer-id queries are derived. Validation matches
    the original module: at least one side present; ``plated_through``
    requires both sides.
    """

    top: bool = True
    bottom: bool = False
    plated_through: bool = False

    def __post_init__(self) -> None:
        if not self.top and not self.bottom:
            raise ValueError("PadLayers must have at least one of top/bottom")
        if self.plated_through and not (self.top and self.bottom):
            raise ValueError("plated_through requires both top and bottom")

    # ------------------------------------------------------------------
    # Layer-id view — used by Pad / PadStrategy / future kapton layers
    # ------------------------------------------------------------------

    def layer_ids(self) -> tuple[LayerId, ...]:
        """Layer ids with copper, in canonical order (top, bottom)."""
        out: list[LayerId] = []
        if self.top:
            out.append(TOP)
        if self.bottom:
            out.append(BOTTOM)
        return tuple(out)

    def has_layer(self, layer: LayerId) -> bool:
        """True if this pad has copper on *layer*."""
        if layer == TOP:
            return self.top
        if layer == BOTTOM:
            return self.bottom
        return False

    def plated_neighbours(self, layer: LayerId) -> frozenset[LayerId]:
        """Layers galvanically bonded to *layer* through this pad.

        Excludes *layer* itself. For a plated-through pad with both
        sides, ``plated_neighbours("top") == {"bottom"}``. For an
        isolated double-sided pad or a single-sided pad, returns the
        empty set.
        """
        if not self.plated_through:
            return frozenset()
        if layer == TOP and self.bottom:
            return frozenset({BOTTOM})
        if layer == BOTTOM and self.top:
            return frozenset({TOP})
        return frozenset()
