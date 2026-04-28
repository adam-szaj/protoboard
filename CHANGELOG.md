# Changelog

All notable changes to this project will be documented in this file.

The format is based on [Keep a Changelog](https://keepachangelog.com/en/1.1.0/),
and this project adheres to [Semantic Versioning](https://semver.org/spec/v2.0.0.html).

## [Unreleased]

### Phase 0 — Bootstrap
- [x] `pyproject.toml`, `uv`, `ruff`, `black`, `mypy --strict`, `pytest`, pre-commit hooks
- [x] Directory structure
- [x] CI: GitHub Actions (lint + mypy + pytest)
- [x] SPDX headers, `LICENSE` (MIT), `README.md` skeleton
- [x] `CHANGELOG.md` (Keep a Changelog)

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
