# Bug: shorty po drag — utrata połączeń + propozycja unified drag model

## Streszczenie

Po drag pojedynczego komponentu, multi-selection lub grupy, **wires
łączące moved elementy ze stacjonarnymi** są:

- Albo **dropowane jako skewed** (gdy `_update_wires_for_move`
  translateuje jeden endpoint, drugi zostaje na miejscu i wire
  traci ortho/diagonal shape)
- Albo **translatowane "rozciągliwie"** zachowując ortho (wire
  ortho `(2.5, 5.5) → (3.5, 5.5)` po drag rezystora w prawo o 1
  raster staje się `(2.5, 5.5) → (4.5, 5.5)` — ortho zachowany)

Pierwszy przypadek powoduje **silent data loss** + kaskadowe SHORTy
gdy pady tracą połączenie z netami (zaobserwowane na
`reference_big_project`: 8 SHORTów po drag grupy).

Drugi przypadek jest **niespójny semantycznie**: dlaczego ortho
wire "rozciąga się" a diagonal się zrywa? Fizycznie wire ma
zlutowane dwa endpointy — drag jednego pinu **musi** zerwać
połączenie (lutowane druty się nie rozciągają).

## Status: ZAIMPLEMENTOWANE (2026-06-02)

Pełen refactor unified drag model dostarczony w commitach
`c67fa11` (fundament) + `a3c2f25` (SelectTool orchestrator) + follow-ups
(live drag rendering, preferences UI, lock_internal_wires field).

Zaimplementowane Steps z planu:
* **Step 0**: `domain/layout/drag_classification.py` — pure 5-way partition
  (internal / cross_group_disconnect / half_orphan_internal /
  half_orphan_external / external).
* **Step 1**: `gui/drag_preferences.py` + `AppSettings.drag_*` YAML
  round-trip.
* **Step 2**: `gui/dialogs/drag_disconnect_dialog.py` (Reroute/Drop/
  Cancel + opt-out) + `drag_ask_remove_dialog.py` (Yes/Cancel + opt-out).
* **Step 3**: `services/drag_detector.py` emits `DragWireClassification`
  + free/sgroup/cross-disconnect drop buckets + `hard_locked_wire_pads`.
* **Step 4**: `domain/commands/drag_cmds.py::MoveDragSetCommand` —
  atomic delta translation; no skewed-drop heuristic.
* **Step 5**: `gui/tools/select_tool.py::_commit_floating_unified` —
  orchestrator routes through MoveDragSetCommand with preference
  resolution + post-commit autoroute scheduling.
* **Step 6**: `gui/board_view.py::set_wire_drag_state` — opacity
  ghosting (drop=0.3, disconnect=0.5).
* **Step 7**: PreferencesDialog "Drag" tab with 3 combo-boxes + lock
  default checkbox.
* **Step 8**: `ComponentGroup.lock_internal_wires: bool` field —
  promotes soft conflicts to HARD when foreign drags touch internal wires.

Legacy `MoveComponentCommand(placement_only=False)` +
`TranslateAnchoredWiresCommand` + `_update_wires_for_move` pozostają
jako deprecated shim dla back-compat z command recordings.

Mniejsze fixy podrzędne zacommitowane 2026-06-02 wcześniej
(`cf40f8a`, `bc97a49`, `4edcaf2`, `2737f15`, `e136d84`) złagodziły
symptomy; pełen refactor naprawia root cause.

## Reprodukcja

1. Otwórz `tests/fixtures/reference_big_project`
2. Run DRC — 0 błędów
3. Press na pin komponentu grupy `37cf8735` (RD1) — np. D3
4. Przeciągnij grupę o 1 raster → release
5. Run DRC — 8 SHORT issues

Konkretny przykład wire'a który ginie:

```
Wire id=ecc99743 (net 12V), insulated, side=bottom
  Path: (18.5, 58.5) → (17.5, 57.5) → (16.5, 56.5) → (15.5, 55.5) → (14.5, 54.5)
  Endpoint a (PinKnot): SW6/3 at (18.5, 58.5) — w grupie 37cf8735
  Endpoint b (PinKnot): R30/2 at (14.5, 54.5) — w innej grupie
```

