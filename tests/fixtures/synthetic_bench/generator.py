# SPDX-License-Identifier: MIT
"""Deterministic synthetic project generator for benchmarks.

Produces a self-consistent ``(Board, Layout, Schematic, instances, routing)``
bundle that is routable-by-construction:

* Components are placed in a regular slot grid with horizontal+vertical
  corridors wide enough for the autorouter to thread wires between
  packages.
* The netlist mixes ~70% local 2-pin nets between geometric neighbours,
  ~20% local 3-5-pin nets, and ~10% pins on two global rails
  (``GND`` / ``VCC``).
* Every project is verified DRC-clean (no SHORT/OVERLAP) before being
  returned; generation is fully deterministic (driven by a single seed).

The generator is the source-of-truth for the YAML fixtures under
``tests/fixtures/synthetic_bench/<name>/``.  ``scripts/regenerate.py``
re-emits them on disk via ``save_project``.
"""

from __future__ import annotations

import random
import uuid
from dataclasses import dataclass, field
from typing import Literal

from protoboard.domain.board.board import Board
from protoboard.domain.board.boxes import (
    BoardGrid,
    HBox,
    PadAtom,
    SizePolicy,
    StripAtom,
)
from protoboard.domain.board.pads import PadLayers
from protoboard.domain.components.definition import ComponentDefinition
from protoboard.domain.components.instance import ComponentInstance
from protoboard.domain.layout.layout import Layout
from protoboard.domain.layout.placement import (
    ComponentPlacement,
    PlacementCollection,
    pin_world_position,
)
from protoboard.domain.routing_settings import RoutingSettings
from protoboard.domain.schematic.net import Net, PinRef
from protoboard.domain.schematic.schematic import Schematic
from protoboard.geometry import Point, Vector

# ---------------------------------------------------------------------------
# Spec
# ---------------------------------------------------------------------------


BoardKind = Literal["perfboard", "plated_through", "stripboard_mixed"]


@dataclass(frozen=True)
class ProjectSpec:
    """Knobs that fully determine the generated project."""

    name: str
    rows: int  # board rows
    cols: int  # board cols
    n_components: int  # total component count
    kind: BoardKind = "perfboard"
    # Mix knobs — must sum to <= 1.0; remainder goes to local 2-pin nets.
    global_rail_pin_frac: float = 0.10  # per rail (GND, VCC)
    multipin_local_frac: float = 0.20  # fraction of remaining pins in 3-5-pin nets
    seed: int = 0x5EED


SMALL = ProjectSpec(name="small", rows=15, cols=20, n_components=10)
MEDIUM = ProjectSpec(name="medium", rows=30, cols=40, n_components=50)
LARGE = ProjectSpec(name="large", rows=60, cols=80, n_components=150)
PLATED_THROUGH = ProjectSpec(
    name="plated_through",
    rows=30,
    cols=40,
    n_components=50,
    kind="plated_through",
)
STRIPBOARD_MIXED = ProjectSpec(
    name="stripboard_mixed",
    rows=30,
    cols=40,
    n_components=40,
    kind="stripboard_mixed",
)


@dataclass
class ProjectBundle:
    """A complete in-memory project ready to be saved or routed."""

    board: Board
    layout: Layout
    schematic: Schematic
    instances: dict[uuid.UUID, ComponentInstance]
    routing: RoutingSettings
    components: dict[str, ComponentDefinition] = field(default_factory=dict)


# ---------------------------------------------------------------------------
# Board construction
# ---------------------------------------------------------------------------


_HUG = SizePolicy.HUG


def _perf_cell(plated_through: bool = False) -> PadAtom:
    layers = PadLayers(
        top=True,
        bottom=plated_through,
        plated_through=plated_through,
    )
    return PadAtom(width_policy=_HUG, height_policy=_HUG, layers=layers)


def _strip_cell(length: int) -> StripAtom:
    return StripAtom(
        width_policy=_HUG,
        height_policy=_HUG,
        length=length,
        orientation="horizontal",
        layers=PadLayers(top=True),
    )


