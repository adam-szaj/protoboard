# SPDX-License-Identifier: MIT
"""User-selectable wire-simplification policy."""

from __future__ import annotations

from enum import StrEnum


class SimplifyMode(StrEnum):
    """How aggressively to shorten a wire path.

    CHEBYSHEV replaces a subpath with the shortest grid-aligned path and may
    ignore the bare-diagonal parity rule. BARE_PARITY applies the same policy
    to insulated wires, but preserves the source pad's parity family for each
    new bare diagonal. The default therefore retains the anti-short invariant
    required by bare-diagonal routing.
    """

    CHEBYSHEV = "chebyshev"
    BARE_PARITY = "bare_parity"
