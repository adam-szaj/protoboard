# SPDX-License-Identifier: MIT
"""Tests for Theme body_print_max_height default."""

from __future__ import annotations

from protoboard.controller.theme import DEFAULT_THEME, Theme


def test_body_print_max_height_default() -> None:
    assert Theme().body_print_max_height == 1.0
    assert DEFAULT_THEME.body_print_max_height == 1.0
