# Changelog

All notable changes to this project will be documented in this file.

The format is based on [Keep a Changelog](https://keepachangelog.com/en/1.1.0/),
and this project adheres to [Semantic Versioning](https://semver.org/spec/v2.0.0.html).

## [Unreleased]

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
