# CLAUDE.md — Perfboard/Stripboard Layout Designer

## 0. Cel projektu i pozycjonowanie

Aplikacja desktop (Linux, PySide6) do projektowania fizycznego rozmieszczenia komponentów i prowadzenia połączeń na uniwersalnych płytkach prototypowych (perfboard, stripboard, w przyszłości breadboard i custom).

**Kluczowa różnica względem DIYLayout Creator:** projekt jest *connectivity-aware*. Wewnętrzny model danych to graf logicznych sieci (netlist), a widok fizyczny płytki jest projekcją tego grafu na siatkę otworów. Dzięki temu możliwe jest:

- DRC (Design Rule Check) — weryfikacja, że fizyczny layout realizuje wszystkie sieci z netlisty i nie tworzy zwarć,
- import netlisty z KiCAD jako punktu wyjścia,
- eksport netlisty do KiCAD (do importu w PCB editor),
- inteligentne sugestie routingu uwzględniające izolację przewodów i topologię padów dwustronnych.

**Nazwa robocza:** `protoboard-designer` (do zmiany).

## 1. Stack technologiczny

| Warstwa | Wybór | Uzasadnienie |
|---|---|---|
| Język | Python 3.11+ | Szybki development, bogaty ekosystem do parsingu i grafów |
| GUI | PySide6 (Qt 6) | `QGraphicsScene`/`QGraphicsView` idealne pod edytor 2D z warstwami; cross-platform na przyszłość |
| Grafy | `networkx` | Algorytmy connectivity, A*, weryfikacja DRC |
| Routing | własny A* na siatce + `networkx` jako fallback | Customowe koszty (jumper, izolacja, cięcie paska) |
| Persistence | YAML (`ruamel.yaml`) | Czytelność, diff-friendly w gicie, zachowuje kolejność i komentarze |
| KiCAD I/O | własny parser S-expression (MIT) + opcjonalny moduł `kiutils` (GPL) | Licencja dualna |
| Testy | `pytest` + `pytest-qt` | Standard |
| Linter/format | `ruff` + `black` + `mypy --strict` | Strict typing od początku |
| Pakowanie | `uv` (środowisko) + `pyproject.toml` (PEP 621) | Nowoczesny, szybki |

**Brak zależności od KiCAD CLI** — wszystko czytane/pisane jako pliki.

## 2. Licencja

- **Rdzeń projektu, własny parser KiCAD, GUI, modele danych:** MIT.
- **Opcjonalny moduł `kicad_integration_gpl`:** GPLv3 (jeśli kiedyś użyjemy `kiutils` lub innych GPL-owych bibliotek KiCAD). User świadomie instaluje przez `pip install protoboard-designer[kicad-gpl]`.
- Każdy plik źródłowy ma SPDX header: `# SPDX-License-Identifier: MIT` (lub `GPL-3.0-or-later` w module GPL).

## 3. Architektura — przegląd warstw

```
┌─────────────────────────────────────────────────────────┐
│  GUI Layer (PySide6)                                    │
│  - MainWindow, BoardView (QGraphicsView), Panels        │
│  - Renderery dla Board/Component/Wire (QGraphicsItem)   │
└─────────────────────────────────────────────────────────┘
                          ▲
                          │ obserwuje zmiany w modelu (Qt signals)
                          │
┌─────────────────────────────────────────────────────────┐
│  Application Layer                                      │
│  - CommandStack (undo/redo)                             │
│  - Komendy: PlaceComponent, RouteWire, CutStrip, ...    │
│  - Selection, Tools (place, route, delete, ...)         │
└─────────────────────────────────────────────────────────┘
                          ▲
                          │ operuje na
                          │
┌─────────────────────────────────────────────────────────┐
│  Domain Model (pure Python, no Qt)                      │
│  - Project: Board + ComponentLibrary + Schematic +      │
│             Layout + Settings                           │
│  - Board (box-model), Pad, Strip                        │
│  - Component, Footprint, Pin                            │
│  - Net (logical), WireSegment/Jumper (physical)         │
│  - ConnectivityGraph (networkx)                         │
└─────────────────────────────────────────────────────────┘
                          ▲
                          │ używane przez
                          │
┌─────────────────────────────────────────────────────────┐
│  Services                                               │
│  - DRCEngine                                            │
│  - Router (A* z customowymi kosztami)                   │
│  - KiCadImporter / KiCadExporter                        │
│  - ProjectSerializer (YAML)                             │
└─────────────────────────────────────────────────────────┘
```

