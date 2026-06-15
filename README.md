# protoboard-designer

A **connectivity-aware** desktop layout designer for prototyping boards
(perfboard / stripboard), built with Python and PySide6 (Qt 6).

![CI](https://github.com/adam-szaj/protoboard-designer/actions/workflows/ci.yml/badge.svg)
![License: MIT](https://img.shields.io/badge/license-MIT-blue.svg)
![Python](https://img.shields.io/badge/python-3.11%2B-blue.svg)

Unlike a purely graphical drawing tool, the internal data model is a
**netlist graph** — the physical layout you see on screen is a *projection*
of that graph onto the board's hole grid. Because the app always knows
which holes are electrically connected, it can run design-rule checks,
import/export KiCAD netlists, and suggest (or fully compute) wire routes.

---

## Features

### Board modelling
- **Perfboard and stripboard** support, with configurable grid size and
  pitch. A two-pass box-layout engine computes pad positions.
- **Stripboard cuts** — break a copper strip between two pads to split a net.
- **Double-sided boards** — wires on the top or bottom layer, with
  plated-through holes carrying connectivity between sides.
- Example boards under `boards/` (e.g. `breadboard_full.yaml`).

### Components & library
- Built-in **component library** (`library/`) covering passives, op-amps,
  transistors, diodes, voltage regulators, crystals, connectors, the
  74xx TTL and CD40xx CMOS families, and more.
- Footprints for DIP-N, axial/radial passives, SIL headers, TO-92, and
  configurable pin-header grids.
- Place, **rotate** (0/90/180/270°) and **flip** components; live snapping
  to the nearest pad center.
- Editable RefDes / value / rotation via the properties panel.
- Add your own libraries by pointing the app at extra YAML directories in
  Preferences.

### Connectivity & nets
- Incremental connectivity graph (`networkx` + a custom union-find) keeps
  net membership up to date as you wire, cut, and move parts.
- **Net-aware highlighting** — select a pad or pin to see its whole net light
  up across the board.

### Wiring
- Multi-segment **wire routing tool** with same-side and cross-side (via)
  fragments; one continuous solderable run is always modelled as a single
  wire.
- **Wire editing** — drag path vertices, excise/replace fragments, flip a
  fragment to the other layer, toggle bare vs. insulated.
- **Bare vs. insulated** wires are rendered and design-rule-checked
  differently (bare wires short whatever pads they cross).

### Routing assistant
- A* **autorouter** with a configurable cost model (free pad, same-net pad,
  occupied cell, direction change, near-pin penalties, …).
- **Route hints** — the route tool shows the top candidate paths as you draw.
- **Autoroute all nets** in one pass, followed by iterative simplification and
  "unweave" passes that straighten and untangle the result.
- Optional high-performance **Rust A\*** backend (PyO3) for large boards.

### Design Rule Check (DRC)
- Checks for shorts, missing nets, component overlaps, floating pins,
  bare-wire crossings, off-pad joints, and invalid group exports.
- Results appear in a dockable DRC panel; clicking an issue highlights and
  centers the offending element.

### Groups
- Group components (and sub-groups) into reusable, hierarchical blocks.
- **Exported pins** let a group present a clean external interface; routing
  and DRC can treat a group much like a single component.
- Multi-group and mixed selection drag with conflict-aware wire handling.

### KiCAD interoperability
- **Import** a KiCAD XML netlist into a schematic, with a footprint
  heuristic for common parts (unknown footprints raise a warning, not an
  error).
- **Export** the schematic back to KiCAD netlist format; round-trips
  preserve net names and pin counts.

### Import / export
- Native project format is human-readable **YAML** (`ruamel.yaml`):
  `board.yaml` + `layout.yaml` + `schematic.yaml` per project folder.
- Export to **SVG**, **PDF** (including a multi-panel A4 print template at
  1:1 scale), and **PNG** — all WYSIWYG with the on-screen board view.
- Export a **Bill of Materials** as CSV.

### Editing & UX
- Full **undo/redo** via a command stack (`Ctrl+Z` / `Ctrl+Y`).
- Multi-selection, rubber-band select (left-to-right = fully enclosed,
  right-to-left = touched), and Shift+click add/remove.
- Customizable toolbars and keybindings.

---

## Architecture

The codebase is layered, and the domain model is deliberately **Qt-free**
(fully testable without a display server):

```
GUI Layer (PySide6 / QGraphicsScene)
  ↑ Qt signals
Application Layer (CommandStack, Tools, Selection)
  ↑ Commands
Domain Model (pure Python — no Qt)
  ↑
Services (DRC, Router, KiCAD I/O, Persistence)
```

See [`CLAUDE.md`](CLAUDE.md) and the [`docs/`](docs/) directory for detailed
documentation of the data model, geometry, routing, commands, persistence,
DRC, GUI, and connectivity subsystems.

| Layer | Technology |
|---|---|
| Language | Python 3.11+ |
| GUI | PySide6 (Qt 6), `QGraphicsScene` / `QGraphicsView` |
| Graphs | `networkx` + custom incremental union-find |
| Routing | Custom A* (Python, with an optional Rust/PyO3 backend) |
| Persistence | YAML (`ruamel.yaml`) |
| KiCAD I/O | Custom S-expression / XML parser |
| Tests | `pytest` + `pytest-qt` |
| Tooling | `ruff` + `black` + `mypy --strict`, packaged with `uv` |

---

## Installation from source

### Prerequisites

- **Python 3.11 or newer**
- **[uv](https://docs.astral.sh/uv/)** — the project's package/venv manager
  (`pip install uv`, or see the uv install docs)
- A Linux desktop with an X11 / XWayland session. The app forces
  `QT_QPA_PLATFORM=xcb`, so it runs under Wayland via XWayland without any
  manual setup.
- *(Optional)* a **Rust toolchain** ([rustup](https://rustup.rs/)) if you
  want to build the native A* backend.

### 1. Clone the repository

```bash
git clone https://github.com/adam-szaj/protoboard-designer.git
cd protoboard-designer
```

### 2. Create a virtual environment and install

```bash
uv venv
source .venv/bin/activate
uv pip install -e ".[dev]"
```

This installs the app in editable mode together with the development
dependencies (pytest, ruff, black, mypy, etc.). For a runtime-only install,
drop the `[dev]` extra:

```bash
uv pip install -e .
```

### 3. (Optional) build the Rust A* backend

The pure-Python router works out of the box; the Rust backend just makes
autorouting large boards faster. With a Rust toolchain installed:

```bash
uv pip install maturin
cd src/protoboard/services/routing/astar_rs
maturin develop --release
cd -
```

If the native module is absent, the app transparently falls back to the
Python implementation.

### 4. Run the application

```bash
protoboard
```

Useful flags:

```bash
protoboard --version        # print version and exit
protoboard --verbose        # DEBUG-level logging
protoboard --help
```

On first launch a **welcome dialog** lets you create a new project or open an
existing one.

### 5. (Optional) install the pre-commit hooks

If you plan to contribute:

```bash
pre-commit install
```

---

## Development

Common commands once the dev environment is active:

```bash
# Lint
ruff check src/ tests/

# Format check
black --check src/ tests/

# Type check (strict)
mypy src/

# Run the full test suite
pytest

# Run a single test
pytest tests/unit/test_layout_engine.py::test_name -v

# Run with coverage
pytest --cov=protoboard --cov-report=term-missing

# Property-based (hypothesis) tests are skipped by default; opt in with:
pytest -m property
```

---

## Building standalone bundles

[PyInstaller](https://pyinstaller.org/) build scripts produce self-contained
distributions (no Python install required on the target machine):

```bash
# Linux  → dist/protoboard-designer-<VERSION>-linux-x86_64.tar.gz
./packaging/build-linux.sh

# Windows → dist\protoboard-designer-<VERSION>-windows-x86_64.zip
powershell -ExecutionPolicy Bypass -File packaging\build-windows.ps1
```

See [`packaging/README.md`](packaging/README.md) for what gets bundled and how
versioning works.

---

## License

**MIT** — see [LICENSE](LICENSE).

The optional `kicad_integration_gpl` module (when enabled) is licensed under
**GPLv3**; the core, KiCAD parser, GUI, and data models remain MIT.
