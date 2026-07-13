# SPDX-License-Identifier: MIT
"""Status notifications published by the board-view surface (qt-ports §2.3)."""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import TYPE_CHECKING

if TYPE_CHECKING:
    from collections.abc import Mapping


@dataclass(frozen=True, slots=True)
class StatusEvent:
    """One notification published from behind the board-view seam.

    ``kind`` is the structural discriminator (``"route.locked_pin"``,
    ``"select.status"``, …); ``data`` carries structural fields.  During
    the ports migration tools keep today's formatted message under
    ``data["text"]`` for 1:1 status-bar parity; formatting can later move
    to the consumer.
    """

    kind: str
    data: Mapping[str, object] = field(default_factory=dict)

    @property
    def text(self) -> str:
        """Pre-formatted display text (``""`` clears the transient message)."""
        return str(self.data.get("text", ""))

    @property
    def timeout_ms(self) -> int:
        """Display timeout in ms (0 = sticky until replaced or cleared)."""
        value = self.data.get("timeout_ms", 0)
        return value if isinstance(value, int) else 0