**Reguła:** Domain Model nie importuje Qt. Można go testować jednostkowo bez display servera.

## 4. Model danych — szczegółowa specyfikacja

### 4.1. Jednostki

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

### 4.2. Box model dla płytek

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
    layers: PadLayers   # zob. 4.3
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

### 4.3. Pady i warstwy

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
- `top=True, bottom=True, plated=False`: **dwa niezależne węzły**, użytkownik musi je połączyć ręcznie (np. lutem przeciągniętym przez otwór).

### 4.4. Komponenty i footprinty

**Footprint = parametryczny wzorzec geometryczny pinów na siatce.**

```python
# protoboard/components/footprints.py
@dataclass
class FootprintTemplate(ABC):
    """Generator pozycji pinów."""
    @abstractmethod
    def pin_positions(self) -> list[PinPosition]: ...
    @abstractmethod
    def pin_count(self) -> int: ...

@dataclass
class SingleRow(FootprintTemplate):
    """SIL-N: N pinów w jednym rzędzie z zadanym pitchem."""
    n: int
    pitch: float = 1.0  # w RasterUnit

@dataclass
class DualRow(FootprintTemplate):
    """DIP: 2 rzędy po n pinów, z numeracją wokół (1,2,..n,n+1,..2n)."""
    n_per_row: int          # tj. DIP-14 → n_per_row=7
    pitch: float = 1.0
    rows_spacing: float = 3.0  # standardowy DIP: 3 rastry (7.62 mm)

@dataclass
class Grid(FootprintTemplate):
    """Macierz np. dla PGA / dużych konektorów."""
    rows: int
    cols: int
    pitch: float = 1.0

# Predefiniowane aliasy:
DIP = lambda n: DualRow(n_per_row=n // 2)              # DIP-8, DIP-14, DIP-16, DIP-28...
SIL = lambda n: SingleRow(n=n)                          # SIL-3, SIL-4, SIL-8...
TO92 = SIL(3)
TO220 = SIL(3)  # mechanicznie inny, ale connectivity-wise tożsamy
```

**Komponent = footprint + mapowanie pin_number → pin_name:**

```python
@dataclass
class ComponentDefinition:
    name: str                          # "LM358"
    footprint: FootprintTemplate       # DIP(8)
    pin_names: dict[int, str]          # {1:"OUT_A", 2:"IN-_A", 3:"IN+_A", 4:"VEE", ...}
    description: str = ""
    metadata: dict[str, Any] = field(default_factory=dict)
    # mechanical_hint: orientacja flat side dla TO-92 itp. — nie wpływa na connectivity

@dataclass
class ComponentInstance:
    """Konkretna sztuka komponentu w projekcie."""
    id: UUID
    definition_ref: str                # nazwa z biblioteki
    refdes: str                        # "U1", "R5"
    value: str                         # "LM358", "10k"
    # Geometria nadawana w Layout (poniżej), nie w samym komponencie.
```

Biblioteka komponentów: katalog `library/` z plikami YAML grupowanymi tematycznie (`opamps.yaml`, `transistors.yaml`, `connectors.yaml`). Custom komponenty użytkownika w `~/.config/protoboard-designer/library/`.

### 4.5. Schematic = graf logiczny (netlist)

```python
@dataclass
class PinRef:
    """Referencja do konkretnego pinu konkretnej instancji."""
    component_id: UUID
    pin_number: int

@dataclass
class Net:
    """Logiczna sieć — zbiór pinów, które MAJĄ być elektrycznie połączone."""
    id: UUID
    name: str                # "VCC", "N$12"
    pins: set[PinRef]

@dataclass
class Schematic:
    components: dict[UUID, ComponentInstance]
    nets: dict[UUID, Net]
    # Brak geometrii — to czysty graf wymagań.
```

### 4.6. Layout = fizyczna realizacja

