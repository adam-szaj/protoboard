# Drag-pipeline architecture (po refaktorze)

## Aktorzy

- **`SelectTool`** — owner stanu drag (orig placements, snapped position,
  floating mode, overlay items). Listenuje na Qt eventy: press / move /
  release / Esc / Enter / double-click.
- **`DragDetector`** (`services/drag_detector.py`, nowy) — pure-logic
  classifier. In: `DragSnapshot` + moved component IDs + dragged wire
  IDs. Out: `DragCommitDecision`. Konsumuje **`PadStrategy`** dla każdej
  decyzji o padzie, zamiast inline'ować rule 5/6/8 logikę.
- **`DragSnapshot`** (frozen dataclass w `drag_detector.py`) —
  immutable view tego co layout reprezentuje "teraz" dla potrzeb
  detekcji. Zawiera `pad_index` (już aktualny, mutowany incrementally
  podczas drag), `placements`, `schematic`, `instances`, `components`.
- **`PadStrategy`** (`services/pad_strategy.py`, istniejący) —
  reguły pozwoleń. `DRCEnforcingStrategy` jest defaultem dla
  drag-pipeline (rule 6 strict: żaden wire na pinie, rule 8: net gate).
- **`PadIndex`** (`domain/board/pad_state.py`) — single source of truth
  dla pad occupancy. **Mutowany incrementally** podczas drag przez
  `remove_component(cid)` + `add_component(cid, new_placement)` zamiast
  pełnego rebuild per snap.
- **`CommandStack`** — execute macro `[RemoveWires] +
  [TranslateAnchoredWires] + [DropWires] + [MoveComponent]`. Każda
  command pojedyncza, undo-friendly.
- **`DragConflictOverlay`** (`drag_conflict_overlay.py`) — bez zmian,
  renderuje wynik DragCommitDecision (Qt items).

## Sekwencja (single component drag)

```
on_press
  ├─ capture orig placement
  ├─ self._drag_pad_index = session.pad_index().clone()
  └─ self._drag_detector = DragDetector(session.strategy_for("drc"))

on_move (×N, ~60 fps)
  ├─ snap cursor → new position
  ├─ if snap unchanged → skip
  ├─ update self._session.layout.placements[cid] in-place
  ├─ self._drag_pad_index.remove_component(cid)
  ├─ self._drag_pad_index.add_component(cid, new_pl, defn, schematic)
  ├─ snapshot = DragSnapshot(pad_index=self._drag_pad_index, ...)
  ├─ decision = self._drag_detector.classify(snapshot, {cid}, dragged_wires)
  ├─ self._drag_overlay.update(decision) → red/yellow rects + crosses
  └─ self._corner_text.show(decision.description)

on_release
  ├─ decision = self._drag_detector.classify(snapshot, {cid}, dragged_wires)
  │   (decision już aktualne z ostatniego on_move; classify cheap)
  ├─ if decision.is_clear and auto_commit_on_clear:
  │     enter floating with commit pending
  │     auto-commit (Enter event synthesised) OR floating UX
  ├─ if decision.is_soft:
  │     enter floating; user musi confirm (Enter / dblclick LPM)
  └─ if decision.is_hard:
        enter floating; commit blocked; user musi cofnąć (Esc) lub
        przesunąć dalej

on_commit (Enter / dblclick LPM)
  ├─ build cmds = [
  │     RemoveWireCommand(wid) for wid in decision.auto_remove_wire_ids,
  │     TranslateAnchoredWiresCommand(cid, old_pl, new_pl),
  │     DropWiresCommand(skewed_wire_ids),
  │     MoveComponentCommand(cid, new_pl, old_pl, placement_only=True),
  │   ]
  ├─ stack.execute(MacroCommand(cmds))
  ├─ self._drag_pad_index = None
  └─ session.layout_changed.emit()

on_cancel (Esc)
  ├─ revert placements + wire paths in-place
  ├─ self._drag_pad_index = None
  └─ session.layout_changed.emit()
```

## Co mutuje stan i kiedy

| Stan                              | Live drag (on_move)              | Commit                                    | Cancel (Esc) |
|-----------------------------------|----------------------------------|-------------------------------------------|--------------|
| `layout.placements[cid].position` | in-place mutacja (live preview)  | utwierdzone przez MoveComponentCommand    | revert       |
| `layout.wires[*].path`            | jeśli `dragged_wire`: in-place   | utwierdzone przez TranslateAnchoredWires  | revert       |
| `self._drag_pad_index`            | incremental remove/add           | discarded                                 | discarded    |
| `session.pad_index()` (cached)    | **nietknięty** — stale do commit | invalidated by `layout_changed`           | invalidated  |
| `schematic.nets`                  | **nietknięty**                   | mutated przez `decision.auto_net_actions` | nietknięty   |

Kluczowa zasada: live drag NIGDY nie mutuje schematic, NIGDY nie tworzy
ani usuwa wires na `session.layout.wires`. Cały aspekt "wire wisi na
pinie i się rusza z nim" rozwiązany na poziomie path mutation, którą
i tak musimy zrobić by rysować live preview. Decision tylko
*deklaratywnie* mówi "po commit te wires masz usunąć / przenieść".

## Gdzie strategia jest wstrzykiwana

- `DragDetector.__init__(strategy: PadStrategy)` — owner przekazuje
  *raz* na drag-start.
- Wewnątrz `classify()` strategy jest jedynym sędzią dla każdego
  per-pad pytania:
  - `strategy.can_place_pin(pad, our_net)` — rule 6+8 razem (foreign
    wire na pinie, foreign-net pin-on-pin).
  - `strategy.can_form_t_junction(pad, ...)` — gdy dragged wire ma
    przejść w T-junction (rzadkie w drag, częstsze w route).
  - `strategy.can_solder_wire_to_pad(...)` — dla endpointów dragged
    wires lądujących na padach po przesunięciu.

`SelectTool` nie inspekcjonuje pad internals nigdzie. Cała wiedza
"co tu się dzieje na padzie X po przesunięciu komponentu Y" idzie
przez `DragDetector → strategy → pad_index`.

## Co znika

- **`find_conflicts`, `find_wire_conflicts`, `find_wires_crossing_pads`,
  `find_skewed_wires`** w `pad_conflict.py` / `wire_utils.py` zostają
  jako pure helpers wywoływane *wewnątrz* `DragDetector`. Nie są już
  punktem wejścia dla drag pipeline.
- **`classify_drag_commit`** w `placement_conflict.py` redukuje się
  do thin wrapper: `DragDetector(strategy).classify(snapshot, …)`.
- **`_update_drag_overlay` z ~150 LOC inline logiki** redukuje się
  do ~30 LOC: zbuduj snapshot, deleguj, narysuj wynik.
- **`MoveComponentCommand._update_wires_for_move`** zostaje jako
  private helper dla `TranslateAnchoredWiresCommand`. Move staje się
  pure placement.
- **PadIndex rebuild per snap** (~3ms × N) zastąpiony jednym clone
  na drag-start + incremental update per snap (~0.2ms).

## Co zostaje bez zmian

- `DragConflictOverlay` — czysty rendering, niezależny od reszty.
- `DragCommitDecision` + `DragSeverity` — kontrakt z overlay'em.
- `MoveConflictResult` + `MoveConflictSeverity` (`pad_conflict.py`) —
  używane przez group drag (`_build_group_drag_commands`); osobna
  ścieżka, do migracji w follow-up.
- `MacroCommand` — sekwencjonowanie bez zmian.
