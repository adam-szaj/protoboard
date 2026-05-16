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