```python
@dataclass
class ComponentPlacement:
    component_id: UUID
    position: RasterPosition       # gdzie pin 1 leży
    rotation: Literal[0, 90, 180, 270]
    side: Literal["top", "bottom"]

@dataclass
class WireSegment:
    """Fizyczny przewód między dwoma punktami siatki."""
    id: UUID
    path: list[RasterPosition]     # punkty siatki, sąsiednie pozycje
    insulated: bool                # izolowany czy goły
    side: Literal["top", "bottom"]
    color: str | None = None       # do wizualizacji

@dataclass
class StripCut:
    """Przecięcie paska na stripboardzie."""
    id: UUID
    strip_atom_ref: str            # ID atomu Strip
    position: int                  # po którym otworze (od początku paska)

@dataclass
class Layout:
    placements: dict[UUID, ComponentPlacement]
    wires: list[WireSegment]
    cuts: list[StripCut]
```

### 4.7. ConnectivityGraph — most między modelami

```python
class ConnectivityGraph:
    """Buduje graf elektryczny z Board + Layout. Każdy węzeł = elektrycznie
    odrębny punkt. Pady plated połączone, pasek = jeden węzeł (chyba że cut),
    wire łączy dwa węzły, pin komponentu siedzi na padzie."""

    def __init__(self, board: Board, layout: Layout, components: dict[UUID, ComponentDefinition]):
        self._g = networkx.Graph()
        self._build()

    def electrical_nets(self) -> list[set[PinRef]]:
        """Zwraca de facto sieci wynikające z fizycznego layoutu."""

    def shorts(self) -> list[Short]:
        """Czy są pady połączone, które nie powinny być?"""
```

**DRC = porównanie `Schematic.nets` z `ConnectivityGraph.electrical_nets()`.**

### 4.8. Pad-conflict i auto-tworzenie netów

#### Konflikt przy placement/drag (pad-na-pad i pin-na-drucie)

Gdy komponent jest stawiany lub przeciągany, GUI sprawdza konflikty w dwóch warstwach:

**`domain/layout/pad_conflict.py`** — czyste funkcje domenowe:
- `find_conflicts()` — wykrywa gdy pin nowego komponentu trafia na pad istniejącego.
- `find_wire_conflicts()` — wykrywa gdy pin nowego komponentu trafia na punkt `WireSegment.path`. Net drutu jest określany przez skanowanie wszystkich punktów ścieżki w mapie `point_to_net` (zbudowanej przez GUI z pozycji pładnych pinów z netem).

`ConflictKind` enum — klasyfikacja konfliktu:

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

**`gui/placement_conflict.py`** — orchestracja GUI:
- `check_and_resolve()` wywołuje obie funkcje domenowe, pokazuje `PadConflictDialog` gdy potrzebna decyzja, mutuje `session.schematic.nets` bezpośrednio i zwraca zaktualizowany `ComponentPlacement` (z `shared_pins`) lub `None` przy blokadzie.
- Sprawdzenie body-overlap (bbox komponentów) poprzedza pad-conflict — blokuje tylko gdy żaden pin się nie pokrywa (czyste nakładanie się obudów).

#### Auto-tworzenie netów przy rysowaniu drutu

**`domain/schematic/wire_net_ops.py`** — czyste funkcje:
- `find_pin_at(pt, session)` — zwraca `(comp_id, pin_num)` jeśli punkt trafia w pin komponentu (tolerancja 1e-4).
- `classify_wire_nets(wire, session)` — klasyfikuje parę endpoints drutu jako `WireNetKind` (NO_PINS, BOTH_UNCONNECTED, ONE_UNCONNECTED, SAME_NET, DIFFERENT_NETS).

**`gui/wire_net_resolver.py`** — `resolve_wire_nets(session, wire, cmd)`:
- Wywołuje `classify_wire_nets`, mutuje `session.schematic.nets` i rejestruje każdą zmianę w `cmd.record_net_change()` przed mutacją.
- Zwraca `False` (drut nie jest commitowany) tylko przy DIFFERENT_NETS gdy user anuluje merge dialog.

**`domain/commands/component_cmds.py`** — `AddWireWithNetsCommand`:
- Pierwsza aplikacja: dołącza drut do layoutu (nety już zmutowane przez resolver).
- Redo: przywraca `_nets_after` z powrotem.
- Revert (undo): usuwa drut i przywraca `_nets_before`.

**Przepływ**: `RouteTool._commit()` → tworzy `AddWireWithNetsCommand` → `resolve_wire_nets()` (mutuje nety, rejestruje diff) → `stack.execute(cmd)` (dołącza drut).

