# SPDX-License-Identifier: MIT
"""``FloatTransform`` — GUI alias for the armed rotate/move/flip core.

The state machine itself is Qt-free and lives in
:mod:`protoboard.services.editing.armed_transform` as
:class:`~protoboard.services.editing.armed_transform.ArmedTransform` (moved out
of ``gui`` so it is testable headless and reachable from the controller).  Every
Qt seam it needs — snapping, command execution, key-chord matching, preview
drawing, status — arrives as an injected callback, so ``SelectTool`` still
constructs it directly.

This module re-exports the class under its historical GUI name so existing
callers and ``isinstance`` checks keep working unchanged.
"""

from __future__ import annotations

from protoboard.services.editing.armed_transform import (
    ArmedTransform as FloatTransform,
)
from protoboard.services.editing.armed_transform import _Original

__all__ = ["FloatTransform", "_Original"]
