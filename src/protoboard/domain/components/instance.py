# SPDX-License-Identifier: MIT
"""ComponentInstance — a placed-or-placeable copy of a definition.

The `id` is a UUID; `refdes` (e.g. "U1", "R5") is a user-facing label and
may be renamed without affecting connectivity (which references the UUID).
"""

from __future__ import annotations

import uuid
from dataclasses import dataclass


@dataclass(frozen=True, slots=True)
class ComponentInstance:
    id: uuid.UUID
    definition_ref: str
    refdes: str
    value: str

    def __post_init__(self) -> None:
        if not self.definition_ref:
            raise ValueError("definition_ref must not be empty")
        if not self.refdes:
            raise ValueError("refdes must not be empty")

    @staticmethod
    def new(definition_ref: str, refdes: str, value: str) -> ComponentInstance:
        """Create an instance with a freshly generated UUID."""
        return ComponentInstance(
            id=uuid.uuid4(),
            definition_ref=definition_ref,
            refdes=refdes,
            value=value,
        )