## 5. Routing — sugestie

### 5.1. Model przeszkód i kosztów

```python
class RoutingCostModel:
    """Konfigurowalny model kosztów dla A*."""
    free_pad: float = 1.0
    same_net_pad: float = 0.1          # zachęta do "sklejania" sieci
    occupied_by_other_net: float = INF  # kolizja
    crossing_bare_wire: float = INF     # goły × goły = zwarcie
    crossing_with_jumper: float = 5.0   # jeśli user pozwala na jumpery
    strip_cut_cost: float = 3.0
    direction_change: float = 0.2       # delikatna kara za zakręty (estetyka)
```

**Reguła izolacji (kluczowa):**
- przewód izolowany może krzyżować dowolny inny — koszt = `crossing_with_jumper` (jeśli dozwolone w projekcie),
- przewód goły × pad innej sieci → kolizja (INF),
- przewód goły leżący na padzie `plated_through` jednej sieci na warstwie A → connectivity rozprzestrzenia się na warstwę B (intencjonalnie lub jako zwarcie).

### 5.2. Algorytm

A* na grafie siatki gdzie:
- węzły = (x, y, side) dla każdego dostępnego punktu siatki,
- krawędzie = sąsiednie pozycje (4- lub 8-connectivity, konfigurowalne),
- przejście top↔bottom przez `plated_through` pad za darmo,
- heurystyka = manhattan distance,
- cel = którykolwiek pad z target net.

**Tryb sugestii (Faza 4):** podświetl top-K najtańszych ścieżek między aktualnie wybranym pinem a wszystkimi padami docelowej sieci. User wybiera klikiem.

### 5.3. Project setting

```yaml
# w project.yaml
routing:
  allow_jumpers: true       # czy izolowane przewody mogą krzyżować inne
  allow_top_routing: true
  allow_bottom_routing: true
  default_wire_insulated: true
  cost_model:
    crossing_with_jumper: 5.0
    # ... override
```

## 6. Undo/Redo

**Command pattern od początku.** Wszystkie mutacje modelu przez `Command`:

```python
class Command(ABC):
    @abstractmethod
    def apply(self, project: Project) -> None: ...
    @abstractmethod
    def revert(self, project: Project) -> None: ...
    description: str   # "Place R1 at (3,5)"

class CommandStack:
    def execute(self, cmd: Command) -> None: ...   # apply + push, clear redo
    def undo(self) -> None: ...
    def redo(self) -> None: ...
    def can_undo(self) -> bool: ...
    def can_redo(self) -> bool: ...
```

Komendy MVP: `PlaceComponent`, `MoveComponent`, `RotateComponent`, `RemoveComponent`, `AddWire`, `AddWireWithNetsCommand`, `RemoveWire`, `AddStripCut`, `RemoveStripCut`, `RenameNet`, `Macro` (kompozycja).

`AddWireWithNetsCommand` zastępuje `AddWireCommand` w `RouteTool` — atomowo commituje drut i towarzyszące mu mutacje schematu (create/extend/merge net). `AddWireCommand` pozostaje jako prostszy wariant (używany przy wczytywaniu projektu). Komendy mutujące schematic wymagają stanu implementującego `SchematicState(ComponentState)` z polem `schematic: Schematic`.

## 7. Persistence — struktura projektu (folder)

```
my_project/
├── project.yaml          # metadane, settings, raster, licencja na board
├── board.yaml            # definicja box-model płytki
├── schematic.yaml        # netlist (logical)
├── layout.yaml           # placements, wires, cuts (physical)
├── library/              # nadpisania/dodatki do globalnej biblioteki
│   └── custom_parts.yaml
└── exports/              # generowane (gitignored opcjonalnie)
    ├── netlist.net       # KiCAD export
    ├── bom.csv
    └── render.svg
```

`ProjectSerializer` ładuje folder atomowo, zapisuje przez tymczasowy katalog + atomowy `rename`.

## 8. KiCAD I/O

### 8.1. Import (`.net` w formacie KiCadXML lub S-expression)

```python
class KiCadNetlistImporter:
    def import_(self, path: Path) -> Schematic: ...
```

