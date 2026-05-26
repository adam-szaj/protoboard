# Persistence i KiCAD I/O

## Struktura projektu (folder)

```
my_project/
├── board.yaml            # definicja box-model płytki
├── schematic.yaml        # netlist (logical)
├── layout.yaml           # placements, wires, groups (physical)
├── instances.yaml        # instancje komponentów (id, refdes, value)
├── routing.yaml          # ustawienia autorouter per projekt (opcjonalny)
├── library/              # nadpisania/dodatki do globalnej biblioteki
│   └── custom_parts.yaml
└── exports/              # generowane (gitignored opcjonalnie)
    ├── netlist.net
    ├── bom.csv
    └── render.svg
```

`load_project()` zwraca 5-krotkę `(board, layout, schematic, instances, routing_settings)`. Brak `routing.yaml` → domyślne `RoutingSettings()`.

## Format wire — v1 vs v2

`layout.yaml` ma pole `format_version` (od 2026-05-27):

- **`format_version: 1`** (default dla starych plików; pominięcie traktowane
  jak v1) — `wires:` to lista `WireSegment` (jeden per side-segment); brak
  bloku `knots`.
- **`format_version: 2`** (pisany gdy `layout.wires_v2` lub `layout.knots`
  jest zapełnione) — projekt zawiera **dodatkowe** bloki:
  - `knots:` lista (id, kind: pin|joint, pin lub position)
  - `wires_v2:` lista (id, a_knot_id, b_knot_id, insulated, fragments:[…])

Legacy `wires:` lista zostaje zapisywana dla kompat z runtime przed pełnym
cutoverem (Commit C w refactorze wire-model-knots). Pliki v1 ładują się
bezbólowo; v2-aware loader buduje też `Layout.knots` i `Layout.wires_v2`.

Migracja v1 → v2: caller (typowo session.py po `load_project`) wywołuje
`migrate_layout_to_v2(layout, board, pin_index, pin_position_resolver)`
gdy biblioteka footprintów jest już dostępna. Funkcja:
1. Rezolwuje endpointy każdego legacy WireSegment do PinKnot (gdy
   coincidence z `pin_world_position`) albo JointKnot na padzie.
2. Wstawia każdy segment jako jednofragmentowy Wire.
3. Iteratywnie wywołuje `try_collapse_joint` na każdym JointKnocie z
   degree=2 — łączy te same-insulated wires, jeśli `side` się zgadza lub
   pozycja joint to plated-through (wtedy zostają dwa fragmenty z via).

## KiCAD I/O

### Import (`.net` XML, format `<export version="D">`)

```python
class KiCadNetlistImporter:
    def import_(self, path: Path) -> Schematic: ...
```

Mapowanie footprintów: heurystyka po nazwie (`Package_DIP:DIP-8_W7.62mm` → `DIP(8)`, `R_Axial`, `TO-92`, `PinHeader`). Nieznane footprinty: warning + import sieci bez geometrii.

### Eksport

Format `.net` zgodny z importem do KiCAD PCB editor. Nie generujemy `.kicad_sch`.

### Parser

- `protoboard/kicad/parser.py` — własny minimalistyczny parser S-expression (MIT), tylko podzbiór potrzebny do netlisty.
- `protoboard_kicad_gpl/` — osobny pakiet (GPL, opcjonalny), oparty o `kiutils`.

### Roundtrip

Export → import zachowuje nazwy sieci i liczbę pinów (test w `tests/integration/`).
