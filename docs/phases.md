# Fazy implementacji

### Faza 0 — Bootstrap ✅
- Setup repo: `pyproject.toml`, `uv`, `ruff`, `black`, `mypy --strict`, `pytest`, pre-commit.
- CI: GitHub Actions (lint + test + mypy).
- SPDX headers, LICENSE (MIT), README.md, CHANGELOG.md.

### Faza 1 — Domain Model + Box Layout ✅
- `units.py`, `geometry.py`.
- Box model + layout engine (HUG/FILL/FIXED, gap ułamkowy/ujemny).
- Board wrapper + YAML serializer.

### Faza 2 — Components + Schematic + Layout + Connectivity ✅
- FootprintTemplate, ComponentDefinition, biblioteka YAML.
- Schematic (Net, PinRef), Layout (Placement, WireSegment, StripCut).
- ConnectivityGraph (networkx). DRC: SHORT, MISSING_NET, OVERLAP.
- CommandStack + PlaceComponent/MoveComponent/RemoveComponent.

### Faza 3 — GUI MVP ✅
- MainWindow, BoardView, snap, tools (Select/Place/RouteWire/CutStrip).
- Pad-conflict detection, body-overlap blocking, pin-na-drucie.
- Auto-tworzenie netów przy rysowaniu drutu. Multi-selection. Wire editing.
- Grupy komponentów, Disconnect pin, menu bar.
- **563 testów.**

### Faza 4 — Routing assistant ✅
- RoutingCostModel, A* (4-connectivity, top layer), highlight top-3 tras.
- **13 testów A* routera.**

### Faza 5 — KiCAD I/O ✅
- KiCadNetlistImporter + KiCadNetlistExporter. Roundtrip test.

### Faza 6 — Pełne DRC + eksporty ✅
- DRC: FLOATING_PIN, BARE_WIRE_CROSS.
- Eksport SVG, BOM CSV, PDF, PNG.
- [ ] Print UI (PDF już to robi, pozostaje UI).

### Faza 7 — Nice-to-have ✅ (większość)
- Autorouter pełny (A* z rip-up & retry, MacroCommand).
- Groups panel. Grupy — enkapsulacja, root group, eksport padów.
- Wielowarstwowy routing (top+bottom), preferred_side, kara pod komponentem.
- Iteracyjna optymalizacja autorouter. Steiner tree greedy. near_pin_pads.
- RoutingSettings + routing.yaml. insert_pad_nodes. FlipWireFragmentCommand.

**Pozostałe (nie zaimplementowane):**
- Instancje grup (wielokrotne użycie sub-układu).
- Breadboard, custom matrix boards.
- Plugin API.
- Wsparcie Eagle/Fritzing netlist.
- Undo/redo dla `check_and_resolve`.
- Klawiaturowe modyfikatory do bypass-u pad-conflict.
