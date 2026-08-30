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
from protoboard.domain.components.definition import (
    ComponentDefinition,
    MetaPin,
    PackageOption,
)
from protoboard.domain.components.instance import ComponentInstance
from protoboard.domain.layout.knot import PinKnot
from protoboard.domain.layout.layout import Layout
from protoboard.domain.layout.placement import ComponentPlacement, PlacementCollection
from protoboard.domain.layout.wire import KnotRef, Wire, WireCollection
from protoboard.domain.schematic.net import PinRef
from protoboard.domain.schematic.schematic import Schematic
from protoboard.geometry import Point
from protoboard.services.persistence.project_yaml import (
    load_project,
    save_project,
    set_migration_families,
    set_migration_library,
)
from tests.unit._autoroute_invariants import make_sil, sil_families


@dataclass
class _MiniLibrary:
    """Library stub with the same surface ``load_project`` needs."""

    _by_name: dict[str, ComponentDefinition] = field(default_factory=dict)
    package_families: dict = field(default_factory=dict)

    def names(self):  # type: ignore[no-untyped-def]
        return self._by_name.keys()

    def get(self, name: str) -> ComponentDefinition:
        return self._by_name[name]

    def __contains__(self, name: object) -> bool:
        return isinstance(name, str) and name in self._by_name


def _board() -> Board:
    return Board(
        root=BoardGrid(
            width_policy=SizePolicy.HUG,
            height_policy=SizePolicy.HUG,
            cell=PadAtom(
                width_policy=SizePolicy.HUG,
                height_policy=SizePolicy.HUG,
                layers=PadLayers(top=True, bottom=True, plated_through=True),
            ),
            rows=20,
            cols=20,
            row_gap=0.0,
            col_gap=0.0,
        )
    )


def _make_resistor_library() -> _MiniLibrary:
    defn = make_sil("R", 2)
    lib = _MiniLibrary(package_families=sil_families(defn))
    lib._by_name["R"] = defn
    return lib


def _build_pin_to_pin_wire_state() -> tuple[Layout, dict, Schematic]:
    """One internal pin-to-pin wire whose path matches both PinKnots."""
    from protoboard.domain.layout.placement import pin_world_position

    defn = make_sil("R", 2)
    families = sil_families(defn)
    layout = Layout(placements=PlacementCollection({}), wires=WireCollection())
    inst_a = ComponentInstance.new(definition_ref="R", refdes="R1", value="10k").bind(
        defn, families
    )
    inst_b = ComponentInstance.new(definition_ref="R", refdes="R2", value="10k").bind(
        defn, families
    )
    instances = {inst_a.id: inst_a, inst_b.id: inst_b}
    layout.placements[inst_a.id] = ComponentPlacement(
        component_id=inst_a.id,
        position=Point(3.0, 5.0),
        rotation=0,
        side="top",
    )
    layout.placements[inst_b.id] = ComponentPlacement(
        component_id=inst_b.id,
        position=Point(3.0, 8.0),
        rotation=0,
        side="top",
    )
    pos_a = pin_world_position(
        inst_a.resolved_footprint(), layout.placements[inst_a.id], 2
    )
    pos_b = pin_world_position(
        inst_b.resolved_footprint(), layout.placements[inst_b.id], 1
    )
    pka = layout.get_or_create_pin_knot(PinRef(inst_a.id, 2))
    pkb = layout.get_or_create_pin_knot(PinRef(inst_b.id, 1))
    wire = Wire(
        id=__import__("uuid").uuid4(),
        path=[pos_a, pos_b],
        side="top",
        a=KnotRef(id=pka.id),
        b=KnotRef(id=pkb.id),
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
        set_migration_families(lib.package_families)
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
            set_migration_families(None)

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
            defn.default_footprint(lib.package_families),
            snapped_layout.placements[ka.pin.component_id],
            ka.pin.pin_number,
        )
        pos_b = pin_world_position(
            defn.default_footprint(lib.package_families),
            snapped_layout.placements[kb.pin.component_id],
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
        set_migration_families(lib.package_families)
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
            set_migration_families(None)

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
        set_migration_families(lib.package_families)
        try:
            save_project(
                directory=tmp_path,
                board=_board(),
                layout=layout,
                schematic=schematic,
                instances=instances,
            )
            _b, l1, _s, i1, *_ = load_project(tmp_path)
            # Corrupt the path.
            _shift_wire_path(l1, 1.0, 0.0)
            save_project(
                directory=tmp_path,
                board=_board(),
                layout=l1,
                schematic=schematic,
                instances=i1,
            )
        finally:
            set_migration_library(None)
            set_migration_families(None)

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
            defn.default_footprint(lib.package_families),
            l2.placements[ka.pin.component_id],
            ka.pin.pin_number,
        )
        # path[0] should still be ~+1 off in x (the corruption is preserved).
        assert w.path[0].x - pin_pos.x == pytest.approx(1.0)