def _grid(cell, rows: int, cols: int) -> BoardGrid:
    return BoardGrid(
        width_policy=_HUG,
        height_policy=_HUG,
        cell=cell,
        rows=rows,
        cols=cols,
        row_gap=0.0,
        col_gap=0.0,
    )


def _build_board(spec: ProjectSpec) -> Board:
    if spec.kind == "plated_through":
        return Board(root=_grid(_perf_cell(plated_through=True), spec.rows, spec.cols))

    if spec.kind == "stripboard_mixed":
        # Left half: stripboard (one horizontal strip per row).
        # Right half: plain perfboard.
        strip_cols = spec.cols // 2
        perf_cols = spec.cols - strip_cols
        strip_grid = _grid(_strip_cell(strip_cols), spec.rows, 1)
        perf_grid = _grid(_perf_cell(), spec.rows, perf_cols)
        root = HBox(
            width_policy=_HUG,
            height_policy=_HUG,
            children=[strip_grid, perf_grid],
            gap=0.0,
        )
        return Board(root=root)

    # default perfboard
    return Board(root=_grid(_perf_cell(), spec.rows, spec.cols))


# ---------------------------------------------------------------------------
# Component definitions / library
# ---------------------------------------------------------------------------


def _resolve_components() -> dict[str, ComponentDefinition]:
    """Load the built-in library so the generator can look up footprints."""
    from protoboard.paths import BUILTIN_LIBRARY_DIR
    from protoboard.services.library.loader import load_library

    lib = load_library([BUILTIN_LIBRARY_DIR])
    return {name: lib.get(name) for name in lib.names()}


# Two-pin resistor (SIL, pitch=1.0).  Default leg reach for pin 2 is 4
# rasters (see passive.yaml bendable_pins.default.y=4), but for the
# generator we override pin 2 offset to (0, 1) so the body sits compactly
# in a 1×2 slot — keeps the grid math straightforward.
_RESISTOR_DEF = "R"
# Four-pin SIL header — used for the "wider package" component archetype.
_HEADER_DEF = "PinHeader_1x04"


# ---------------------------------------------------------------------------
# Placement
# ---------------------------------------------------------------------------


@dataclass(frozen=True)
class _Slot:
    """One placement cell in the slot grid."""

    archetype: str  # "R" or "H4"
    origin_col: int  # left edge of slot, in board cells
    origin_row: int  # top edge of slot, in board cells


def _slot_geometry() -> dict[str, tuple[int, int]]:
    """Width × height (board cells) reserved by each archetype's slot."""
    return {
        "R": (1, 2),  # 2-pin SIL, vertical
        "H4": (1, 4),  # 4-pin SIL, vertical
    }


def _pick_archetypes(rng: random.Random, n: int) -> list[str]:
    """~70% resistors, ~30% 4-pin headers."""
    archetypes: list[str] = []
    for _ in range(n):
        archetypes.append("H4" if rng.random() < 0.30 else "R")
    return archetypes


