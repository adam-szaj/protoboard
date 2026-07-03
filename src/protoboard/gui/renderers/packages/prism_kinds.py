# SPDX-License-Identifier: MIT
"""Per-kind prism renderers — the declared body looks, as registry entries.

These replace the old ``_KIND_SILHOUETTE`` table in ``prism_silhouette.py``:
each procedural package ``kind`` (see ``domain/components/package/kind.py``)
registers a renderer whose ``_silhouette`` returns the *declared* look, so a
``dip_switch`` stays plain even though its two pin columns would column-guess
a DIP notch.  A kind with no registered renderer falls back to the base
:class:`~protoboard.gui.renderers.packages.prism.PrismRenderer` (the column
guess) — rendering never blocks on an unknown kind.
"""

from __future__ import annotations

from protoboard.domain.components.package.shapes import (
    Prism as _PkgPrism,
)
from protoboard.gui.renderers.packages.prism import PrismRenderer
from protoboard.gui.renderers.prism_silhouette import (
    PrismSilhouette,
    SilSilhouette,
    dip_silhouette,
)


class DipIcRenderer(PrismRenderer):
    """DIP IC (narrow / wide): the notched dual-row body with a pin-1 marker."""

    renders = ("dip_ic", "wdip_ic")

    def _silhouette(self, pkg: _PkgPrism) -> PrismSilhouette | None:
        # Parameterized by the two pin columns; a degenerate layout (not two
        # columns) yields None → the plain bbox rectangle, same as before.
        return dip_silhouette(pkg)


class SilHeaderRenderer(PrismRenderer):
    """SIL header: the rounded housing hugging the single pin row."""

    renders = "sil"

    def _silhouette(self, pkg: _PkgPrism) -> PrismSilhouette | None:
        return SilSilhouette()


class PlainBodyRenderer(PrismRenderer):
    """Kinds whose declared look is a plain rectangle — no notch, no housing.

    ``dip_switch`` is the case the column guess *cannot* distinguish from a
    real DIP-IC (same two columns, deliberately plain); ``pin_header`` / ``jst``
    connectors carry their look in the per-kind fill colour instead.
    """

    renders = ("dip_switch", "pin_header", "jst")

    def _silhouette(self, pkg: _PkgPrism) -> PrismSilhouette | None:
        return None