Diagonal manual-routed wire łączący SW6 (12V, grupa A) i R30 (12V,
grupa B). Drag grupy A o (+1, 0): endpoint A skacze na (19.5, 58.5),
endpoint B stoi. Pierwszy segment `(19.5, 58.5) → (17.5, 57.5)` ma
dx=-2, dy=-1 → skewed. `_update_wires_for_move` dropuje wire.

SW6/3 traci połączenie z 12V. Sąsiednie wires GND/VCC mające
vertex na (19.5, 58.5) przez plated-through fusione 12V z innym
netem → SHORT.

## Czy dotyczy pojedynczego komponentu?

**Tak**, mechanizm jest identyczny. Pojedynczy komponent = degenerate
case grupy z 1 członkiem.

- **Ortho wires po dragu komponentu**: zostają translateowane,
  zachowują ortho — wygląda "OK", ale jest to **niespójność
  semantyczna** (drut fizycznie się nie rozciąga).
- **Diagonal/multi-bend wires**: dropowane jako skewed.

## Model do naprawy: unified drag (komponent ≡ multi-selection ≡ grupa)

### Abstrakcja

**Wszystko co dragowane = "drag set"** — encja z:
- **Members**: zbiór komponentów (1 dla pojedynczego komponentu,
  N dla multi-selection, N dla grupy)
- **Pins**: zbiór padów wszystkich members (private + exported dla
  grupy; wszystkie public dla pojedynczego komponentu i
  multi-selection)
- **Bodies**: bbox każdego komponentu
- **Internal wires**: wires których **oba endpointy są na pinach
  members** drag setu. Te wires podążają z dragiem **jako blok**
  (translacja path = translacja delta).
- **External wires**: wszystko inne (dotyka pinów members ale
  drugi endpoint poza — np. JointKnot, pin innego komponentu).
  Te wires **nie mogą podążyć z dragiem** — drag = zerwanie.

### Klasyfikacja wire względem aktualnego dragu

Wire jest **drag-internal** iff:

```python
# Oba endpointy to PinKnoty komponentów w drag set:
isinstance(knots[wire.a.id], PinKnot)
  and isinstance(knots[wire.b.id], PinKnot)
  and knots[wire.a.id].pin.component_id in drag_set_members
  and knots[wire.b.id].pin.component_id in drag_set_members
```