def _build_slots(spec: ProjectSpec, archetypes: list[str]) -> list[_Slot]:
    """Pack components into a regular slot grid with corridors.

    Corridor sizing: 1 cell horizontal gap between slot columns,
    2 cells vertical gap between slot rows.  That leaves room for
    the autorouter without making boards needlessly huge.
    """
    geom = _slot_geometry()
    max_slot_h = max(h for _, h in geom.values())
    slot_w = max(w for w, _ in geom.values())  # 1
    h_gap, v_gap = 1, 2
    margin_x, margin_y = 1, 1

    # Determine usable cols/rows in slot units.  For stripboard_mixed we
    # restrict placements to the right (perfboard) half so wires can
    # exploit the strips on the left for buses.
    avail_cols = spec.cols - 2 * margin_x
    avail_rows = spec.rows - 2 * margin_y
    if spec.kind == "stripboard_mixed":
        perf_offset_cols = spec.cols // 2
        avail_cols -= perf_offset_cols
        margin_x_eff = margin_x + perf_offset_cols
    else:
        margin_x_eff = margin_x

    slots_per_row = max(1, (avail_cols + h_gap) // (slot_w + h_gap))
    slot_rows = max(1, (avail_rows + v_gap) // (max_slot_h + v_gap))
    capacity = slots_per_row * slot_rows
    if len(archetypes) > capacity:
        raise ValueError(
            f"Board {spec.rows}x{spec.cols} ({spec.kind}) fits only "
            f"{capacity} slots ({slots_per_row}x{slot_rows}); requested "
            f"{len(archetypes)} components"
        )

    slots: list[_Slot] = []
    for i, archetype in enumerate(archetypes):
        sr, sc = divmod(i, slots_per_row)
        col = margin_x_eff + sc * (slot_w + h_gap)
        row = margin_y + sr * (max_slot_h + v_gap)
        slots.append(_Slot(archetype=archetype, origin_col=col, origin_row=row))
    return slots


def _placement_for_slot(
    slot: _Slot,
    component_id: uuid.UUID,
) -> tuple[ComponentPlacement, dict[int, Vector]]:
    """Build placement + override pin offsets so the body fits the slot.

    Pad centres sit at integer + 0.5 (the standard cell-centre convention).
    We anchor pin 1 at the slot's top-left cell centre, and override
    bendable pin offsets so subsequent pins step one cell down.
    """
    # Cell centre = (col + 0.5, row + 0.5).
    pos = Point(slot.origin_col + 0.5, slot.origin_row + 0.5)
    # No pin offsets: footprint defaults already place pins on the slot's
    # consecutive cell centres (SIL pitch=1.0 → pin n at (0, n-1)).
    pin_offsets: dict[int, Vector] = {}
    placement = ComponentPlacement(
        component_id=component_id,
        position=pos,
        rotation=0,
        side="top",
        pin_offsets=pin_offsets,
    )
    return placement, pin_offsets


# ---------------------------------------------------------------------------
# Netlist
# ---------------------------------------------------------------------------


def _build_nets(
    rng: random.Random,
    spec: ProjectSpec,
    instances: dict[uuid.UUID, ComponentInstance],
    components: dict[str, ComponentDefinition],
    placements: dict[uuid.UUID, ComponentPlacement],
) -> Schematic:
    """Generate a mixed-topology netlist over all available pins.

    Algorithm:
      1. Materialise every (component, pin) with its world position.
      2. Reserve a fraction for the global GND / VCC rails (random sample).
      3. Iterate over the remaining pins in a deterministic order; for
         each unassigned pin, find its nearest unassigned neighbour and
         either form a 2-pin net (default) or extend the net to 3-5 pins
         with the closest still-free neighbours (with probability
         ``multipin_local_frac``).
    """
    schematic = Schematic(nets={})

    # Build (pin_ref, world_pos) for every pin.
    pin_pool: list[tuple[PinRef, Point]] = []
    for cid, placement in placements.items():
        inst = instances[cid]
        defn = components[inst.definition_ref]
        for pin_no in range(1, defn.default_footprint().pin_count() + 1):
            ref = PinRef(component_id=cid, pin_number=pin_no)
            pin_pool.append(
                (ref, pin_world_position(defn.default_footprint(), placement, pin_no))
            )

    rng.shuffle(pin_pool)
    total = len(pin_pool)
    n_rail_each = max(2, int(total * spec.global_rail_pin_frac))

    # GND rail
    gnd_pins = {pin_pool[i][0] for i in range(n_rail_each)}
    schematic.nets[uuid.uuid4()] = _make_net("GND", gnd_pins, global_net=True)

    # VCC rail
    vcc_pins = {pin_pool[i][0] for i in range(n_rail_each, 2 * n_rail_each)}
    schematic.nets[uuid.uuid4()] = _make_net("VCC", vcc_pins, global_net=True)

    # Remaining pins → local nets.  Sort by (y, x) for stable nearest-neighbour
    # pairing — gives nets that connect physically close pins (low wire/MST ratio).
    remaining = [(ref, pos) for ref, pos in pin_pool[2 * n_rail_each :]]
    remaining.sort(
        key=lambda rp: (rp[1].y, rp[1].x, str(rp[0].component_id), rp[0].pin_number)
    )

    assigned: set[PinRef] = set()
    local_nets: list[set[PinRef]] = []

    for i, (ref, pos) in enumerate(remaining):
        if ref in assigned:
            continue
        # Find nearest unassigned pin (excluding pins on the same component
        # — connecting a component to itself would form a short via the body).
        nearest: tuple[float, int] | None = None
        for j, (other_ref, other_pos) in enumerate(remaining):
            if j == i or other_ref in assigned:
                continue
            if other_ref.component_id == ref.component_id:
                continue
            d = abs(pos.x - other_pos.x) + abs(pos.y - other_pos.y)
            if nearest is None or d < nearest[0]:
                nearest = (d, j)
        if nearest is None:
            break  # last lonely pin — drop it (no net)

        members = {ref, remaining[nearest[1]][0]}
        # Possibly extend to 3-5-pin net.
        if rng.random() < spec.multipin_local_frac:
            target_size = rng.randint(3, 5)
            # Greedy: add nearest unassigned pins until reaching target_size.
            while len(members) < target_size:
                centroid_x = sum(
                    remaining[k][1].x
                    for k in range(len(remaining))
                    if remaining[k][0] in members
                ) / len(members)
                centroid_y = sum(
                    remaining[k][1].y
                    for k in range(len(remaining))
                    if remaining[k][0] in members
                ) / len(members)
                next_idx: tuple[float, int] | None = None
                for k, (cand_ref, cand_pos) in enumerate(remaining):
                    if cand_ref in members or cand_ref in assigned:
                        continue
                    if any(cand_ref.component_id == m.component_id for m in members):
                        continue
                    d = abs(cand_pos.x - centroid_x) + abs(cand_pos.y - centroid_y)
                    if next_idx is None or d < next_idx[0]:
                        next_idx = (d, k)
                if next_idx is None:
                    break
                members.add(remaining[next_idx[1]][0])

        assigned |= members
        local_nets.append(members)

    for idx, members in enumerate(local_nets):
        schematic.nets[uuid.uuid4()] = _make_net(f"N{idx:03d}", members)

    return schematic


def _make_net(name: str, pins: set[PinRef], global_net: bool = False) -> Net:
    return Net(
        id=uuid.uuid4(),
        name=name,
        pins=set(pins),
        global_net=global_net,
    )


# ---------------------------------------------------------------------------
# Top-level: generate_project
# ---------------------------------------------------------------------------


def generate_project(spec: ProjectSpec) -> ProjectBundle:
    """Produce a self-consistent project bundle from *spec*.

    Pure function — no I/O.  Raises ``ValueError`` if the spec is
    infeasible (board too small for the requested component count).
    """
    rng = random.Random(spec.seed)
    components = _resolve_components()
    if _RESISTOR_DEF not in components or _HEADER_DEF not in components:
        raise RuntimeError(
            "Built-in library missing required defs "
            f"({_RESISTOR_DEF}, {_HEADER_DEF})"
        )

    board = _build_board(spec)
    archetypes = _pick_archetypes(rng, spec.n_components)
    slots = _build_slots(spec, archetypes)

    instances: dict[uuid.UUID, ComponentInstance] = {}
    placements: dict[uuid.UUID, ComponentPlacement] = {}
    r_count = h_count = 0

    for slot in slots:
        if slot.archetype == "R":
            r_count += 1
            inst = ComponentInstance.new(
                definition_ref=_RESISTOR_DEF,
                refdes=f"R{r_count}",
                value="10k",
            )
        else:
            h_count += 1
            inst = ComponentInstance.new(
                definition_ref=_HEADER_DEF,
                refdes=f"J{h_count}",
                value="HDR4",
            )
        instances[inst.id] = inst
        placement, _ = _placement_for_slot(slot, inst.id)
        placements[inst.id] = placement

    layout = Layout(placements=PlacementCollection(placements))
    schematic = _build_nets(rng, spec, instances, components, placements)

    routing = RoutingSettings()

    return ProjectBundle(
        board=board,
        layout=layout,
        schematic=schematic,
        instances=instances,
        routing=routing,
        components=components,
    )