Mapowanie: KiCAD component (refdes, value, footprint) → `ComponentInstance` z dopasowaniem footprintu z naszej biblioteki (po nazwie + heurystyka, np. `Package_DIP:DIP-8_W7.62mm` → `DIP(8)`).

Konflikty raportowane do usera (dialog "nie znam footprintu X, dopasuj ręcznie").

### 8.2. Eksport

Format: `.net` zgodny z importem do KiCAD PCB editor. NIE generujemy `.kicad_sch` — to za duża inwestycja na fazę 1.

### 8.3. Własny parser vs `kiutils`

- `protoboard/kicad/parser.py` — własny minimalistyczny parser S-expression (MIT). Wspiera tylko podzbiór potrzebny do netlisty.
- `protoboard_kicad_gpl/` — osobny pakiet (GPL), opcjonalny, oparty o `kiutils`. Daje pełną kompatybilność z formatami KiCAD.

## 9. DRC — Design Rule Check

```python
class DRCEngine:
    def check(self, project: Project) -> list[DRCIssue]: ...

class DRCIssue:
    severity: Literal["error", "warning", "info"]
    code: str          # "MISSING_NET", "SHORT", "FLOATING_PIN", "OVERLAP"
    message: str
    locations: list[RasterPosition]
    affected_nets: list[UUID]
```

Sprawdzenia w MVP:
- `MISSING_NET`: sieć ze schematu nie jest w pełni zrealizowana fizycznie,
- `SHORT`: pady różnych sieci są elektrycznie połączone,
- `OVERLAP`: dwa komponenty zajmują ten sam pad,
- `FLOATING_PIN`: pin komponentu nie należy do żadnej sieci (warning),
- `BARE_WIRE_CROSS`: dwa nieizolowane przewody różnych sieci się krzyżują.

## 10. GUI — układ

```
┌──────────────────────────────────────────────────────────┐
│ Menu / Toolbar                                           │
├───────────┬────────────────────────────────┬─────────────┤
│           │                                │             │
│ Component │       Board View               │ Properties  │
│ Library   │       (QGraphicsView)          │ + DRC Panel │
│ (drzewo)  │       z warstwami:             │             │
│           │       - board                  │             │
│           │       - bottom wires           │             │
│           │       - components             │             │
│           │       - top wires              │             │
│           │       - overlay (DRC, hints)   │             │
│           │                                │             │
├───────────┴────────────────────────────────┴─────────────┤
│ Status bar: pozycja kursora, jednostki, tryb             │
└──────────────────────────────────────────────────────────┘
```

**Tryby (tools):** Select, Place Component, Route Wire, Cut Strip, Measure.

**Skróty klawiaturowe** w stylu KiCAD: `M` move, `R` rotate, `Del` delete, `Ctrl+Z/Y` undo/redo, `Esc` cancel.

## 11. Fazy implementacji

### Faza 0 — Bootstrap (1-2 dni)
- [x] Setup repo: `pyproject.toml`, `uv`, `ruff`, `black`, `mypy --strict`, `pytest`, pre-commit hooks.
- [x] Struktura katalogów (zob. §13).
- [x] CI: GitHub Actions (lint + test + mypy).
- [x] SPDX headers, `LICENSE` (MIT), `README.md` skeleton.
- [x] `CHANGELOG.md` (Keep a Changelog).

### Faza 1 — Domain Model + Box Layout (MVP rdzenia)
- [x] `units.py`, `geometry.py` (Point, Size, Insets, Rect w RasterUnit).
- [x] Box model: `Box`, `HBox`, `VBox`, `BoardGrid`, atomy (`PadAtom`, `StripAtom`, `GapAtom`).
- [x] Layout engine (intrinsic + layout pass) z testami pokrywającymi: HUG, FILL, FIXED, gap ułamkowy, gap ujemny.
- [x] `Board` jako wrapper drzewa boxów + cache pozycji atomów.
- [x] YAML serializer dla Board.
- [x] **Test deliverable:** perfboard 10×10 i stripboard 10×10 w YAML, pozycje atomów zweryfikowane.

