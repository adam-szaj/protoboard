# Phase 3 — multi-group / mixed-drag

Date: 2026-06-08
Status: planned, decisions confirmed 2026-06-08
Prerequisite: Phase 2c (commit 5e514ef) on main.

## Decyzje użytkownika (2026-06-08)

1. ✅ **Hit("group", gid)** jako nowy kind w dispatcher
2. ✅ **Long-press migrujemy** — nie ma już single-group select przez long-press; Ctrl+klik na bbox grupy = ADD, plain LMB na bbox grupy = SET single-group
3. ✅ **Rubber-band selektuje grupy** — LTR "all members inside", RTL "any member inside"
4. ✅ **Mixed conflict: SOFT** — komponent free vs komponent grupy w drag-set: SOFT (wszystko to "moje rzeczy")
5. ✅ **Wariant A z pre-validation** (MacroCommand + multi-group conflict-check w SelectTool przed budową macro; sub-commandy z `skip_conflict_check=True`)

## Insight architektoniczny (user 2026-06-08)

> Multi/mixed drag nie różni się mocno od group drag. Różnica: wires nie muszą być
> zaznaczone, ale jeśli są zaznaczone, semantykę już mamy.

To znaczy: cały drag pipeline (snapshot orig positions, conflict-check, MacroCommand
z N×MoveGroup + M×MoveComponent + K×ReplaceWireFragments, HARD/SOFT floating UX,
atomic rollback) — **wszystko już istnieje dla single-group dragu**. Phase 3 to:

1. Skala (set zamiast singular `_selected_group_id`)
2. **Pre-validation widzi cały drag-set jako jedność** (multi-group aware conflict-check)
3. Hit("group", ...) routing przez dispatcher

Czyli **nie budujemy nowej domain command** — używamy macro istniejących, z jednym
nowym `check_multi_group_move_conflicts` przed commitem.

## Cel

Pozwolić użytkownikowi:
1. **Zaznaczyć kilka grup naraz** (Ctrl+klik na bbox każdej z nich, lub rubber-band obejmujący kilka bbox-ów grup).
2. **Zaznaczyć mix** top-level komponentów + jednej lub więcej grup.
3. **Drag wszystkich zaznaczonych elementów** jako jednego ruchu.

Dziś działa tylko **jedna grupa** (long-press) **albo** **N top-level komponentów**, nigdy mix.

## Status quo — co jest

| Element | Stan |
|---|---|
| `_selected_group_id: uuid.UUID \| None` | singular |
| Long-press na bbox grupy 500ms → select / 1000ms → enter | działa, ale tylko jednej grupy |
| Drag-set filtruje "lock'd-in-group" (`_drag_filtered_ids`) | komponent z grupy z `_is_locked_in_group()=True` jest pomijany w drag |
| `check_group_move_conflicts(moved_ids, active_group_id, ...)` | przyjmuje **jeden** `active_group_id` |
| `_build_group_drag_commands` | operuje na `effective_group_id = self._selected_group_id \| _common_group_for(drag_ids)` |
| `_snapshot_owned_wire_paths` | bierze jedno `group_id` + members |
| Rubber-band | selektuje **komponenty** + **wires**, **nie grupy** |
| Status bar `g:N` | dziś 0 lub 1; komentarz w `selection_summary()` mówi że gdy multi-group lands, to `len(self._selected_group_ids)` |

## Kluczowe pliki

| Plik | Zmiana |
|---|---|
| `src/protoboard/gui/tools/select_tool.py` | `_selected_group_id` → `_selected_group_ids: set[UUID]`; drag-set logic; selection summary; long-press + Ctrl+klik na bbox group; `_select_group`; rubber-band finish |
| `src/protoboard/domain/layout/pad_conflict.py` | `check_group_move_conflicts` przyjmuje `active_group_ids: set[UUID]` (lub osobna nowa funkcja `check_multi_group_move_conflicts`) |
| `src/protoboard/domain/commands/group_cmds.py` (?) | MoveGroup już istnieje per grupa; macro MoveMixedDragSet musi wrappować N MoveGroup + N MoveComponent atomowo (lub jeden nowy `MoveDragSetCommand` extension) |
| `tests/gui/test_select_tool.py` | TestMultiGroupSelection + TestMixedDrag |
| `tests/unit/test_pad_conflict.py` | multi-group conflict scenarios |
| `docs/multi-group-mixed-drag-plan.md` | TEN PLIK |

## Architektura zmiany

### Nowe pojęcie: **DragSet**

Drag-set to logiczny zbiór elementów do przesunięcia. Phase 3 rozszerza go z "płaska lista uuid" na strukturę:

```python
@dataclass(frozen=True)
class DragSet:
    free_components: frozenset[uuid.UUID]    # top-level, nie w grupie
    groups: frozenset[uuid.UUID]              # grupy do przeniesienia jako monolity
    wires: frozenset[uuid.UUID]               # wires w drag (z `_selected_wire_ids`)

    @property
    def all_component_ids(self) -> frozenset[uuid.UUID]:
        """Wszystkie komponenty (free + members wszystkich grup, rekursywnie)."""
        # uses all_component_ids(groups_map, gid) for each group
```

To jest **pure-Python** dataclass, można testować bez Qt.

Drag pipeline:
1. **press-time**: zbuduj `DragSet` z `_selected_*` + `_selected_group_ids`
2. **move-time**: snapshot orig positions / wire paths z `DragSet.all_component_ids`
3. **release-time**: zbuduj macro command — albo N MoveGroup + N MoveComponent atomically, albo MoveDragSetCommand (nowy)
4. **conflict-check**: `check_multi_group_move_conflicts(drag_set, ...)` rozpoznaje że komponenty grupy A vs grupy B to nie wewnętrzny konflikt

### Nowe pole `_selected_group_ids: set[UUID]`

Zastępuje `_selected_group_id`. Kompatybilność:
- `_select_group(gid)` → `clear() + add(gid)` (legacy single-select)
- `_select_groups_add(gid)` → `add(gid)` (multi)
- selection summary: `g:{len(_selected_group_ids)}`

## Fazy implementacji

### Etap 1 — DragSet dataclass + unit tests (~80 LoC)

Nowy moduł `src/protoboard/gui/drag_set.py` (pure-Python, no Qt):
- `@dataclass(frozen=True)` z trzema frozenset polami
- `all_component_ids` property — union free + recursive group members
- helper `from_selection(free_comps, group_ids, wires, groups_map)` factory

Testy: `tests/unit/test_drag_set.py` — 8-10 testów asercji właściwości
(union, idempotencja factory, pusty drag set, nested groups).

### Etap 2 — `_selected_group_id` → `_selected_group_ids: set` (~100 LoC churn)

Pole + wszystkie 7 callsite migrate.  
Compat shim: `_selected_group_id` jako `@property` zwracające
`next(iter(self._selected_group_ids), None)` żeby drag pipeline (etap 3)
nie psuło się od razu. Po Etap 5 shim removed.

`selection_summary()` przekonwertowany: `g:{len(self._selected_group_ids)}`.

`_select_group(gid)` zostaje jako legacy entry — clear + add to set.
W Etap 5 dispatcher pokrywa te use case przez `Hit("group", gid)` SET.

### Etap 3 — drag-set construction w on_press (~120 LoC)

Trzy ścieżki on_press LMB drag-arm (pin, wire, bbox) zamiast budować
płaską `drag_ids` budują `DragSet` przez `DragSet.from_selection(...)`.
`_drag_orig_positions` snapshot iteruje `drag_set.all_component_ids`.

`_is_locked_in_group` filter w drag-set:
- komponent grupy `gid` jest "locked" tylko gdy `gid not in drag_set.groups`
- jeśli grupa jest **częścią dragu**, jej członki są częścią dragu

### Etap 4 — multi-group conflict-check + pre-validation (~150 LoC)

**Nowa funkcja** `pad_conflict.check_multi_group_move_conflicts`:
- bierze `drag_set: DragSet` zamiast `(moved_ids, active_group_id)`
- traktuje wszystkie elementy w `drag_set` jako jeden ruchowy zbiór
- używa `PadIndex.clone()` z **wszystkimi** symulowanymi ruchami upfront

Logika konfliktów (decyzja 4: SOFT dla mixed):
- konflikt pad-pad wewnątrz **jednej** grupy w drag-set → SOFT
- konflikt pad-pad między różnymi elementami **drag-set** (free vs free,
  free vs grupa, grupa vs grupa) → **SOFT** (zatwierdzone)
- konflikt pad-pad między elementem drag-set a komponentem **poza** drag-set → HARD
- wire conflict: zachowuje obecną semantykę (ancestor wires → SOFT removable,
  subgroup wires → HARD/SOFT per existing rules)

Stara `check_group_move_conflicts` (single-group) zostaje jako shim
wywołujący `check_multi_group_move_conflicts(DragSet(groups={gid}, ...))`.

