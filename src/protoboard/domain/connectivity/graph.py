# SPDX-License-Identifier: MIT
"""ConnectivityGraph — re-exported from the incremental implementation.

The incremental implementation in ``incremental.py`` replaces the old
networkx-based ConnectivityGraph.  This module re-exports it under the
original name for backward compatibility with all existing imports.
"""

from protoboard.domain.connectivity.incremental import (
    ConnectivityGraph as ConnectivityGraph,
)

# Re-export _NodeKey for any code that imported it directly from this module.
from protoboard.types import NodeKey as _NodeKey  # noqa: F401

__all__ = ["ConnectivityGraph"]
