# Plan refactoringu drag-conflict pipeline

## Context

Sesja diagnostyczna 2026-05-31 wykryła serię bugów wokół drag-and-drop
komponentów:

1. **Foreign wire przyklejał się do pinu** dragowanego komponentu (fix
   `eae4cd4`).
2. **Pin na środkowym segmencie wire'a** nie był wykrywany jako konflikt
   (fix `e28346a`).
3. **Brak live overlay** dla pin-na-wire scenariusza (fix `1328a8e`).
4. **Wire na bottom-side ignorowany** przez detector (fix `ee489a7`).
5. **Same-net wire pod pinem** silently akceptowany mimo rule 6 strict
   (fix `c052dcb`).

Wszystkie te bugi to symptomy jednego głębszego problemu
architektonicznego:

- **`MoveComponentCommand.apply` mutuje wires GEOMETRYCZNIE** (przez
  pin_map) zamiast STRUKTURALNIE (przez PinKnot ref). Każdy callsite
  który ma wire-vertex na "starym" położeniu pinu jest brany za
  "własny" wire.
- **`find_conflicts` / `find_wire_conflicts` / `_wire_hit_via_pad_index`**
  to **trzy niezależne implementacje** logiki "co jest na padzie" —
  każda z własnym net resolution path, każda potencjalnie z bug
  zignorowania bottom-side / mid-segment / cross-net.
- **PadStrategy istnieje** (od refaktoru pad/PadLayer) ale **nie jest
  używana** w drag pipeline. Każda detection function ma własne
  zaszyte reguły, więc nie ma jednego miejsca by powiedzieć "rule 6
  strict: żaden wire na pinie".
- **`_update_drag_overlay` (live) i `classify_drag_commit` (release)**
  to **dwie niezależne kopie** tej samej logiki. Co naprawiamy w
  jednym musi być powielone w drugim — czego konsekwencją był 1328a8e
  (live overlay nie miał wire detection którą release miał).