**Migration `_build_group_drag_commands`** → nowa `_build_drag_set_commands`
która:
1. pre-validation: wywołuje `check_multi_group_move_conflicts` z post-move clone
2. jeśli HARD → enter HARD floating (jak dziś)
3. jeśli SOFT → enter SOFT floating
4. jeśli CLEAR → buduj MacroCommand:
   - `MoveGroupCommand(gid, delta)` dla każdej `gid in drag_set.groups`
   - `MoveComponentCommand(cid, new_pos)` dla każdej `cid in drag_set.free_components`
   - `ReplaceWireFragmentsCommand(wid, new_path)` dla każdej `wid in drag_set.wires`
   - wszystkie z `skip_conflict_check=True` bo my już zwalidowaliśmy
5. `session.command_stack.execute(MacroCommand(sub_commands))` — atomic
   rollback z `[[group-origin-invariant]]` mamy w gratisie

### Etap 5 — Hit("group") routing + migracja long-press (~120 LoC)

#### `Hit("group", gid)` jako nowy kind (decyzja 1)

W `selection_fsm.py`:
```python
HitKind = Literal["component", "wire", "group"]
```

`Hit.__post_init__` już validuje (linia 75 w obecnym kodzie — "group" jest już
w whitelist!), więc tylko trzeba zacząć go używać.

#### Hit-test routing w on_press

Dziś on_press hit-test order (linia 1130-1147 w `_build_hit_from_press`):
1. pin
2. wire  
3. bbox

Nowy order **bez modyfikatora** lub **z Ctrl[+Shift]**:
1. pin
2. wire
3. **group outline** (`_hit_test_group_bbox_empty`) → `Hit("group", gid)`
4. bbox (komponent)

Bbox zostaje po group outline bo bbox to "wnętrze komponentu", a group outline
to "rama grupy" — ramę powinno się trafić wcześniej niż wnętrze komponentu pod nią.

#### Dispatcher `_apply_selection_op` dla `Hit("group", ...)`

- `_resolve_basis(Hit("group", gid), SINGLE)` → returns `(empty_comps, empty_wires, frozenset({gid}))` — **nowy 3-tuple zamiast 2-tuple**
- Wymaga rozszerzenia 3 helperów `_set_selection / _add_to_selection / _remove_from_selection`
  o trzeci basis: `group_basis: frozenset[uuid.UUID]`
- Każdy helper operuje na `_selected_group_ids` analogicznie jak na `_selected_ids`

#### Migracja long-press (decyzja 2)

Long-press 500ms / 1000ms dziś robi:
- 500ms → `_select_group(gid)` (SET single group)
- 1000ms → `enter_group(gid)` (zmień kontekst)

Po migracji:
- 500ms **usunięte** — plain LMB na bbox grupy = `_apply_selection_op(Hit("group", gid), SINGLE, SET)`
  daje ten sam efekt (clear+set), bez czekania 500ms — **szybsze UX**
- 1000ms **zachowane** dla enter-group (nadal jedyny sposób wejścia w grupę bez double-click)

Sub-decision: czy plain LMB na bbox grupy = SET single group, czy = SET first
member as component? Decyzja: **SET single group** (grupa to byt, nie komponent).
Double-click w jej członka nadal enters group (osobna mechanika).

Skutek: `_start_longpress` zachowane tylko dla edit-phase (1000ms).
Select-phase usunięta. `_longpress_phase` zostaje tylko "idle" lub "edit".

### Etap 6 — rubber-band selektuje grupy (~80 LoC)

Decyzja 3: LTR = all members inside, RTL = any member inside.

`_finish_rubber_band` (linia 6300+):
- iteruje `session.layout.groups`
- dla każdej `gid`, lista AABB jej members:
  ```python
  member_bboxes = [
      session.component_bbox(cid) 
      for cid in all_component_ids(groups_map, gid)
  ]
  ```
- LTR: `gid` selected iff **wszystkie** member_bboxes są fully inside rect
- RTL: `gid` selected iff **dowolny** member_bbox intersects rect

Wynik agreguje do `selected_groups: set[UUID]`.

Pre-existing convention rubber-band: bezpośrednio ustawia `_selected_ids` /
`_selected_wire_ids`. Dla `_selected_group_ids` analogicznie:
```python
self._selected_group_ids = selected_groups
```

**Ważne**: gdy LTR rubber-band łapie grupę G1 (wszyscy members inside) **oraz**
pojedyncze komponenty grupy G2 (nie wszyscy inside), to:
- G1 → trafia do `_selected_group_ids`
- members G2 którzy są inside → **NIE** trafiają do `_selected_ids` (są w grupie nieczynnej, `_is_locked_in_group` filtering)

Wyjątek: free komponenty (top-level, `_is_locked_in_group()=False`) **trafiają**
do `_selected_ids` normalnie.

### Etap 7 — testy + integracja (~250 LoC)

