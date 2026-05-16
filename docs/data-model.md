# Model danych — szczegółowa specyfikacja

## 1. Jednostki

```python
# protoboard/units.py
class RasterUnit(NewType('RasterUnit', float)):
    """Jednostka rastra. 1.0 = standardowy pitch (typowo 2.54 mm)."""

class Millimeter(NewType('Millimeter', float)):
    """Bezwzględny wymiar fizyczny."""
```

- Wewnętrznie wszystkie pozycje w `RasterUnit` jako `float` (NIE int — gapy ułamkowe i ujemne są legalne).
- Konwersja `RasterUnit ↔ mm` via `Board.raster_pitch_mm` (domyślnie 2.54).
- UI ma przełącznik wyświetlania: HP (raster units), mm, cale.

## 2. Box model dla płytek

Płytka opisana jako drzewo boxów w stylu TeX-owym. Layout liczony w `RasterUnit`.

```python
# protoboard/board/boxes.py
class SizePolicy(Enum):
    FIXED = "fixed"      # rozmiar zadany jawnie
    HUG   = "hug"        # dopasuj do zawartości
    FILL  = "fill"       # rozciągnij do dostępnej przestrzeni

@dataclass
class Box(ABC):
    width_policy: SizePolicy
    height_policy: SizePolicy
    width: float | None  = None  # wymagane gdy policy=FIXED
    height: float | None = None
    margin: Insets = Insets.zero()  # może być ujemny

@dataclass
class HBox(Box):
    children: list[Box]
    gap: float = 1.0  # może być ułamkowy, ujemny, zero

@dataclass
class VBox(Box):
    children: list[Box]
    gap: float = 1.0

@dataclass
class Grid(Box):
    cell: Box           # template komórki
    rows: int
    cols: int
    row_gap: float = 1.0
    col_gap: float = 1.0

# Atomy (liście drzewa):
@dataclass
class PadAtom(Box):
    """Pojedynczy otwór."""
    layers: PadLayers   # zob. sekcja 3
    label: str | None = None

@dataclass
class StripAtom(Box):
    """Ciągły pasek N otworów połączonych galwanicznie."""
    length: int | Literal["fill"]
    orientation: Literal["horizontal", "vertical"]
    layers: PadLayers
    cuttable: bool = True   # czy ścieżka da się przeciąć

@dataclass
class GapAtom(Box):
    """Pusta przestrzeń, brak padów."""
    pass
```

**Layout engine (czysta funkcja):**
```python
def layout(box: Box, available: Size) -> LayoutResult:
    """Zwraca pozycje wszystkich atomów w RasterUnit."""
```

Algorytm dwuprzebiegowy: 1) intrinsic size pass (oblicz HUG od dołu drzewa), 2) layout pass (rozdziel FILL przestrzeń).

**Przykłady:**
```yaml
# perfboard 30x20:
type: Grid
rows: 20
cols: 30
row_gap: 1.0
col_gap: 1.0
cell:
  type: PadAtom
  layers: {top: true, bottom: true, plated_through: true}

# stripboard 30x20:
type: VBox
gap: 1.0
children:
  - {type: StripAtom, length: 30, orientation: horizontal, layers: ...}
  # ... powtórzone 20 razy (lub przez Grid z StripAtom)

# breadboard z power rails (gap < 1 między rails, gap > 1 między blokami):
type: VBox
gap: 0.0
children:
  - {type: StripAtom, length: fill, orientation: horizontal, label: "+"}
  - {type: StripAtom, length: fill, orientation: horizontal, label: "−", margin: {top: -0.5}}
  - {type: GapAtom, height: 1.5}
  - {type: Grid, rows: 5, cols: 30, cell: {type: StripAtom, length: 5, orientation: vertical}}
  # ...
```

## 3. Pady i warstwy

```python
@dataclass
class PadLayers:
    top: bool = True              # czy pad istnieje na warstwie górnej
    bottom: bool = False          # czy pad istnieje na warstwie dolnej
    plated_through: bool = False  # galwanicznie połączone (top↔bottom)
    # plated_through ma sens tylko gdy top=True i bottom=True
```

**Implikacje dla connectivity:**
- `top=True, bottom=False`: jeden węzeł na warstwie górnej.
- `top=True, bottom=True, plated=True`: jeden węzeł (top i bottom są elektrycznie tym samym).
- `top=True, bottom=True, plated=False`: **dwa niezależne węzły**, użytkownik musi je połączyć ręcznie.

## 4. Komponenty i footprinty

Szczegółowa specyfikacja geometrii, obrotu, flip i bbox: [geometry.md](geometry.md).

```python
@dataclass
class ComponentDefinition:
    name: str                          # "LM358"
    footprint: FootprintTemplate       # DIP(8)
    pin_names: dict[int, str]          # {1:"OUT_A", 2:"IN-_A", ...}
    description: str = ""
    metadata: dict[str, Any] = field(default_factory=dict)

@dataclass
class ComponentInstance:
    id: UUID
    definition_ref: str                # nazwa z biblioteki
    refdes: str                        # "U1", "R5"
    value: str                         # "LM358", "10k"
```

Biblioteka: katalog `library/` z plikami YAML (`opamps.yaml`, `transistors.yaml`, `connectors.yaml`). Custom: `~/.config/protoboard-designer/library/`.

