# Implementation phases (historical)

The original phase plan tracked the path from zero to a usable application.
All planned phases have shipped; the file is kept as a high-level summary.
For day-to-day history use `CHANGELOG.md` (chronological) and `git log`.

| Phase | Scope | Status |
|---|---|---|
| 0 — Bootstrap | `pyproject.toml`, `uv`, `ruff`, `black`, `mypy --strict`, `pytest`, pre-commit. CI on GitHub Actions. SPDX headers, LICENSE (MIT), README, CHANGELOG. | ✅ |
| 1 — Domain model + box layout | `units.py`, `geometry.py`. Box model and layout engine (HUG / FILL / FIXED, fractional / negative gaps). Board wrapper + YAML serialiser. | ✅ |
| 2 — Components + schematic + layout + connectivity | `FootprintTemplate`, `ComponentDefinition`, YAML library. Schematic (`Net`, `PinRef`). Layout (`ComponentPlacement`, `Wire`, knot model). `ConnectivityGraph`. DRC: SHORT, MISSING_NET, OVERLAP. CommandStack + Place / Move / Remove. | ✅ |
| 3 — GUI MVP | MainWindow, BoardView, snap, tools (Select / Place / Route / Cut). Pad-conflict detection, body-overlap blocking, pin-on-wire detection. Auto-create nets when drawing wires. Multi-selection. Wire editing. Groups, Disconnect pin, menu bar. | ✅ |
| 4 — Routing assistant | `RoutingCostModel`, A* (initially 4-connectivity top-layer only), highlight top-3 paths. | ✅ |
| 5 — KiCAD I/O | `KiCadNetlistImporter` + `KiCadNetlistExporter`. Round-trip test. | ✅ |
| 6 — Full DRC + exports | DRC: FLOATING_PIN, BARE_WIRE_CROSS (+ later: BARE_WIRE_INTERIOR_CROSS, OFF_PAD_JOINT, OFF_BOARD_PIN, GROUP_*, HALF_ORPHAN_WIRE, WIRE_PIN_OFF_PLATED_THROUGH). Exports: SVG, BOM CSV, PDF, PNG, multi-panel A4 PDF template. | ✅ |
| 7 — Nice-to-have | Full autorouter (A* with rip-up & retry, MacroCommand). Groups panel and group encapsulation (root group, exported pads, re-export chain). Two-layer routing (top + bottom), `preferred_side`, under-component penalty. Iterative simplify after autoroute. Steiner-tree seeding. `near_pin_cost`. `RoutingSettings` + `routing.yaml`. `insert_pad_nodes`. `FlipWireFragmentCommand`. Unified drag pipeline (`DragSet`, `DragDetector`, `MoveDragSetCommand`, preference-driven soft resolution). Multi-group / mixed-drag. Selection FSM rev 3. Wire-pin (virtual goldpin). Native Rust A*. Paint-perf refactor. | ✅ |

**Not yet implemented:**

- Group *instances* (re-use one designed sub-circuit several times).
- Breadboard / arbitrary matrix boards beyond perf/strip.
- Plugin API for routers, DRC checks, exporters.
- Eagle / Fritzing netlist import.
- Undo / redo for `check_and_resolve` (placement conflict dialog).
- Keyboard modifiers to bypass pad-conflict during placement.
- Kapton-tape per-pad modifier — described in
  [pad-pin-wire-junction-semantics.md](pad-pin-wire-junction-semantics.md),
  storage and rendering still to do.