**Unit:**
- `tests/unit/test_drag_set.py` — już z Etap 1
- `tests/unit/test_pad_conflict.py::TestMultiGroupConflict`
  - 1 grupa, drag inside → CLEAR
  - 2 grupy, drag-set zawiera obie, kolizja pad-pad between → CLEAR (SOFT inside drag-set; decyzja 4)
  - 2 grupy, jedna w drag-set, druga statyczna, kolizja → BLOCKED
  - mixed (1 grupa + 1 free), kolizja free vs group member → CLEAR (SOFT)
  - mixed, kolizja member vs static komponent poza drag → BLOCKED

**GUI:**
- `tests/gui/test_select_tool.py::TestMultiGroupSelection`
  - Ctrl+klik na bbox grupy → dodaje do `_selected_group_ids`
  - Ctrl+Shift+klik → usuwa z setu
  - plain LMB na bbox grupy → SET single group (replaces selekcję)
  - long-press 500ms (legacy) — **test zostaje usunięty** (semantyka migrowana)
  - long-press 1000ms → enter group (zostaje)
- `tests/gui/test_select_tool.py::TestMixedDrag`
  - drag-set = {free C1, group G1{C2,C3}} + drag o (+2, 0) → C1+C2+C3 przesunięte
  - drag-set jak wyżej + wire W1 in `_selected_wire_ids` → wire też przesunięty
  - undo single Ctrl+Z → wszystko wraca atomowo
- `tests/gui/test_select_tool.py::TestRubberBandSelectsGroups`
  - LTR rubber-band łapiący 2 grupy (wszyscy members inside) → obie w `_selected_group_ids`
  - LTR łapiący 1 grupę full + 1 grupę partial → tylko full
  - RTL łapiący partial → wszystkie partial-intersect grupy

### Etap 8 — memory + docs + merge (~40 LoC docs)

`docs/highlight-state-machine.txt` rev 3 — dopisać `Hit("group", ...)`
do macierzy modifierów + tabela: `Hit("group", gid)` SET/ADD/REMOVE
mutuje `_selected_group_ids` zamiast `_selected_ids`/`_selected_wire_ids`.

`MEMORY.md` + memo Phase 3:
- `[[multi-group-drag-2026-XX-XX]]` — Phase 3 done; supersedes `g:0/1` shim w summary
- update istniejące `[[selection-fsm-refactor-2026-06-08]]` z nową notatką o spec rev 3

`docs/multi-group-mixed-drag-plan.md` → rename na `_DONE` lub usuń (plan-after-execution).

Merge do main.

## Ryzyka

| Ryzyko | Mitigacja |
|---|---|
| `check_group_move_conflicts` ma 6 użytkowników poza drag-pipeline; multi-group sig change rozprasza | Stary 1-arg API zostaje jako shim wołający multi z `{gid}`; nowe sites używają multi |
| Atomowość N×MoveGroup + N×MoveComponent — partial commits przy revert | MacroCommand wzorzec już istnieje (`atomic_rollback` z `[[group-origin-invariant]]`); reużywamy |
| `DragSet.all_component_ids` rekurencyjne rozwijanie podgrup — koszt | groups rzadko są zagnieżdżone na >3 poziomy; benchmark w testach jeśli >100ms |
| Long-press 500ms migracja może zaskakuje użytkowników którzy zdążyli się przyzwyczaić | Plain LMB na bbox = SET single group daje ten sam efekt bez czekania 500ms — szybsze UX, krótka adaptation. Dokumentujemy w CHANGELOG. |
| Rubber-band selektujący grupy obok komponentów: konflikt "wszystko inside" dla LTR | Specjalna semantyka: grupa "inside" gdy wszystkie jej members inside (AABB unionu) |

## Decyzje — zatwierdzone (zob. nagłówek)

Decyzje 1-5 są zatwierdzone i wpisane na początku dokumentu.  
Etapy poniżej odzwierciedlają zatwierdzone wybory.

## Estymacja

- Etapy 1-2: niskie ryzyko (~3-4h)
- Etapy 3-4: średnie ryzyko, dużo testów (~6-8h)
- Etapy 5-6: średnie ryzyko, UX validation (~4-5h)
- Etap 7: testy (~3-4h)
- Etap 8: docs + merge (~1h)

**Łącznie: ~17-22h pracy aktywnej + buffer na regresje.**

## Workaround dziś (bez Phase 3)

Jeśli chcesz drag jednego komponentu razem z grupą:
1. Wejdź w grupę (double-click).
2. W kontekście grupy dodaj komponent (Ctrl+klik).
3. Drag.

Ten workaround **zmienia kontekst grupy**, więc nowy komponent staje się
członkiem grupy. Nie jest to "mixed drag" — jest to ekspansja grupy. Dla
prawdziwego mixed dragu potrzebujemy Phase 3.