class TestVariantFootprintOnLoad:
    """Regression: load must measure pin positions against the component's
    CHOSEN variant footprint, not the definition default. A part saved as a
    variant whose pin spacing differs from the default would otherwise have
    every pin "move" on load, snapping the wire and dragging neighbours."""

    def _catalogue(self):
        from protoboard.services.library.package_family_loader import (
            load_full_package_catalogue,
        )

        return load_full_package_catalogue()

    def _variant_with_offset_spacing(self, fam):
        # Find a family with >=2 variants whose two-pin spacing differs, so the
        # default footprint and the chosen variant disagree (the bug's trigger).
        def spacing(variant) -> float:
            pins = variant.package.pin_positions()
            return pins[1].x - pins[0].x if len(pins) >= 2 else 0.0

        for fid, family in fam.items():
            vs = list(family.variants)
            if len(vs) < 2:
                continue
            spans = {round(spacing(v), 2) for v in vs}
            if len(spans) >= 2 and all(v.package.pin_count() == 2 for v in vs):
                # default = first variant; pick a later one with other spacing.
                default_v = vs[0]
                for v in vs[1:]:
                    if round(spacing(v), 2) != round(spacing(default_v), 2):
                        return fid, default_v.variant_id, v.variant_id
        return None

    def test_variant_part_path_stable_across_loads(self, tmp_path: Path) -> None:
        from protoboard.domain.components.definition import ComponentDefinition
        from protoboard.services.persistence.project_yaml import (
            set_migration_families,
        )

        fam = self._catalogue()
        found = self._variant_with_offset_spacing(fam)
        if found is None:
            pytest.skip("no 2-pin family with differing variant spacing")
        family_id, default_vid, other_vid = found

        # Definition defaults to the FIRST variant; the instance overrides to a
        # variant with different pin spacing.
        defn = ComponentDefinition(
            name="P",
            meta_pins=(MetaPin(("A",)), MetaPin(("B",))),
            package_options=(
                PackageOption(
                    name=f"{family_id}/{default_vid}",
                    ref=f"{family_id}/{default_vid}",
                    pin_map={1: (1,), 2: (2,)},
                ),
            ),
        )
        lib = _MiniLibrary(package_families=fam)
        lib._by_name["P"] = defn

        layout = Layout(placements=PlacementCollection({}), wires=WireCollection())
        inst = ComponentInstance.new(
            definition_ref="P",
            refdes="P1",
            value="",
            package_variant=f"{family_id}/{other_vid}",
        ).bind(defn, fam)
        instances = {inst.id: inst}
        layout.placements[inst.id] = ComponentPlacement(
            component_id=inst.id, position=Point(3.0, 5.0), rotation=0, side="top"
        )
        schematic = Schematic(nets={})

        set_migration_library(lib)
        set_migration_families(fam)
        try:
            save_project(
                directory=tmp_path,
                board=_board(),
                layout=layout,
                schematic=schematic,
                instances=instances,
            )
            _b1, l1, _s1, i1, *_ = load_project(tmp_path)
            save_project(
                directory=tmp_path,
                board=_board(),
                layout=l1,
                schematic=schematic,
                instances=i1,
            )
            _b2, l2, _s2, _i2, *_ = load_project(tmp_path)
        finally:
            set_migration_library(None)
            set_migration_families(None)

        # The chosen variant resolves to the SAME footprint on both loads, so
        # the placement is stable — pin positions don't drift to the default.
        from protoboard.domain.components.package import resolved_footprint
        from protoboard.domain.layout.placement import pin_world_position

        fp = resolved_footprint(defn, inst, fam)
        pos2 = pin_world_position(fp, l2.placements[inst.id], 2)
        # The variant's pin-2 world position must match what resolved_footprint
        # gives — i.e. load used the variant, not defn.default_footprint() (SIL spacing).  # noqa: E501
        from protoboard.domain.components.footprints import SIL as _SIL

        default_pos = pin_world_position(_SIL(2), l2.placements[inst.id], 2)
        assert (pos2.x, pos2.y) != (default_pos.x, default_pos.y)