### Faza 2 — Components + Schematic + Layout + Connectivity
- [x] `FootprintTemplate` z `SingleRow`, `DualRow`, `GridFootprint`. Aliasy `DIP`, `SIL`, `TO92`.
- [x] `ComponentDefinition`, `ComponentInstance`, biblioteka YAML.
- [x] Loader biblioteki (built-in + user override).
- [x] `Schematic` (Net, PinRef).
- [x] `Layout` (ComponentPlacement, WireSegment, StripCut).
- [x] `ConnectivityGraph` (networkx) — budowa z Board+Layout+Components.
- [x] DRC w wersji minimalnej: SHORT, MISSING_NET, OVERLAP.
- [x] CommandStack (generic, TDD) + PlaceComponent / MoveComponent / RemoveComponent.
- [x] **Test deliverable:** dzielnik napięcia (2×R, 3 nety) na perfboard, DRC=clean.

### Faza 3 — GUI MVP
- [x] `MainWindow`, layout paneli (library dock lewy, properties+DRC dock prawy).
- [x] `BoardView` z `QGraphicsScene`, render padów/drutów/komponentów na warstwach.
- [x] Snap do najbliższego atomu-pada i pinu komponentu.
- [x] Tools: Select (klik+drag+R+Del), Place (double-click library, R=rotate), Route Wire (klik-klik, Enter/Esc), Cut Strip.
- [x] Properties panel: edycja refdes/value/rotation, Apply.
- [x] Library panel z double-click.
- [x] Undo/Redo Ctrl+Z/Y.
- [x] Save/Load projektu (folder z YAML).
- [x] Zoom z centrowaniem na kursora (wheelEvent: `mapToScene` przed/po `scale` + `translate`).
- [x] Pan środkowym przyciskiem i Ctrl+drag; `NoAnchor` zapobiega resetowi pozycji przy `setSceneRect`.
- [x] Pad-conflict detection i resolution: `find_conflicts`, `ConflictKind`, `PadConflictDialog`, `shared_pins` na `ComponentPlacement`.
- [x] Body-overlap blocking: sprawdzenie bbox komponentów przed pad-conflict; blokada tylko gdy żaden pin się nie pokrywa.
- [x] Pin-na-drucie: `find_wire_conflicts` wykrywa gdy pin komponentu trafia na punkt `WireSegment`; auto-join lub auto-create net.
- [x] Auto-tworzenie netów przy rysowaniu drutu: `wire_net_ops`, `wire_net_resolver`, `AddWireWithNetsCommand` z pełnym undo/redo.
- [x] A* router: tie-breaker int w heap zapobiega porównywaniu `Point` bez `__lt__`.
- [x] **Test deliverable:** 505 testów (17 pad-conflict, 10 wire-net-ops, 8 wire-cmd, pozostałe domenowe i GUI).

### Faza 4 — Routing assistant (sugestie)
- [x] `RoutingCostModel` z konfiguracją per projekt.
- [x] A* na grafie siatki (4-connectivity, top layer).
- [x] Tryb "highlight": pierwszy klik w Route Wire → top-3 ścieżki jako dashed overlay (zielony/żółty/pomarańczowy).
- [x] Wsparcie izolowane/nieizolowane przewody.
- [x] **Test deliverable:** 13 testów A* routera (cost model, ścieżki, occupied pads).

### Faza 5 — KiCAD I/O
- [x] `KiCadNetlistImporter`: `.net` XML → `Schematic` (format `<export version="D">`).
- [x] Heurystyka mapowania footprintów (DIP-N, R_Axial, TO-92, PinHeader).
- [x] Nieznane footprinty: warning + import sieci bez geometrii.
- [x] `KiCadNetlistExporter`: `Schematic` → `.net` XML.
- [x] Roundtrip test: export → import zachowuje nazwy sieci i liczbę pinów.
- [x] Toolbar: Import KiCAD .net / Export KiCAD .net.

### Faza 6 — Pełne DRC + eksporty
- [x] DRC: FLOATING_PIN (warning), BARE_WIRE_CROSS (error).
- [x] Eksport SVG (top layer: pady, druty, komponenty z refdes).
- [x] BOM CSV (RefDes, Value, Footprint, Description — sortowane).
- [ ] Eksport PNG/PDF.
- [ ] Print-ready layout (1:1 do druku jako szablon do nawiercania).

