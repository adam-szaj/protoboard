# Persistence and KiCAD I/O

## Project layout (on disk)

```
my_project/
├── board.yaml             # box-model board definition
├── schematic.yaml         # netlist (logical)
├── layout.yaml            # placements, wires, knots, groups (physical)
├── instances.yaml         # component instances (id, refdes, value)
├── routing.yaml           # per-project router settings (optional)
├── session_commands.yaml  # auto-recorded command stream (see command-recorder.md)
├── library/               # project-local library overrides / additions
│   └── custom_parts.yaml
└── exports/               # generated artefacts (optional gitignore)
    ├── netlist.net
    ├── bom.csv
    └── render.svg
```

`load_project()` returns `(board, layout, schematic, instances, routing_settings)`.
Missing `routing.yaml` falls back to default `RoutingSettings()`.

## Wire format

Every save uses **`format_version: 2`** — the knot-aware layout schema. The
top-level `wires:` block holds the v2 records and is paired with a `knots:`
block:

```yaml
format_version: 2
knots:
  - id: 7b3c…
    kind: pin              # "pin" | "joint" | "wire_pin"
    pin: {component_id: 4a…, pin_number: 1}
  - id: 9e21…
    kind: joint
    position: {x: 4.5, y: 3.5}
  - id: 1c80…
    kind: wire_pin
    position: {x: 6.5, y: 2.5}
    net_id: 88…
wires:
  - id: 2f5a…
    a_knot_id: 7b3c…
    b_knot_id: 9e21…
    insulated: true
    fragments:
      - side: top
        path: [{x: 0.5, y: 0.5}, {x: 1.5, y: 0.5}]
```

Older files that still hold `format_version: 1` (legacy `WireSegment` list,
no `knots:` block) are migrated on load by
`services/persistence/wire_migration.py::migrate_layout_to_v2`. The migration:

1. Resolves each legacy segment endpoint into a `PinKnot` (when it coincides
   with a `pin_world_position`) or a `JointKnot` on the pad.
2. Inserts every segment as a single-fragment `Wire`.
3. Iteratively calls `try_collapse_joint` on each degree-2 `JointKnot` — same
   insulated value plus either same `side` or a plated-through joint position
   collapses two wires into one (with a fragment boundary at the via).

The migration runs once at load time. The save path always emits
`format_version: 2`.

## Groups

`layout.yaml` serialises every `ComponentGroup` with `id`, `name`,
`component_ids`, `child_group_ids`, `exported_pins`, and
`lock_internal_wires`. The loader runs the encapsulation validator (see
[data-model.md §9](data-model.md)) leaves-first and silently strips
`exported_pins` entries that violate the re-export chain; the count is
logged at INFO.

## KiCAD I/O

### Import (`.net` XML, `<export version="D">`)

```python
class KiCadNetlistImporter:
    def import_(self, path: Path) -> Schematic: ...
```

Footprint mapping is a name-heuristic over the KiCAD footprint string
(`Package_DIP:DIP-8_W7.62mm` → `DIP(8)`, `R_Axial`, `TO-92`, `PinHeader`, …).
Unknown footprints are imported as netlist-only (no geometry) with a warning.

### Export

The exporter writes `.net` files compatible with the KiCAD PCB editor. We do
*not* generate `.kicad_sch`.

### Parser

- `protoboard/services/kicad/parser.py` — small in-tree S-expression parser
  (MIT), only the subset needed for the netlist.
- `protoboard_kicad_gpl/` — separate optional package (GPL) on top of
  `kiutils`.

### Round-trip

Export → import preserves net names and pin counts (`tests/integration/`).

## Git integration

When the user saves, two commits are made in the project directory (the
repo is auto-initialised on the first save):

1. **Session log commit** — stages `session_commands.yaml` only. Message:
   `Session log: N commands`. The recorder is reset to an empty log on
   success.
2. **Project snapshot commit** — `git add -A` for the rest (board, layout,
   schematic, the now-empty log). Message: `Save: <project_name>`.

Every git commit is a self-contained snapshot; the log between commits
describes the *path* that produced it. Implementation:
`services/persistence/git_repo.py` (thin subprocess wrapper, no GitPython) +
`MainWindow._commit_save`. Git errors surface in the status bar but do not
roll back the save.
