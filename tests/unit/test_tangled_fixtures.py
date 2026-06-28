# SPDX-License-Identifier: MIT
"""Regression: hand-crafted tangled fixtures must autoroute without DRC shorts.

These fixtures (under ``tests/fixtures/tangled*/``) reproduce real layouts
where the simplify pass historically produced shorts — bare wires that
took a shortcut across an alien-net pad or through a shared pad on a
plated-through hole.  PadIndex-backed simplify should eliminate that
class of bug; this test loads each fixture, re-runs autoroute from a
clean slate with ``enable_simplify_pass=True``, and asserts that DRC
reports no SHORT issue.
"""
from __future__ import annotations

from dataclasses import replace
from pathlib import Path

import pytest

from protoboard.paths import BUILTIN_LIBRARY_DIR
from protoboard.services.drc.engine import DRCEngine
from protoboard.services.library.loader import load_library
from protoboard.services.persistence.project_yaml import load_project
from protoboard.services.routing.autorouter import autoroute


FIXTURES_DIR = Path(__file__).parent.parent / "fixtures"


def _run_autoroute_and_drc(fixture_dir: Path):
    from protoboard.services.library.package_family_loader import (
        load_full_package_catalogue,
    )
    from protoboard.services.persistence.project_yaml import (
        set_migration_families,
        set_migration_library,
    )

    lib = load_library([BUILTIN_LIBRARY_DIR])
    families = load_full_package_catalogue()
    # Bind loaded instances to the catalogue exactly as production does
    # (mirrors session.py): without it the returned instances are unbound
    # and resolving their geometry raises UnboundInstanceError.
    set_migration_library(lib)
    set_migration_families(families)
    try:
        board, layout, schematic, instances, routing, _, _ = load_project(fixture_dir)
    finally:
        set_migration_library(None)
        set_migration_families(None)
    components = {name: lib.get(name) for name in lib.names()}

    # Re-route from a clean slate so the simplify pass is the *only* code
    # path that could produce a short here.  Force wire_mode=bare_diagonal
    # because that's the only mode in which autoroute runs the simplify pass
    # (which is the code path under test).
    clean = replace(layout, wires=[])
    result = autoroute(
        board, clean, schematic, components, instances,
        cost_model=routing.cost_model(),
        wire_mode="bare_diagonal",
        max_rip_up_passes=routing.max_rip_up_passes,
        max_optimize_iterations=0,  # deterministic
        enable_simplify_pass=True,
        simplify_mode=routing.simplify_mode,
    )

    routed_layout = replace(clean, wires=list(result.wires))
    issues = DRCEngine().check(
        board=board,
        schematic=schematic,
        layout=routed_layout,
        components=components,
        instances=instances,
    )
    return result, issues


@pytest.mark.parametrize(
    "fixture_name",
    ["tangled", "tangled_02", "tangled_03", "tangled_04", "tangled_04b"],
)
def test_tangled_autoroute_simplify_no_shorts(fixture_name: str) -> None:
    """Autoroute + simplify on a tangled fixture must not produce SHORT issues."""
    fixture_dir = FIXTURES_DIR / fixture_name
    if not fixture_dir.exists():
        pytest.skip(f"fixture {fixture_name!r} not present")
    _result, issues = _run_autoroute_and_drc(fixture_dir)
    shorts = [i for i in issues if i.code == "SHORT"]
    assert not shorts, (
        f"Simplify produced {len(shorts)} short(s) on {fixture_name}: "
        + "; ".join(i.message for i in shorts[:5])
    )
