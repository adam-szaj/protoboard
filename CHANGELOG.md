# Changelog

All notable changes to this project will be documented in this file.

The format is based on [Keep a Changelog](https://keepachangelog.com/en/1.1.0/),
and this project adheres to [Semantic Versioning](https://semver.org/spec/v2.0.0.html).

## [Unreleased]

### Autoroute memory + off-board precondition (2026-05-27)

- A* (``astar.py``) previously carried the full ``list[PathStep]`` in
  every heap entry and grew it with ``path + [step]`` on each neighbour
  push.  On dense boards (80×60 plated-through perfboard) with no
  detour budget (``max_detour_factor=0.0``) the heap stored hundreds of
  thousands of entries × path length, pushing RSS past 10 GB on a
  single VCC route.  Replaced with a singly-linked-list path chain
  (one 2-tuple ``(parent, step)`` per push); shared prefixes coalesce
  in memory, ``_unfold_path_chain`` materialises the list only at goal
  / trace / rollback time.  Same routes produced; large drop in peak
  RSS on hard searches.
- New autoroute precondition: ``OFF_BOARD_PIN``.  Any pin in the
  schematic whose world position is not on the board grid raises
  ``AutoroutePreconditionError`` before A* runs.  An off-board pin has
  no valid node in the routing graph, so without an early bail-out
  A* explores the whole board attempting to reach an unreachable
  target — the same path-as-list explosion above.  Error message
  names the component / pin / net and tells the user to move the
  component back onto the board.

### Wire model refactor — Knot / Fragment domain (2026-05-27)

Two-commit refactor that introduces a richer wire model alongside the legacy
`WireSegment` list and fixes the segment-proliferation bug (d33ea56) by
preserving wire identity across all path-edit operations.

**Commit A — foundations (121d7b1):**
- New domain types in `src/protoboard/domain/layout/`:
  - `wire.py`: `Wire` (frozen, identity by id) + `Fragment` (same-side
    sub-path) + `KnotRef`.  Legacy `WireSegment` kept as a compat shim used
    by the v1 loader; removal scheduled for a future major.
  - `knot.py`: `Knot` protocol + `PinKnot` (anchored to `PinRef`, position
    resolved lazily) + `JointKnot` (free t-joint at a pad).
- `Layout` gained `knots`, `pin_knots`, `wires_v2` storage plus mutators
  that own the back-ref invariant: `get_or_create_pin_knot`,
  `create_joint_knot`, `add_wire_v2`, `remove_wire_v2`,
  `replace_wire_fragments` (preserves id — the headline fix),
  `try_collapse_joint` (same `insulated` AND (same `side` OR
  plated-through via)).
- Project YAML bumped to `format_version: 2`; legacy v1 files load
  unchanged.  New `services/persistence/wire_migration.py` rebuilds
  `wires_v2` + `knots` from legacy segments via iterative joint collapse.

**Commit B — consumers + identity preservation (6685177):**
- `ConnectivityGraph` gained `add_wire_v2(Wire) / remove_wire_v2(Wire)`
  that iterate fragments and aggregate merges into a single `WireRecord`
  per wire.id; plated-through transitions carry end-to-end electrical
  continuity through one record.
- `gui/renderers/wire_renderer.py`: `update_wire_groups_v2 /
  _build_wire_group_v2` render one `QGraphicsItemGroup` per Wire with
  per-fragment sub-items and via markers at side transitions; new theme
  keys `via_marker_color` / `via_marker_radius` (default fallbacks).
- `services/drc/engine.py`: `_check_off_pad_joint` raises `OFF_PAD_JOINT`
  errors for any `JointKnot` whose position is not on a board pad —
  enforces the §4 plan invariant.
- `services/routing/{autorouter,postprocess}.py`: `straighten_wire`,
  `corner_cut_wire`, `generic_simplify_wire`, `_nudge_wire`,
  `_try_mid_cell_reroute` now preserve `wire.id` across path edits.
- `services/routing/unweave.py`: `_from_logical_path` gained
  `seed_ids=[…]` so segment reconstruction reuses input ids in emission
  order; `_remerge` keeps the first segment's id when fusing;
  `_swap_fragment_between` preserves outer/middle ids; `_rebalance_sides`
  no longer mints fresh ids on attribute edits.
- New v2 commands in `domain/commands/component_cmds.py`:
  `AddWireV2Command`, `RemoveWireV2Command` (snapshots orphan joints for
  revert), `ReplaceWireFragmentsCommand` (id-preserving atomic
  fragment-list replacement).

Tests: 34 new unit tests in `test_wire_v2_model.py`,
`test_wire_v2_commands.py`, `test_unweave_id_preservation.py`.  Full
suite: 1256 passed, 61 skipped (was 1222 passed before refactor).
mypy --strict clean on all touched files (only pre-existing
pad_conflict.py + component_cmds.py legacy errors remain).

Plan: `~/.claude/plans/peppy-puzzling-hoare.md`.

### Phase 6 — Full DRC + Exports
- Added `FLOATING_PIN` (warning) and `BARE_WIRE_CROSS` (error) DRC checks
- SVG exporter (`services/export/svg.py`) — top-layer render: pads, wires, component outlines with refdes labels
- BOM CSV exporter (`services/export/bom.py`) — RefDes/Value/Footprint/Description, sorted
- Toolbar actions: Export SVG, Export BOM, Import KiCAD .net, Export KiCAD .net

### Phase 5 — KiCAD I/O
- `KiCadNetlistImporter` — parses `<export version="D">` XML netlist to `Schematic`
- Footprint heuristic: DIP-N, R_Axial (SIL-2), TO-92, PinHeader_MxN; unknown → `ImportWarning`
- `KiCadNetlistExporter` — serialises `Schematic` back to KiCAD XML; roundtrip preserves net names and pin counts
- 3 fixture netlists: `voltage_divider.net`, `minimal.net`, `unknown_footprint.net`

### Phase 4 — Routing Assistant
- `RoutingCostModel` — configurable per-step weights (free_pad, same_net_pad, occupied, direction_change)
- A* router on pad graph: 4-connectivity, top-K paths via edge-forbidding, Manhattan heuristic
- Route Wire tool shows top-3 hint paths as dashed green/yellow/orange overlay on first click
- `render_routing_hints()` overlay renderer

### GUI Bug Fixes (post Phase 3)
- **BUG 1**: `RouteTool._commit()` — preview lines were not cleared before emitting `layout_changed`; wire appeared to vanish after commit
- **BUG 2**: `CutTool` — position index was always wrong (`pos.x - pos.x == 0`); double `refresh()` call removed; strip geometry now correctly computed from center
- **BUG 3**: `SelectTool.deactivate()` — `_dragging` and `_drag_orig_pos` were not reset, causing stale drag state on reactivation
- **BUG 4/5**: `SelectTool.on_scene_cleared()` — now preserves `_selected_id` so in-progress drag can commit; highlight is redrawn after drag release
- **BUG 6**: `PlaceTool` undo — `session.instances` was mutated outside the command; moved to `PlaceComponentCommand` via `ComponentState` Protocol so undo removes the instance too
- `ComponentState` Protocol introduced in `component_cmds.py` — decouples commands from GUI layer
- 65 new `pytest-qt` GUI tests in `tests/gui/`; `tests/conftest.py` with shared `session` and `board_view` fixtures

### Phase 3 — GUI MVP
- [x] `pyproject.toml`, `uv`, `ruff`, `black`, `mypy --strict`, `pytest`, pre-commit hooks
- [x] Directory structure
- [x] CI: GitHub Actions (lint + mypy + pytest)
- [x] SPDX headers, `LICENSE` (MIT), `README.md` skeleton
- [x] `CHANGELOG.md` (Keep a Changelog)

### Phase 3 — GUI MVP
- [x] MainWindow with library dock, properties+DRC dock, toolbar, status bar
- [x] BoardView (QGraphicsView) — PCB-green background, gold pads, zoom/pan
- [x] Snap to nearest pad atom center
- [x] Tools: Select (S), Place (double-click library, R=rotate), Route Wire (W,
      multi-segment, Enter commit/Esc cancel), Cut Strip (C)
- [x] Properties panel — editable refdes/value/rotation per component
- [x] Undo/Redo Ctrl+Z/Y
- [x] Save/Load project folder (board.yaml + layout.yaml + schematic.yaml)
- [x] DRC panel updated on F5
- [x] Built-in component library (passive, opamps, transistors)

### Phase 2 — Components, Schematic, Layout, Connectivity, DRC, Commands
- [x] Schematic: PinRef, Net, Schematic with pins_for_component / net_for_pin
- [x] Physical layout: ComponentPlacement (4 rotations), WireSegment, StripCut, Layout
- [x] pin_world_position() with rotation support (0/90/180/270° CW)
- [x] ConnectivityGraph (networkx): pads → nodes, wires → edges, plated-through merge
- [x] DRCEngine: SHORT, MISSING_NET, OVERLAP checks
- [x] CommandStack (generic, TDD) + PlaceComponent / RemoveComponent commands
- [x] 309 tests, mypy --strict clean

### Phase 1 — Domain Model + Box Layout
- [x] Failing tests written first (TDD): units, geometry, pads, boxes,
      layout engine (intrinsic, layout, FILL, margins, errors, property-based),
      Board, YAML serializer, perfboard/stripboard 10×10 fixtures
- [x] `units.py` (RasterUnit, Millimeter, conversions), `geometry.py` (Point,
      Size, Insets, Rect)
- [x] `errors.py` (ProtoboardError, LayoutError, SerializationError)
- [x] Box model (`HBox`, `VBox`, `BoardGrid`, `PadAtom`, `StripAtom`, `GapAtom`)
      — frozen dataclasses with full validation
- [x] Two-pass layout engine: intrinsic-size + layout (positions = atom centers)
- [x] FILL distribution with single-FILL-per-axis rule and bounded-parent
      requirement
- [x] `Board` wrapper with cached layout, `atoms()` iterator, `bounds`, `size_mm`
- [x] YAML serializer/deserializer for boards (round-trip preserved)
- [x] 181 unit + property-based tests, all green