Wire `find_owned_wires` (statyczna klasyfikacja "wire należy
do grupy") jest **niezależna** od tej klasyfikacji.  Na przykład
half-orphan wire owned grupy (jeden PinKnot grupy, drugi
JointKnot na pinie/padzie internalnej sieci) jest:

- **statycznie owned by group** — wchodzi do outline grupy, jest
  locked przed manual delete
- **dynamicznie external dla dragu** — JointKnot endpoint nie
  podąża z dragiem (joint to luźny węzeł, nie pin)

W praktyce drag używa **drag-internal** classification.  Static
ownership decyduje o outline/lock/render.

### Conflict mask

Każdy drag set + każdy stacjonarny element produkuje **maskę**
zajętych padów po klasach:

- **Pin mask**: pady gdzie są piny komponentów
- **Body mask**: pady wewnątrz bbox komponentów (poza pinami)
- **Wire mask**: pady gdzie są wire vertices (endpoint/middle —
  rozróżnione dla insulated vs bare)

**Hard collision** = `(moved.pins ∪ moved.bodies) ∩ (stationary.pins ∪ stationary.bodies) ≠ ∅`

Konkretnie: pin-on-pin (different nets), pin-on-body, body-on-pin,
body-on-body.  **Blokuje commit dragu** (drag stays in floating
state, user musi anulować lub przesunąć inaczej).

**Soft collision** = każda inna kolizja zawierająca wire:
- `moved.pins ∩ stationary.wires` (pin moved trafia na wire
  stacjonarny) → drop tego wire
- `moved.wires ∩ stationary.pins` (wire moved trafia na pin
  stacjonarny) → drop tego wire (moved wire)
- `moved.wires ∩ stationary.wires` (wire moved krzyżuje wire
  stacjonarny) → drop jednego (kontekst rozstrzyga)

**Wires nie wchodzą w hard collision check** — domyślnie.

### Opt-in hard mask dla stacjonarnej grupy

Każda grupa ma flag `lock_internal_wires` (domyślnie False).  Gdy
True, **internal wires grupy** wchodzą do jej **hard collision
mask** — moved pin lądujący na internal wire **takiej zabezpieczonej
grupy** = HARD conflict (blok), nie SOFT (auto-remove).

Semantyka: "moja grupa jest immutable — nikt nie powinien przez
nią przejść swoim pinem nawet jeśli to spowodowałoby auto-remove".

### Soft conflict resolution — preference-driven

Zachowanie sterowane przez `app_settings` per kategoria konfliktu.
Każda kategoria ma osobne preference field z jedną z wartości:

- `auto-remove` — usuń bez pytania przy commit
- `ask-remove` — dialog "Remove wire(s)? [Yes / Cancel]"
- `auto-reroute` — usuń + odpal autoroute na affected nets
- `ask-reroute` — dialog "Reroute / Drop / Cancel"
- `block` — traktuj jako HARD (drag floating, nie commit)

### Kategorie soft conflictów

1. **`drag.free_wire_conflict`** — moved pin lub moved wire trafia
   na free wire (wire nieowned by żadną grupą).
   - Default: `auto-remove`
   - Free wires są "luźne" — typowy autoroute output albo manual
     scratch wires.  Bezpieczne usunąć bez pytania.

2. **`drag.stationary_group_wire_conflict`** — moved pin lub
   moved wire trafia na wire owned by stationary group (część
   innej grupy która nie jest dragowana).
   - Default: `ask-remove`
   - Grupy są immutable z zewnątrz — modyfikacja wymaga
     consentu.  Conservative default.
   - Opt-in escalation: grupa z flag `lock_internal_wires`
     promuje conflicts wires na hard (blok drag) niezależnie
     od preference.

3. **`drag.cross_group_disconnect`** — wire łączący moved
   element ze stacjonarnym (jeden endpoint w drag set, drugi
   poza).  Drop zerwie sieć łączącą oba; reroute może to
   naprawić.
   - Default: `ask-reroute`
   - Najbardziej kosztowny przypadek (utrata connectivity).
     Default oferuje user'owi wybór between Auto-reroute,
     Drop, Cancel.
   - Power-userzy mogą przełączyć na `auto-reroute` (silent
     reroute przy commit) gdy ufają autoroute'owi.

### Preferences UI

W `Preferences → Drag` (nowa sekcja):

```
Drag conflict resolution
─────────────────────────────────────────────────────
Free wire on path:        [ auto-remove        v ]
Stationary group wire:    [ ask-remove         v ]
Cross-group disconnect:   [ ask-reroute        v ]

[ ] Lock internal wires by default for new groups
```

Każde pole dropdown z opcjami opisanymi wyżej.  Settings persisted
w `app_settings.yaml`.

### Per-session opt-out

Każdy dialog ma checkbox **"Don't ask again for this session"**.
Wybrana akcja staje się session-level override dla tego typu
konfliktu, niezależnie od preference.  Reset przy restart.

Power-userzy którzy wiedzą że ich layout ma dużo cross-group
wires mogą jednorazowo wybrać "Auto-reroute" + checkbox, i
przez resztę sesji drag działa bez przerywań.

### Multi-selection = ad-hoc temporary group

Drag multi-selection N komponentów jest **implementacyjnie**:

1. Stwórz tymczasową `ComponentGroup` z `component_ids = {N
   wybranych}`.  Nie persystowana, nie zapisana w `layout.groups`.
2. Uruchom drag flow grupy z tą tymczasową grupą jako drag set.
3. Po commit, temp group rozpada się.  Members ich pozycje
   trzymają, internal wires translatowane atomic, external wires
   handlowane przez soft conflict resolution.

Pojedynczy komponent jest **degenerate** case: temp group z 1
członkiem, 0 internal wires (nie ma "wire wewnątrz" pojedynczego
komponentu — wszystkie wires touching jego pin to external).

**Zaleta implementacyjna**: jeden code path dla wszystkiego.
`MoveComponentCommand` + `_update_wires_for_move` per-component
znika, zastąpione **`MoveDragSetCommand`** atomic dla całego
drag setu.

### MoveDragSetCommand

Single atomic command zastępujący macro per-component:

```python
@dataclass
class MoveDragSetCommand(Command[ComponentState]):
    """Atomic move of a drag set (single comp, multi-sel, or group).
    
    Carries:
    - members: dict[comp_id, (old_pl, new_pl)]
    - drag_set_pins: frozenset[(comp_id, pin_num)]
    - internal_wire_ids: frozenset[wire_id] (drag-internal classification)
    - external_wires_to_drop: frozenset[wire_id] (already decided by
      pre-commit soft resolution)
    
    Apply algorithm (single pass):
    1. For each comp_id, set new placement.
    2. For each internal wire (in internal_wire_ids), translate
       wire.path by the drag's delta (one vector for all).
    3. For each external wire in external_wires_to_drop, remove.
    4. Done. No per-component skewed check.  No drop_skewed.
    """
```

**Plus**: jeden code path, atomic, no intermediate skewed state.
**Plus**: usuwa `_update_wires_for_move`, drop_skewed,
`TranslateAnchoredWiresCommand` jako osobne mechanizmy.

### Conflict check architecture

`DragDetector` (lub jego refactor) buduje **dwie maski**:

- `moved_mask`: po projection na nowe pozycje
- `stationary_mask`: po obecnym stanie (excluding moved)

Z masek wynika:

- `hard_collision_pads = (moved.pin ∪ moved.body) ∩ (stationary.pin ∪ stationary.body)`
- `soft_collision_wires = (moved.pin ∩ stationary.wire) ∪ (moved.wire ∩ stationary.pin) ∪ (moved.wire ∩ stationary.wire)`
- `cross_group_disconnect_wires = wires which become external when drag set moves` (jeden endpoint w moved set, drugi poza)

Cross-group disconnect to **specjalny case soft**, wymagający
dialogu (decyzja Auto-reroute / Drop / Cancel).

### Live drag semantics

Live drag musi pokazać prawdziwy effekt dragu:

- **Internal wires podążają** — visualnie wire'y idą z dragiem
- **External wires zostają w starych pozycjach** — nie translateuj
  ich w live drag; pokaż je z **przekreśleniem / ghosting** jako
  "to zostanie usunięte przy commit"
- **Cross-group disconnect**: jeszcze nie wiemy decyzji user'a
  (dialog dopiero przy release).  Live drag pokazuje wire
  w starej pozycji + visual indicator "this connection will be
  broken or rerouted".

## Plan implementacji

### Step 1: drag-internal classification

Nowa funkcja w `protoboard/domain/layout/drag_classification.py`:

```python
def classify_wires_for_drag(
    wires: list[Wire],
    drag_set_members: frozenset[uuid.UUID],
    knots: dict[uuid.UUID, Knot],
) -> tuple[set[uuid.UUID], set[uuid.UUID], set[uuid.UUID]]:
    """Return (internal, external_drop, external_cross_group).
    
    - internal: both PinKnots in drag_set_members → travel with drag.
    - external_drop: not touching drag set OR not anchored to it
      → unaffected.
    - external_cross_group: at least one PinKnot in drag_set_members
      but the other endpoint is outside → will need user decision
      at commit time.
    """
```

Testy: unit tests dla typowych przypadków.

### Step 2: MoveDragSetCommand

Replace `MoveComponentCommand` (placement_only=False ścieżka) +
`TranslateAnchoredWiresCommand` + `_update_wires_for_move` jednym
atomic command.

Migration: stare commands zostają jako shimy dla backward
compatibility w testach + replay.

### Step 3: dual mask conflict check

`DragDetector` produkuje obie maski, klasyfikuje collision.
Hard → block drag.  Soft (zwykły) → auto-remove.  Soft
(cross-group) → dialog.

### Step 4: live drag rendering

Visual: internal wires podążają, external wires (do drop) w
ghosting/strikethrough.

### Step 5: preferences + dialog UX

Pola w `app_settings.yaml`:

```yaml
drag:
  free_wire_conflict: auto-remove
  stationary_group_wire_conflict: ask-remove
  cross_group_disconnect: ask-reroute
  default_lock_internal_wires: false
```

`PreferencesDialog` dostaje nową stronę "Drag" z dropdown per pole.

Dialogi:
- `ask-remove`: "Remove N wire(s)? [Yes / Cancel]" + per-session
  checkbox "Don't ask again for this session"
- `ask-reroute`: "Drag will disconnect N wires.  [Auto-reroute /
  Drop / Cancel]" + per-session checkbox

