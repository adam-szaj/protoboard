# SPDX-License-Identifier: MIT
"""Small logging predicates shared by hot-path diagnostics."""

from __future__ import annotations

import logging


def is_debug_enabled(logger_name: str) -> bool:
    """Return whether *logger_name* currently emits debug records."""
    return logging.getLogger(logger_name).isEnabledFor(logging.DEBUG)
