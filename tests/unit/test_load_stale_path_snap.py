# SPDX-License-Identifier: MIT
"""Tests for ``load_project`` stale PinKnot-anchored wire endpoint snap
(commit 1574637).

A previous session's partial macro commit may have left a saved wire
with ``path[0]/path[-1]`` offset from the pin's resolved world
position — usually by the drag delta of an aborted move.  On reload
the misalignment seeds the next drag's macro into the same
``replace_wire_fragments: new path[0] disagrees with PinKnot
position`` failure.  Fix snaps each offending endpoint back to the
pin position.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from pathlib import Path

import pytest

from protoboard.domain.board.board import Board
from protoboard.domain.board.boxes import BoardGrid, PadAtom, SizePolicy
from protoboard.domain.board.pads import PadLayers
from protoboard.domain.components.definition import ComponentDefinition
from protoboard.domain.components.footprints import SIL
from protoboard.domain.components.instance import ComponentInstance
from protoboard.domain.layout.knot import PinKnot
from protoboard.domain.layout.layout import Layout
from protoboard.domain.layout.placement import ComponentPlacement
from protoboard.domain.layout.wire import KnotRef, Wire
from protoboard.domain.schematic.net import Net, PinRef
from protoboard.domain.schematic.schematic import Schematic
from protoboard.geometry import Point
from protoboard.services.persistence.project_yaml import (
    load_project,
    save_project,
    set_migration_library,
)


@dataclass
class _MiniLibrary:
    """Library stub with the same surface ``load_project`` needs."""

    _by_name: dict[str, ComponentDefinition] = field(default_factory=dict)

    def names(self):  # type: ignore[no-untyped-def]
        return self._by_name.keys()

    def get(self, name: str) -> ComponentDefinition:
        return self._by_name[name]

    def __contains__(self, name: object) -> bool:
        return isinstance(name, str) and name in self._by_name


def _board() -> Board:
    return Board(root=BoardGrid(
        width_policy=SizePolicy.HUG,
        height_policy=SizePolicy.HUG,
        cell=PadAtom(
            width_policy=SizePolicy.HUG,
            height_policy=SizePolicy.HUG,
            layers=PadLayers(top=True, bottom=True, plated_through=True),
        ),
        rows=20, cols=20, row_gap=0.0, col_gap=0.0,
    ))


def _make_resistor_library() -> _MiniLibrary:
    defn = ComponentDefinition(name="R", footprint=SIL(2), pin_names={1: "A", 2: "B"})
    lib = _MiniLibrary()
    lib._by_name["R"] = defn
    return lib


def _build_pin_to_pin_wire_state() -> tuple[Layout, dict, Schematic]:
    """One internal pin-to-pin wire whose path matches both PinKnots."""
    from protoboard.domain.layout.placement import pin_world_position
    defn = ComponentDefinition(name="R", footprint=SIL(2), pin_names={1: "A", 2: "B"})
    layout = Layout(placements={}, wires=[])
    inst_a = ComponentInstance.new(definition_ref="R", refdes="R1", value="10k")
    inst_b = ComponentInstance.new(definition_ref="R", refdes="R2", value="10k")
    instances = {inst_a.id: inst_a, inst_b.id: inst_b}
    layout.placements[inst_a.id] = ComponentPlacement(
        component_id=inst_a.id, position=Point(3.0, 5.0), rotation=0, side="top",
    )
    layout.placements[inst_b.id] = ComponentPlacement(
        component_id=inst_b.id, position=Point(3.0, 8.0), rotation=0, side="top",
    )
    pos_a = pin_world_position(defn.footprint, layout.placements[inst_a.id], 2)
    pos_b = pin_world_position(defn.footprint, layout.placements[inst_b.id], 1)
    pka = layout.get_or_create_pin_knot(PinRef(inst_a.id, 2))
    pkb = layout.get_or_create_pin_knot(PinRef(inst_b.id, 1))
    wire = Wire(
        id=__import__("uuid").uuid4(),
        path=[pos_a, pos_b], side="top",
        a=KnotRef(id=pka.id), b=KnotRef(id=pkb.id),
    )
    layout.add_wire(wire)
    schematic = Schematic(nets={})
    return layout, instances, schematic


def _shift_wire_path(layout: Layout, dx: float, dy: float) -> None:
    """Translate every wire's path by (dx, dy) — simulating a partial-
    commit drag where placements stayed put but the wire moved."""
    for w in layout.wires:
        w.path = [Point(p.x + dx, p.y + dy) for p in w.path]
        new_frags = []
        for frag in w.fragments:
            new_frags.append(
                type(frag)(
                    path=[Point(p.x + dx, p.y + dy) for p in frag.path],
                    side=frag.side,
                ),
            )
        w.fragments = new_frags


# ---------------------------------------------------------------------------
# Tests
# ---------------------------------------------------------------------------


class TestStalePathSnapOnLoad:
    def test_stale_path_snapped_to_pin_positions(self, tmp_path: Path) -> None:
        """A wire whose path is offset by +1 raster from both pins gets
        snapped back when loaded with a registered migration library.
        """
        layout, instances, schematic = _build_pin_to_pin_wire_state()
        # Save the clean state first.
        lib = _make_resistor_library()
        set_migration_library(lib)
        try:
            save_project(
                directory=tmp_path,
                board=_board(),
                layout=layout,
                schematic=schematic,
                instances=instances,
            )
            # Manually corrupt the saved YAML by shifting wire paths +1 in x.
            # Reload, mutate, re-save — simulates a partial-commit residue.
            _b, loaded_layout, _s, loaded_instances, *_ = load_project(tmp_path)
            # The load just normalised; nothing to snap.  Now perturb and
            # re-save.
            _shift_wire_path(loaded_layout, 1.0, 0.0)
            save_project(
                directory=tmp_path,
                board=_board(),
                layout=loaded_layout,
                schematic=schematic,
                instances=loaded_instances,
            )
            # Reload — snap should fire.
            _b2, snapped_layout, _s2, _ins2, *_ = load_project(tmp_path)
        finally:
            set_migration_library(None)

        # Find the wire and compare its endpoints with resolved pin positions.
        assert len(snapped_layout.wires) == 1
        w = list(snapped_layout.wires)[0]
        # Both endpoints must coincide with their PinKnot's pin world pos.
        from protoboard.domain.layout.placement import pin_world_position
        defn = lib.get("R")
        # Identify each endpoint's pin via the PinKnot ref.
        assert w.a is not None and w.b is not None
        ka = snapped_layout.knots[w.a.id]
        kb = snapped_layout.knots[w.b.id]
        assert isinstance(ka, PinKnot) and isinstance(kb, PinKnot)
        pos_a = pin_world_position(
            defn.footprint, snapped_layout.placements[ka.pin.component_id],
            ka.pin.pin_number,
        )
        pos_b = pin_world_position(
            defn.footprint, snapped_layout.placements[kb.pin.component_id],
            kb.pin.pin_number,
        )
        assert w.path[0] == pos_a
        assert w.path[-1] == pos_b

    def test_clean_path_not_perturbed(self, tmp_path: Path) -> None:
        """Idempotent: loading an already-clean project doesn't move
        anything."""
        layout, instances, schematic = _build_pin_to_pin_wire_state()
        lib = _make_resistor_library()
        set_migration_library(lib)
        try:
            save_project(
                directory=tmp_path,
                board=_board(),
                layout=layout,
                schematic=schematic,
                instances=instances,
            )
            _b, l1, _s, _ins, *_ = load_project(tmp_path)
            _b2, l2, _s2, _ins2, *_ = load_project(tmp_path)
        finally:
            set_migration_library(None)

        # Wire path unchanged between the two loads.
        w1 = list(l1.wires)[0]
        w2 = list(l2.wires)[0]
        assert w1.path == w2.path

    def test_no_library_skips_snap(self, tmp_path: Path) -> None:
        """Without a registered migration library the snap is a no-op.

        Defensive behaviour for unit tests that load minimal projects;
        the snap pass needs ``_library_for_migration`` to resolve pin
        world positions and bails out early when it's unavailable.
        """
        layout, instances, schematic = _build_pin_to_pin_wire_state()
        lib = _make_resistor_library()
        set_migration_library(lib)
        try:
            save_project(
                directory=tmp_path, board=_board(), layout=layout,
                schematic=schematic, instances=instances,
            )
            _b, l1, _s, i1, *_ = load_project(tmp_path)
            # Corrupt the path.
            _shift_wire_path(l1, 1.0, 0.0)
            save_project(
                directory=tmp_path, board=_board(), layout=l1,
                schematic=schematic, instances=i1,
            )
        finally:
            set_migration_library(None)

        # Library now unset — load should NOT snap.
        _b2, l2, _s2, _ins2, *_ = load_project(tmp_path)
        w = list(l2.wires)[0]
        # The path remains shifted by +1 from the pin position.  Without
        # library access, snap can't resolve the pin position and skips.
        from protoboard.domain.layout.placement import pin_world_position
        defn = lib.get("R")
        assert w.a is not None
        ka = l2.knots[w.a.id]
        assert isinstance(ka, PinKnot)
        pin_pos = pin_world_position(
            defn.footprint, l2.placements[ka.pin.component_id],
            ka.pin.pin_number,
        )
        # path[0] should still be ~+1 off in x (the corruption is preserved).
        assert w.path[0].x - pin_pos.x == pytest.approx(1.0)