## 5. Schematic = graf logiczny (netlist)

```python
@dataclass
class PinRef:
    component_id: UUID
    pin_number: int

@dataclass
class Net:
    id: UUID
    name: str                # "VCC", "N$12"
    pins: set[PinRef]

@dataclass
class Schematic:
    components: dict[UUID, ComponentInstance]
    nets: dict[UUID, Net]
    # Brak geometrii — to czysty graf wymagań.
```

## 6. Layout = fizyczna realizacja

```python
@dataclass
class ComponentPlacement:
    component_id: UUID
    position: RasterPosition       # gdzie pin 1 leży
    rotation: Literal[0, 90, 180, 270]
    side: Literal["top", "bottom"]

@dataclass
class WireSegment:
    id: UUID
    path: list[RasterPosition]
    insulated: bool
    side: Literal["top", "bottom"]
    color: str | None = None

@dataclass
class StripCut:
    id: UUID
    strip_atom_ref: str
    position: int

@dataclass
class Layout:
    placements: dict[UUID, ComponentPlacement]
    wires: list[WireSegment]
    cuts: list[StripCut]
```

## 7. ConnectivityGraph

```python
class ConnectivityGraph:
    """Graf elektryczny z Board + Layout. Każdy węzeł = elektrycznie odrębny punkt."""

    def __init__(self, board: Board, layout: Layout, components: dict[UUID, ComponentDefinition]):
        self._g = networkx.Graph()
        self._build()

    def electrical_nets(self) -> list[set[PinRef]]: ...
    def shorts(self) -> list[Short]: ...
```

**DRC = porównanie `Schematic.nets` z `ConnectivityGraph.electrical_nets()`.**

Szczegóły inkrementalnej implementacji (union-find): [connectivity.md](connectivity.md).

## 8. Pad-conflict i auto-tworzenie netów

### Konflikt przy placement/drag

**`domain/layout/pad_conflict.py`** — czyste funkcje domenowe:
- `find_conflicts()` — wykrywa gdy pin nowego komponentu trafia na pad istniejącego.
- `find_wire_conflicts()` — wykrywa gdy pin nowego komponentu trafia na punkt `WireSegment.path`.

`ConflictKind` enum:

| Wartość | Znaczenie | Akcja |
|---|---|---|
| `SAME_NET` | Oba piny w tej samej sieci | Auto-OK |
| `DIFFERENT_NETS` | Różne sieci | Blokada |
| `ONE_UNCONNECTED` | Jeden pin bez sieci | Dialog z pytaniem |
| `BOTH_UNCONNECTED` | Oba bez sieci | Dialog — tworzy nowy net |
| `PIN_ON_WIRE_SAME_NET` | Pin i drut w tej samej sieci | Auto-OK |
| `PIN_ON_WIRE_DIFFERENT_NET` | Pin i drut w różnych sieciach | Blokada |
| `PIN_ON_WIRE_WITH_NET` | Drut ma net, pin nie (lub odwrotnie) | Auto-join bez dialogu |
| `PIN_ON_WIRE_UNCONNECTED` | Ani pin ani drut nie mają netu | Auto-tworzy nowy net |

`PadConflict.shared_pins: frozenset[int]` na `ComponentPlacement` oznacza które piny zostały świadomie współdzielone.

**`gui/placement_conflict.py`** — `check_and_resolve()`: wywołuje obie funkcje domenowe, pokazuje `PadConflictDialog`, mutuje `session.schematic.nets`, zwraca zaktualizowany `ComponentPlacement` lub `None` przy blokadzie.

### Auto-tworzenie netów przy rysowaniu drutu

**`domain/schematic/wire_net_ops.py`**:
- `find_pin_at(pt, session)` — zwraca `(comp_id, pin_num)` jeśli punkt trafia w pin (tolerancja 1e-4).
- `classify_wire_nets(wire, session)` → `WireNetKind`.

**`gui/wire_net_resolver.py`** — `resolve_wire_nets(session, wire, cmd)`: mutuje nety, rejestruje diff w `cmd.record_net_change()`.

**Przepływ**: `RouteTool._commit()` → `AddWireWithNetsCommand` → `resolve_wire_nets()` → `stack.execute(cmd)`.

## 9. Grupy komponentów (ComponentGroup)

```python
@dataclass(frozen=True)
class ExportedPin:
    component_id: uuid.UUID
    pin_number: int

@dataclass(frozen=True)
class ComponentGroup:
    id: uuid.UUID
    name: str
    component_ids: frozenset[uuid.UUID]
    child_group_ids: frozenset[uuid.UUID]
    exported_pins: frozenset[ExportedPin]
```

**Root group:** `ROOT_GROUP_ID = UUID("00000000-0000-0000-0000-000000000001")`. Tworzony przez `Layout.ensure_root_group()`. Nie wyświetlany w GroupsPanel.

**GUI — `SelectTool._group_stack`:**
- `[ROOT_GROUP_ID]` — root level; wszystko dostępne.
- `[ROOT_GROUP_ID, g1]` — wewnątrz g1; reszta wyszarzona (alpha 0.2/0.45).
- Double-click → push; Esc → pop.

**Persistence:** layout.yaml serializuje grupy z `id`, `name`, `component_ids`, `child_group_ids`, `exported_pins`.
