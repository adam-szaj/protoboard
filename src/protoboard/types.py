# SPDX-License-Identifier: MIT
"""Shared primitive type aliases.

A *leaf* module — it imports only ``uuid`` and ``typing`` so any layer
(domain, services, gui) can import it without risking an import cycle, the
same way :mod:`protoboard.geometry` is a universal dependency.

This is deliberately limited to **primitive aliases** (uuid/tuple/Literal
wrappers) that were previously duplicated or spelled inline across modules.
Domain *classes* (``Point``, ``Wire``, ``Schematic`` …) keep their own homes;
they are not re-exported here.
"""

from __future__ import annotations

import uuid
from typing import Literal

# A schematic net's identity. Was defined identically in pad_strategy.py,
# pin_pad_classification.py and pad_state.py.
NetId = uuid.UUID

# Physical board layer a pin / wire / pad sits on. Was spelled inline as
# ``Literal["top", "bottom"]`` in ~10 modules plus a local ``Side`` alias.
Side = Literal["top", "bottom"]

# (component_id, pin_number) — a pin's positional key. Spelled inline as
# ``tuple[uuid.UUID, int]`` across many modules.
PinKey = tuple[uuid.UUID, int]

# (quantised_x, quantised_y, side) — a connectivity-graph node key. Was
# ``NodeKey`` in pad_geometry.py and ``_NodeKey`` in connectivity/graph.py.
NodeKey = tuple[float, float, str]

__all__ = ["NetId", "Side", "PinKey", "NodeKey"]
