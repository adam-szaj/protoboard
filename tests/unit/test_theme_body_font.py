# SPDX-License-Identifier: MIT
"""Tests for Theme body_print_font_size default."""

from __future__ import annotations

from protoboard.controller.theme import DEFAULT_THEME, Theme


def test_body_print_font_size_default() -> None:
    assert Theme().body_print_font_size == 4.0
    assert DEFAULT_THEME.body_print_font_size == 4.0
