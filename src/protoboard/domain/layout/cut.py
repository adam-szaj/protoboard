# SPDX-License-Identifier: MIT
"""StripCut — a cut in a stripboard strip."""

from __future__ import annotations

import uuid
from dataclasses import dataclass


@dataclass(frozen=True, slots=True)
class StripCut:
    id: uuid.UUID
    strip_atom_ref: str
    position: int
