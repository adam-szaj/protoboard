# SPDX-License-Identifier: MIT
"""Synthetic benchmark fixtures.

Deterministic generator + a small suite of pre-built projects used by the
bench tests in ``tests/benchmarks/``.  Replaces the broken ``m4`` fixture
that flunked the autoroute precondition (SHORT) and made
``test_routing_quality.py`` fail at collection.

Generator entrypoint:  ``generate_project(spec) -> ProjectBundle``.
Spec presets:          ``SMALL``, ``MEDIUM``, ``LARGE``, ``PLATED_THROUGH``,
                       ``STRIPBOARD_MIXED``.
"""

from __future__ import annotations

from tests.fixtures.synthetic_bench.generator import (
    LARGE,
    MEDIUM,
    PLATED_THROUGH,
    SMALL,
    STRIPBOARD_MIXED,
    ProjectBundle,
    ProjectSpec,
    generate_project,
)

__all__ = [
    "LARGE",
    "MEDIUM",
    "PLATED_THROUGH",
    "ProjectBundle",
    "ProjectSpec",
    "SMALL",
    "STRIPBOARD_MIXED",
    "generate_project",
]
