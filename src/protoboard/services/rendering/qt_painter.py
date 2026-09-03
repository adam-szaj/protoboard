# SPDX-License-Identifier: MIT
"""Small Qt painter lifecycle helpers shared by rendering adapters."""

from __future__ import annotations

from collections.abc import Iterator
from contextlib import contextmanager

from PySide6.QtGui import QPaintDevice, QPainter


@contextmanager
def active_painter(device: QPaintDevice) -> Iterator[QPainter]:
    """Yield an active painter and always finish it on scope exit."""
    painter = QPainter()
    painter.begin(device)
    try:
        yield painter
    finally:
        painter.end()


__all__ = ["active_painter"]