Implementacja autoroute trigger: po commit dragu z choice
`reroute`, dispatch `_autoroute(net_ids=affected_net_ids)`.

### Step 6: opt-in hard internal wires dla grupy

Pole `ComponentGroup.lock_internal_wires: bool = False`.  Gdy
True, internal wires grupy wchodzą do jej hard mask.  GUI
Properties Panel dla grupy z checkbox-em.

## Migracja istniejących projektów

`reference_big_project` ma już 8 wires "do dropowania" przy
typowym dragu.  Po refactor:

1. Pierwsze opening projektu: bez zmian, layout zachowany.
2. Pierwsza drag operacja: dialog "8 wires disconnect at commit
   — Auto-reroute, Drop, Cancel".
3. User wybiera Cancel — projekt pozostaje, user rerouteuje wires
   ręcznie aby były internal albo akceptuje że są cross-group.

Nie wymaga jednokrotnej migracji — refactor jest backward-compatible
dla existing layouts.

## Powiązane fixy zacommitowane

- `cf40f8a` fix(drag-detector): skip own-group wires in pin-on-wire
- `bc97a49` fix(group-drag): clicked component as primary anchor
- `4edcaf2` fix(autoroute): insulated wires block only endpoints
- `2737f15` fix(drc): SHORT location uses visible pin
- `e136d84` fix(persistence): graceful load when canonicalisation fails

