# SPDX-License-Identifier: MIT
"""Tests for shared instrumentation logging helpers."""

from __future__ import annotations

import logging

from protoboard.instrumentation.logging import is_debug_enabled


def test_is_debug_enabled_follows_logger_level() -> None:
    logger_name = "protoboard.tests.logging_helper"
    logger = logging.getLogger(logger_name)
    previous = logger.level
    try:
        logger.setLevel(logging.DEBUG)
        assert is_debug_enabled(logger_name)
        logger.setLevel(logging.INFO)
        assert not is_debug_enabled(logger_name)
    finally:
        logger.setLevel(previous)