### Faza 7 — Nice-to-have (backlog)
- Autorouter (cała netlista naraz, np. Lee algorithm z rip-up & retry).
- Breadboard, custom matrix boards, definicje user-defined.
- Cross-platform (Windows, macOS).
- 3D preview (przez VPython/three.js w QtWebEngine).
- Plugin API (Python plugins w `~/.config/protoboard-designer/plugins/`).
- Wsparcie dla Eagle/Fritzing netlist.

## 12. Testy — strategia

- **Unit:** wszystko w `domain/`, `services/` — bez Qt, szybkie.
- **Integration:** end-to-end na poziomie modelu (utwórz projekt programatycznie, zapisz, wczytaj, sprawdź DRC).
- **GUI:** `pytest-qt`, smoke testy (otwórz okno, kliknij toolbar, nie crashnij).
- **Property-based:** `hypothesis` dla layout engine (random box trees → invariants: pozycje atomów monotoniczne, brak nakładek poza intencjonalnymi gapami ujemnymi).
- **Snapshot:** rendery YAML projektów porównywane z baseline.
- **Pokrycie:** target 80% dla `domain/` i `services/`, GUI nie liczone.

## 13. Struktura katalogów

```
protoboard-designer/
├── pyproject.toml
├── README.md
├── LICENSE                          # MIT
├── CHANGELOG.md
├── CLAUDE.md                        # ten plik
├── docs/
│   ├── architecture.md
│   ├── box-model.md
│   ├── kicad-interop.md
│   └── routing.md
├── src/
│   └── protoboard/
│       ├── __init__.py
│       ├── units.py
│       ├── geometry.py
│       ├── domain/
│       │   ├── board/
│       │   │   ├── boxes.py         # Box, HBox, VBox, Grid, atomy
│       │   │   ├── layout_engine.py # intrinsic + layout pass
│       │   │   ├── pads.py          # PadLayers
│       │   │   └── board.py
│       │   ├── components/
│       │   │   ├── footprints.py    # SingleRow, DualRow, Grid + aliasy
│       │   │   ├── definition.py
│       │   │   └── instance.py
│       │   ├── schematic/
│       │   │   ├── net.py
│       │   │   └── schematic.py
│       │   ├── layout/
│       │   │   ├── placement.py
│       │   │   ├── wire.py
│       │   │   ├── cut.py
│       │   │   └── layout.py
│       │   ├── connectivity/
│       │   │   └── graph.py         # ConnectivityGraph
│       │   ├── project.py
│       │   └── commands/
│       │       ├── base.py          # Command, CommandStack
│       │       ├── component_cmds.py
│       │       ├── wire_cmds.py
│       │       └── ...
│       ├── services/
│       │   ├── drc/
│       │   │   ├── engine.py
│       │   │   └── checks.py
│       │   ├── routing/
│       │   │   ├── cost_model.py
│       │   │   └── astar.py
│       │   ├── persistence/
│       │   │   ├── serializer.py
│       │   │   ├── board_yaml.py
│       │   │   ├── schematic_yaml.py
│       │   │   └── layout_yaml.py
│       │   ├── library/
│       │   │   └── loader.py
│       │   └── kicad/
│       │       ├── parser.py        # własny S-expr parser, MIT
│       │       ├── importer.py
│       │       └── exporter.py
│       ├── gui/
│       │   ├── main_window.py
│       │   ├── board_view.py
│       │   ├── renderers/
│       │   │   ├── board_renderer.py
│       │   │   ├── component_renderer.py
│       │   │   └── wire_renderer.py
│       │   ├── panels/
│       │   │   ├── library_panel.py
│       │   │   ├── properties_panel.py
│       │   │   └── drc_panel.py
│       │   └── tools/
│       │       ├── select_tool.py
│       │       ├── place_tool.py
│       │       ├── route_tool.py
│       │       └── cut_tool.py
│       └── app.py                   # entry point
├── library/                         # built-in komponenty
│   ├── opamps.yaml
│   ├── transistors.yaml
│   ├── passive.yaml
│   └── connectors.yaml
├── boards/                          # built-in szablony płytek
│   ├── perfboard_30x20.yaml
│   ├── stripboard_30x20.yaml
│   └── breadboard_full.yaml
├── tests/
│   ├── unit/
│   │   ├── test_layout_engine.py
│   │   ├── test_connectivity.py
│   │   ├── test_drc.py
│   │   ├── test_routing.py
│   │   └── test_kicad_parser.py
│   ├── integration/
│   │   └── test_project_roundtrip.py
│   ├── gui/
│   │   └── test_main_window_smoke.py
│   └── fixtures/
│       └── kicad_samples/
└── packaging/
    └── protoboard_kicad_gpl/        # opcjonalny pakiet GPL
        ├── pyproject.toml
        ├── LICENSE                  # GPL-3.0
        └── src/protoboard_kicad_gpl/
```