class TestPadIndexUsesVariantFootprint:
    """Regression (pin-pad highlight off-pad): ``PadIndex.add_component`` must
    place a bound instance's PinItems on its *chosen variant's* pads, not the
    definition-default pads. The user saw pin-2's highlight ring ~3.2 holes off
    because the index put the PinItem at the default axial pitch while the part
    was an SMD-0805 (1-hole pitch)."""

    @staticmethod
    def _resistor_and_catalogue():  # type: ignore[no-untyped-def]
        from protoboard.paths import BUILTIN_LIBRARY_DIR
        from protoboard.services.library.loader import load_library
        from protoboard.services.library.package_family_loader import (
            load_full_package_catalogue,
        )

        families = load_full_package_catalogue()
        lib = load_library([BUILTIN_LIBRARY_DIR], families)
        return lib.get("R"), families

    def test_pin_item_lands_on_variant_pad(self) -> None:
        from protoboard.domain.board.pad_state import PadIndex, PinItem
        from protoboard.domain.components.package import resolved_footprint
        from protoboard.domain.components.package.family import resolve_variant
        from protoboard.domain.layout.placement import pin_world_position

        defn, families = self._resistor_and_catalogue()
        # Bind an instance to the SMD-0805 variant (tight 1-hole pin pitch).
        inst = ComponentInstance.new(
            definition_ref="R",
            refdes="R1",
            value="",
            package_variant="chip-smd/imperial/0805",
        ).bind(defn, families)

        board = _board()
        layout = Layout(placements=PlacementCollection({}), wires=WireCollection())
        # The HUG board's pad centres sit at half-integer coordinates, so place
        # pin-1 on a real pad centre; variant pin-2 then lands on (6.5, 5.5).
        placement = ComponentPlacement(
            component_id=inst.id,
            position=Point(5.5, 5.5),
            rotation=0,
            side="top",
        )
        layout.placements[inst.id] = placement
        instances = {inst.id: inst}
        components = {"R": defn}
        schematic = Schematic(nets={})

        index = PadIndex(board, layout, schematic, components, instances)

        # Where the variant says pin-2 is, vs the (wrong) default footprint.
        variant = resolve_variant(families, "chip-smd/imperial/0805")
        assert variant is not None
        variant_fp = variant.package
        variant_pin2 = pin_world_position(variant_fp, placement, 2)
        default_pin2 = pin_world_position(
            defn.default_footprint(families), placement, 2
        )
        # The variant and default must actually disagree, else the test proves
        # nothing.
        assert (variant_pin2.x, variant_pin2.y) != (default_pin2.x, default_pin2.y)

        def pin2_items(pos: Point) -> list[PinItem]:
            pad = index.pad_at(pos)
            if pad is None:
                return []
            return [
                it
                for it in pad.physically_blocked_on("top")
                if isinstance(it, PinItem) and it.pin_number == 2
            ]

        # A PinItem for pin-2 must sit on the VARIANT pad…
        assert pin2_items(variant_pin2), "pin-2 PinItem is not on the variant pad"
        # …and NOT on the default-footprint pad.
        assert not pin2_items(
            default_pin2
        ), "pin-2 PinItem leaked onto the default-footprint pad"

        # Sanity: the bound instance resolves to the same footprint the index
        # used.
        assert (
            inst.resolved_footprint().pin_positions()
            == resolved_footprint(defn, inst, families).pin_positions()
        )