- **Floating-state mutuje placements in-place** podczas live drag, a
  `_drag_idx` rebuilds PadIndex za każdym snap change — koszt ~3 ms ×
  N snap changes na drag, **plus** PadIndex widzi już-przesunięte
  pozycje pinów dragowanego komponentu, **plus** wire's net inheritance
  z `_wire_net_from_existing_pads` opiera się o pierwszy pin który już
  jest na pozycji nowej (= net pin wyssany jest do wire'a, scenariusz
  same-net w bugu #5).

Intended outcome: jedno źródło prawdy dla **"czy dragowany komponent ma
konflikt z czymkolwiek pod nim"**, używające **wstrzykiwanej
PadStrategy**, działające identycznie w live overlay i na release-time
commit, bez tymczasowych mutacji w `layout.placements`.

## Co jest dziś (do zachowania jako kontrakt)

- `DragSeverity = CLEAR | SOFT | HARD` + `DragCommitDecision` z
  polami (description, auto_remove_wire_ids, auto_net_actions,
  blocking_pad_positions, overlay_pad_positions).
- `MoveConflictResult` w pad_conflict.py + `MoveConflictSeverity =
  CLEAR | REMOVABLE | BLOCKED` używany przez overlay rendering.
- `DragConflictOverlay` (`drag_conflict_overlay.py`) — Qt scene items
  z dashed rect + crosses. **Nie ruszać** — to czysty rendering.
- Floating state UX: SOFT → żółta ramka + corner text, HARD → red
  blocking, Esc cancel, Enter commit (z optional auto-accept dla
  CLEAR).
- Commands: `MoveComponentCommand`, `RemoveWireCommand`,
  `ReplaceWireFragmentsCommand`, `MacroCommand` — pozostają jako
  primitives.

## Decyzje projektowe (do ustalenia przed implementacją)

1. **Strategy w live overlay vs release-time** — jeden mechanizm dla
   obu, czy zostawić odrębne? Plan zakłada **wspólny detector**
   produkujący `DragCommitDecision` którego live overlay i release
   pipeline są oboma konsumentami.
2. **Trial PadIndex podczas drag** — czy budować świeży PadIndex za
   każdym snap (obecna metoda, ~3ms × N), czy klonować
   `session.pad_index().clone()` raz na drag-start i **mutować
   incrementally** w trakcie? Klonowanie istnieje
   (`PadIndex.clone()`). Sugerowane: clone + incremental update.
3. **Rule 6 strict w PadStrategy** — `can_place_pin` odrzuca każdy
   wire (już wdrożone w `c052dcb`). Potwierdzenie że to ma być
   default dla DRC+Simplify+Autoroute, nie tylko per-tool knob.
4. **`MoveComponentCommand` mutuje wires?** — obecnie wbudowane
   `_update_wires_for_move` translatuje wires podczas apply. Refactor
   przeniesie translację do **osobnej commandy `TranslateAnchoredWiresCommand`**
   w macro, by Move było czyste (zmienia tylko placement).

## Step 0 — Architekturalny szkic (no-code)

**Cel**: 1-stronicowa diagrama nowego pipeline'a w
`docs/drag-pipeline-architecture.md`. Opisać:
- Aktorów: SelectTool, DragDetector (new), PadStrategy, CommandStack.
- Sekwencję dla live drag: on_press → on_move (×N) → on_release →
  Enter/Esc.
- Co mutuje stan kiedy (placements vs PadIndex clone vs wires).
- Gdzie strategia jest wstrzykiwana.

**Pliki dotykane**: `docs/drag-pipeline-architecture.md` (new).

## Step 1 — Wydzielić `DragDetector` (single source of truth)

**Cel**: jeden moduł produkujący `DragCommitDecision` dla danego
(layout snapshot, moved_comp_ids, strategy). Wykorzystuje
**PadStrategy** zamiast inline net logic.

**Nowy plik**: `src/protoboard/services/drag_detector.py`

```python
@dataclass(frozen=True)
class DragSnapshot:
    """Immutable view of what the layout looks like 'right now' for
    purposes of conflict detection. Built once per snap change."""
    pad_index: PadIndex                # already reflects current placements
    placements: dict[uuid.UUID, ComponentPlacement]
    schematic: Schematic
    instances: dict[uuid.UUID, ComponentInstance]
    components: dict[str, ComponentDefinition]

class DragDetector:
    def __init__(self, strategy: PadStrategy) -> None:
        self._strategy = strategy

    def classify(
        self,
        snapshot: DragSnapshot,
        moved_comp_ids: set[uuid.UUID],
        dragged_wire_ids: frozenset[uuid.UUID] = frozenset(),
    ) -> DragCommitDecision:
        """Single entry point. Returns the same DragCommitDecision that
        `classify_drag_commit` returned, but produced via PadStrategy
        delegations instead of inline rule code."""
        ...
```

Wewnętrznie `DragDetector` woła `strategy.can_place_pin(pad, our_net)`
per pin moved komponentu (rule 6 + 8 razem), `strategy.can_form_t_junction`
dla padów które dragowane wires będą formować (Section 2), etc.

**Pliki dotykane**:
- `src/protoboard/services/drag_detector.py` (new)
- `tests/unit/test_drag_detector.py` (new, ~10 testów per scenariusz:
  pin na własny wire, pin na foreign wire, pad-on-pad różne nety, body
  overlap, all clean, …)

**Verify**:
- `uv run pytest tests/unit/test_drag_detector.py` — green
- `uv run pytest` — bez regresji (DragDetector jest nieużywany do
  Step 4)

## Step 2 — Migrować `classify_drag_commit` na DragDetector

**Cel**: `classify_drag_commit` staje się thin wrapper który buduje
`DragSnapshot` z `session` i deleguje do `DragDetector`.

**Pliki dotykane**:
- `src/protoboard/gui/placement_conflict.py:426` — body
  `classify_drag_commit` skraca do ~20 linii. Usuwa się 6 dependencies
  (`find_wire_conflicts`, `find_wires_crossing_pads`,
  `find_skewed_wires`, własna pad_index build, point_to_net build,
  network resolution).
- Nieoznaczone funkcje pad_conflict.py (`find_wire_conflicts`,
  `_wire_hit_via_pad_index`) dostają deprecation comment — pozostają
  jako wrappers dla legacy callsites, do usunięcia w follow-up.

**Verify**:
- `uv run pytest tests/unit/test_pad_conflict.py tests/gui/test_drag_floating_ux.py` — green
- Pełen suite — 1653+ green
- Manual test: drag pin-na-foreign-wire → SOFT decision, drag
  pin-na-same-net-wire → SOFT (po rule 6 strict)

## Step 3 — `MoveComponentCommand` przestaje translatować wires

**Cel**: `MoveComponentCommand.apply` mutuje tylko `layout.placements`.
Translacja wires (przez PinKnot anchor) idzie do osobnej commandy
`TranslateAnchoredWiresCommand` która jest **zawsze** w macro razem z
Move. Skewed/foreign/orphan wire drop też w osobnych commandach
(`DropWiresCommand(wire_ids)`).

**Nowe komendy** w `src/protoboard/domain/commands/component_cmds.py`:

```python
@dataclass
class TranslateAnchoredWiresCommand(Command[ComponentState]):
    """Translate every wire anchored to (comp_id, pin_num) via PinKnot
    by delta. Foreign wires whose path merely passes through old pin
    positions are untouched."""
    comp_id: uuid.UUID
    old_placement: ComponentPlacement
    new_placement: ComponentPlacement
    _saved_paths: dict[uuid.UUID, list[Point]]  # for revert
    ...

@dataclass
class DropWiresCommand(Command[ComponentState]):
    """Remove every wire in wire_ids. Reusable from drag-commit (skewed,
    crossing, foreign), simplify, ad-hoc cleanup."""
    wire_ids: frozenset[uuid.UUID]
    _saved_wires: list[Wire]
    ...
```

`_update_wires_for_move` zostaje jako private helper dla
`TranslateAnchoredWiresCommand`. Weak-fallback dla orphan wires (bez
knot refs) zostaje — back-compat dla legacy testów.

**Floating commit pipeline** (`select_tool.py:_commit_floating`):
1. `RemoveWireCommand` × N (auto_remove z decision)
2. `TranslateAnchoredWiresCommand` × M (per moved comp_id)
3. `DropWiresCommand` × K (skewed po translacji)
4. `MoveComponentCommand` × P (placement only)

Wszystkie w jednym `MacroCommand`.

**Pliki dotykane**:
- `src/protoboard/domain/commands/component_cmds.py:113-159`
  (`MoveComponentCommand.apply` przestaje wołać
  `_update_wires_for_move`; ~30 LOC removed)
- `src/protoboard/domain/commands/component_cmds.py` — dodać 2 nowe
  klasy (~150 LOC)
- `src/protoboard/gui/tools/select_tool.py:853-946` (`_commit_floating`
  buduje nowe macro structure)
- `src/protoboard/gui/tools/select_tool.py:2685, 2721, 2755, 2811`
  (inne callsites `MoveComponentCommand` — jeśli również translatują
  wires, dostają TranslateAnchoredWiresCommand prepended)
- `tests/unit/test_move_component_foreign_wire.py` — assertions
  niezmienione, ale testy używają nowych commandów explicit
- Tests dla nowych commandów (`tests/unit/test_translate_anchored_wires.py`,
  `tests/unit/test_drop_wires_command.py`)

**Verify**:
- `uv run pytest tests/unit/test_macro_command.py tests/unit/test_move_component_foreign_wire.py` — green
- Pełen suite — 1653+ green
- Manual: drag SIL → foreign wire pod pinem zostaje, własne wires
  przesunięte z pinami; commit usuwa foreign wires per decision

## Step 4 — Live overlay konsumuje DragDetector

**Cel**: `_update_drag_overlay` w `select_tool.py` woła `DragDetector`
zamiast inline `find_conflicts` + `find_wire_conflicts`. Live overlay
i release-time pipeline używają **dokładnie tej samej** logiki.

**Pliki dotykane**:
- `src/protoboard/gui/tools/select_tool.py:2866-3023`
  (`_update_drag_overlay` skraca się o ~80 LOC; cała pętla
  find_conflicts/find_wire_conflicts → 1 call do `DragDetector.classify`)
- `src/protoboard/gui/tools/select_tool.py:2950-3110` — debounced
  `_run_full_drag_validation` też deleguje (i staje się trywialne)

**Verify**:
- Live test: drag SIL na wire → żółta ramka instant (nie po 1s
  timer)
- Drag pin na pin-foreign → red blocking
- Drag clean → no overlay
- Pełen suite

## Step 5 — Trial PadIndex via clone, no full rebuild per snap

**Cel**: zamiast budować świeży PadIndex za każdym snap (~3ms × N),
**sklonować** session pad_index raz na drag-start (już istnieje
`PadIndex.clone()`) i **incrementally aktualizować** w trakcie drag
(`remove_component(cid)` + `add_component(cid, new_placement)`).

**Implementacja**:

```python
# w SelectTool.__init__:
self._drag_pad_index: PadIndex | None = None

# w _begin_drag (linia ~1290):
self._drag_pad_index = self._session.pad_index().clone()
for cid in drag_ids:
    self._drag_pad_index.remove_component(cid)
    pl = self._session.layout.placements[cid]
    inst = self._session.instances[cid]
    defn = self._session.components[inst.definition_ref]
    self._drag_pad_index.add_component(cid, pl, defn, self._session.schematic)

# w on_move (po placement mutation):
for cid in drag_ids:
    self._drag_pad_index.remove_component(cid)
    self._drag_pad_index.add_component(cid, new_pl, defn, schematic)

# DragSnapshot bierze self._drag_pad_index (już aktualny)

# w _commit_floating / _cancel_floating:
self._drag_pad_index = None
```

**Korzyść**: PadIndex build (~3ms) tylko 1× na drag (clone +
N moved components), zamiast N× snap changes. Dla group drag (20
komponentów) drag przy 60 fps daje ~50ms/frame redukcji.

**Pliki dotykane**:
- `src/protoboard/gui/tools/select_tool.py` (~20 LOC dodanych dla
  state management `_drag_pad_index`)
- `src/protoboard/services/drag_detector.py` (DragSnapshot przyjmuje
  pad_index z zewnątrz, nie buduje sam)

**Verify**:
- `uv run pytest tests/benchmarks/test_perf_drag.py` (nowy, jeśli
  istnieje benchmark drag) — perf wins
- Manual: drag dużej grupy → płynny (brak laga)

## Step 6 — Usunąć duplikację `find_wire_conflicts` /
`_wire_hit_via_pad_index`

**Cel**: po Step 2 i Step 4, te funkcje są używane tylko przez
deprecated callsites. Usunąć lub przepisać na `DragDetector` delegacje.

**Pliki dotykane**:
- `src/protoboard/domain/layout/pad_conflict.py` —
  `find_wire_conflicts`, `_wire_hit_via_pad_index`, `find_conflicts`
  pozostają jako thin wrappers do DragDetector (back-compat dla testów)
  lub całkowicie usunięte (decyzja przed Step 6).
- Wszystkie callsites zaktualizowane.

**Verify**:
- Pełen suite green
- Manual smoke

## Step 7 — Logging cleanup

**Cel**: debug log dodany w sesji diagnostycznej (`drag.overlay.enter`,
`drag.overlay.update`, `drag.overlay.result`,
`drag.overlay.find_wire_conflicts`, `wire_hit.lookup`,
`drag.update_wires_for_move.*`) — niektóre były `_log.info`
tymczasowo. Przywrócić na `_log.debug` lub usunąć eksperymentalne.

**Pliki dotykane**:
- `src/protoboard/gui/tools/select_tool.py` — info → debug dla
  `drag.overlay.*`
- `src/protoboard/domain/layout/pad_conflict.py` — usunąć
  `wire_hit.lookup` (DragDetector zastąpi)
- `src/protoboard/domain/commands/component_cmds.py` — zostawić
  `drag.update_wires_for_move.*` (przeniesione do
  `TranslateAnchoredWiresCommand`)

## Verification end-to-end

Po każdym Step:
1. `uv run pytest --ignore=tests/benchmarks --ignore=tests/property -q` — green
2. Manual smoke per scenariusz:
   - Drag pin na foreign wire (top, bottom, plated_through)
   - Drag pin na same-net wire
   - Drag SIL wzdłuż wire (3 piny na 1 wire)
   - Drag SIL na free area (no conflict)
   - Drag grupy (multi-comp drag)
   - Drag pin na pin (pad-pad DIFFERENT_NETS → BLOCKED)
   - Esc anywhere → revert
   - Auto-accept CLEAR vs floating-state UX

Po wszystkich Step:
1. **Acceptance criteria z user obserwacji 2026-05-31**:
   - 3 piny na 1 wire → żółta ramka (live)
   - 2 piny na 1 wire → żółta ramka
   - 2 piny na 2 wire → żółta ramka
   - Pin na bottom wire (drag z top) → żółta ramka
   - Pin na same-net wire → żółta ramka (rule 6 strict)
   - Foreign wire NIE przykleja się do moved pin
2. **Benchmark autoroute** (regression < 5%)
3. **Cross-check tabela** w docs/pad-pin-wire-junction-semantics.md —
   rule 6, 8 w `_find_pin_at`, drag-conflict, classify_drag_commit:
   wszystkie `zgodny`.

## Critical files (lista zbiorcza)

Nowe:
- `src/protoboard/services/drag_detector.py`
- `docs/drag-pipeline-architecture.md`
- `tests/unit/test_drag_detector.py`
- `tests/unit/test_translate_anchored_wires.py`
- `tests/unit/test_drop_wires_command.py`

Modyfikowane (w kolejności Step):
- Step 0: docs
- Step 1: drag_detector.py + tests
- Step 2: `gui/placement_conflict.py`
- Step 3: `domain/commands/component_cmds.py`, `gui/tools/select_tool.py`
- Step 4: `gui/tools/select_tool.py`
- Step 5: `gui/tools/select_tool.py`, `services/drag_detector.py`
- Step 6: `domain/layout/pad_conflict.py`
- Step 7: cleanup loggingu

## Out of scope (follow-up)

- **`MoveComponentCommand` jako pure placement-only** dla wszystkich
  callsites (rotate, flip, group move) — Step 3 ogarnia drag-commit
  path, pozostałe migrowane w osobnym PR.
- **Kapton tape** w `DragDetector` — gdy kapton wejdzie, strategy
  config (`strict_tape_joints`) automatycznie wpływa na decision bez
  zmian w detectorze.
- **Wire-pin** (virtual goldpin) — gdy wejdzie, `can_place_pin`
  rozszerzy się na wire-pin też, bez zmian w drag pipeline.
- **Push-and-shove dla drag** (foreign wire się rozsuwa zamiast być
  usunięty) — pełen feature, nie refactor.
- **Worker-thread DRC z pad_index snapshot** — temat z poprzedniego
  refactoru, niezależny.
- **`MoveComponentCommand._update_wires_for_move` weak-fallback** dla
  orphan wires (bez knot refs) — Step 3 zachowuje to back-compat;
  usunąć gdy wszystkie wires będą miały knot refs (po pełnym
  migracji v2 wire model).