## 14. Konwencje kodu

- **Python 3.11+**, type hints wszędzie, `mypy --strict` musi przechodzić.
- **Brak `Any`** poza dobrze udokumentowanymi miejscami (np. user metadata).
- **Dataclasses** preferowane nad zwykłymi klasami; `frozen=True` gdziekolwiek możliwe.
- **`@property`** dla obliczanych wartości; **brak** publicznych mutowalnych pól w modelu po Fazie 1 (wszystko przez Command).
- **Pure functions** preferowane w `domain/`; side-effecty tylko w `services/persistence` i `gui/`.
- **Brak importów Qt** w `domain/` i `services/` (egzekwowane testem na grafie importów).
- **Naming:**
  - `snake_case` dla funkcji/zmiennych, `PascalCase` dla klas, `SCREAMING_SNAKE` dla stałych.
  - `id` dla UUID, `name` dla string-identifier widzialnego dla usera, `ref` dla cross-reference w YAML.
- **Docstringi:** Google style. Każdy publiczny moduł i klasa opisane.
- **Errors:** custom exceptions w `protoboard.errors`, dziedziczą z `ProtoboardError`.
- **Logging:** `structlog`, level `INFO` domyślnie, `DEBUG` przez `--verbose`.
- **Commits:** Conventional Commits (`feat:`, `fix:`, `refactor:`, `test:`, `docs:`, `chore:`).
- **PRs:** każda faza = osobny PR, opisz w PR description checklisty z fazy.

## 15. Decyzje do potwierdzenia / otwarte kwestie

Te punkty zostawiam jako TODO do dyskusji w trakcie implementacji — Claude Code powinien o nie zapytać przed zmaterializowaniem:

1. **Numeracja pinów w obróconym komponencie**: pin 1 zawsze logicznie ten sam, ale fizyczna pozycja zmienia się z rotation. Czy `pin_position()` zwraca pozycję względną czy absolutną? Proponuję: względną w `Footprint`, absolutną liczy `Layout.pin_world_position(component_id, pin_number)`.
2. **Komponenty wystające poza płytkę**: dozwolone z warningiem czy zabronione twardo?
3. **Wiele kopii tego samego komponentu**: czy `ComponentInstance.id` to UUID (Tak, proponowane), czy `refdes` (np. "U1") jest kluczem? UUID lepsze (rename refdes nie łamie referencji).
4. **Render izolacji przewodu**: jak wizualnie odróżnić goły od izolowanego? Propozycja: izolowany ciągła kolorowa linia, goły szara z czarnymi end-capami.
5. **Stripboard cuts**: wizualnie jako "X" na padzie, czy jako przerwa między padami? Druga opcja czytelniejsza, pierwsza zgodna z konwencją drukowanych szablonów.
6. **Format `.net` KiCAD**: są warianty (legacy, new XML, S-expression). Decyzja w Fazie 5 po przejrzeniu, co eksportuje aktualna wersja KiCAD (8.x/9.x).

## 16. Rekomendacje dla Claude Code

- Pracuj fazami. Nie zaczynaj Fazy N+1 dopóki Faza N nie ma zielonego CI i ręcznego smoke-testu.
- Każda faza zaczyna się od zaktualizowania `CHANGELOG.md` (sekcja `[Unreleased]`) i kończy commitem `chore: complete phase N`.
- Przed implementacją złożonego modułu napisz najpierw failing test (TDD), zwłaszcza dla layout engine i routing.
- Gdy napotkasz decyzję projektową spoza tego dokumentu, **zatrzymaj się i zapytaj** zamiast zgadywać. Dotyczy to szczególnie §15.
- Aktualizuj ten plik (`CLAUDE.md`) gdy decyzje są podejmowane — to żywy dokument, nie tylko start.
- Każda nowa publiczna API musi mieć test i docstring zanim zostanie scommitowana.