Te fixy łagodzą **symptomy** ale nie naprawiają **root cause**
(`_update_wires_for_move` drop_skewed).  Pełen refactor opisany
wyżej zastępuje cały drag-pipeline.

## Pliki kodu

- `src/protoboard/domain/commands/component_cmds.py:1684`
  `_update_wires_for_move` — funkcja do zastąpienia
- `src/protoboard/domain/commands/component_cmds.py:1866`
  `wire_has_skewed_segment` drop — usunąć
- `src/protoboard/gui/tools/select_tool.py:1010-1040`
  `_commit_floating` — refactor do MoveDragSetCommand
- `src/protoboard/services/drag_detector.py` — dual mask
  conflict check
- `src/protoboard/domain/layout/group_ownership.py` — pozostaje
  dla static classification (outline, lock); drag classification
  to nowy moduł

## Test reprodukcyjny

```python
def test_cross_group_disconnect_pref_ask_reroute_opens_dialog():
    # 2 groups: G1=[A,B], G2=[C]
    # Manual diagonal wire from A.pin → C.pin (insulated)
    # app_settings.drag.cross_group_disconnect = "ask-reroute"
    # Drag G1 by (+1, 0)
    # Expected: dialog appears with [Auto-reroute / Drop / Cancel]
    ...

def test_cross_group_disconnect_pref_auto_reroute_silent():
    # Same layout
    # app_settings.drag.cross_group_disconnect = "auto-reroute"
    # Drag G1
    # Expected: no dialog; wire removed; autoroute dispatched
    #           on the affected net post-commit
    ...

def test_free_wire_pref_auto_remove_silent_drop():
    # Single component R1, free wire R1.pin1 → R2.pin1
    # app_settings.drag.free_wire_conflict = "auto-remove" (default)
    # Drag R1
    # Expected: no dialog, wire removed, R1 moved
    ...

def test_stationary_group_wire_pref_ask_remove_opens_dialog():
    # Drag R1 (freestanding) so its pin lands on a wire owned by
    # group G (stationary).
    # app_settings.drag.stationary_group_wire_conflict = "ask-remove"
    # Expected: dialog "Remove wire from group G? [Yes / Cancel]"
    ...

def test_stationary_group_lock_internal_promotes_to_hard():
    # G has lock_internal_wires=True
    # Drag R1 onto G's internal wire pad
    # Expected: HARD conflict regardless of preference;
    #          drag blocked (floating state, cannot commit)
    ...

def test_session_opt_out_skips_subsequent_dialog():
    # First drag: dialog appears, user picks "Drop" + checks
    # "Don't ask again for this session"
    # Second drag with same conflict type: no dialog;
    # session override applies "Drop" choice silently
    ...

def test_internal_wire_loopback_travels_with_single_component():
    # 1 component R1 with a wire R1.pin1 → R1.pin2 (loopback)
    # Drag R1 by (+1, 0)
    # Expected: wire path translated by (+1, 0); no drop
    #           (both endpoints in drag set → internal)
    ...
```
