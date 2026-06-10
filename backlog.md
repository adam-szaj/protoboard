  Backlog ProtoboardDesigner — przegląd

  🎯 Funkcjonalne (czekają na decyzję / implementację)

  Auto-routing / quality:
  - ~~Hierarchical autoroute~~ — **DONE 2026-06-09**: `services/routing/hierarchical.py` + `RoutingSettings.hierarchical` + GUI checkbox; bottom-up DFS po grupach; top-level pass na końcu. Per-wire simplify opcja (`simplify_after_each_wire`) też dodana.
  - Net name color heuristics — auto-color GND/VCC/V±/CLK/SDA/SCL/INT/RX/TX/RST. Świadomie deferred, plug-in między manual i palette/golden.
  - Side unification pass — osobny etap po unweave: scal logical wire na jedną stronę gdzie się da. Szkic _rebalance_sides w unweave.py (majority-vote flip z short_check), wyłączony.
  - ~~Synthetic bench fixtures~~ — **DONE 2026-06-09**: 5-fixture suite (`tests/fixtures/synthetic_bench/`) generowany in-memory; 14/14 bench testów zielone; legacy m4 fixture nadal failuje precondition (broken project, zostawiony w conftest jako legacy).

  DRC / structural:
  - Hierarchical DRC — błędy wewnątrz grupy raportowane w kontekście grupy.
  - Group templates — wielokrotne użycie tego samego sub-układu (instancje grup). Architektura przewiduje, nie implementuje. Duży refactor.

  UI / UX:
  - Project history UI — git wired (auto-init przy save), brakuje: panel historii commitów, restore-to-commit, diff viewer między commitami, commit message dialog przy save.
  - Wire-edit tool ulepszenia — Phase 2c przeniósł na [E], ale tooltip / status bar message dla "Select a wire first" możliwie ulepszyć.
  - Wire-edit action w rejestrze — [E] jest dziś hard-coded w `select_tool.py::on_key`, narusza [[every-action-has-keybinding]]. Dodać `ACTION_WIRE_EDIT` w `keybindings.py` + `ActionSpec` w `actions.py` + slot w `MainWindow` delegujący do `SelectTool._start_wire_edit`, usunąć branch `Key_E` z `on_key`. Pojawi się w Shortcuts (do przebindowania) i menu Edit ► Design ► Edit Wire Path (zgodnie z [[menu-toolbar-redesign-plan-2026-06-03]]).
  - body-render-based-hit-test — pixel-perfect hit-test komponentu przez alpha mask wyrenderowanego body (rozróżnia "klik w body" vs "klik w pustkę w bbox"). Wymaga nietrywialnej implementacji żeby było tanie: cached alpha mask per (footprint, rotation, side), query pixel przy click. Alternatywa dla obecnego pin-vs-bbox rozróżnienia opartego na _HIT_RADIUS — głównie korzyść dla IC z dużymi bbox.

  ⚠️ Wydajność (oznaczone CRITICAL/MEDIUM)

  - Main-thread blockers (audyt 2026-06-01):
    - ~~CRITICAL~~ **DONE 2026-06-09**: _on_autoroute_done O(N²) — incremental interior_vertex_index w `Layout._index_wire`/`_unindex_wire` (12.6× szybciej na 1000 wires)
    - ~~MEDIUM: simplify / unweave / save synchroniczne~~ **DONE 2026-06-09**: wszystkie trzy uruchamiane przez `run_with_progress` w background thread. Snapshot pre-flight, worker liczy nowe wires / zapisuje pliki, main thread aplikuje commands. Cancel dialog w każdej.
    - OK: autoroute / DRC / place mają workery
  - Paint perf Phase 2b/3 — odłożone w Phase 2c (docs/perf-refactor.md notatka). Phase 2 paint 700-900ms → 85ms. Phase 2b "on hold czekając na user confirmation czy back items są widoczne nad board" — to dziś nieaktualne bo Phase 2c rozwiązał problem inaczej, ale Phase 3 paint optimisation może być warta sprawdzenia.

  🔧 Drobiazgi (małe, gdy ktoś będzie chciał)

  - Per-fragment DRC — Wire refactor 2026-05-27 zostawił to jako follow-up.
  - Off-pad joint warnings — DRC nie wykrywa joint-knot pozycji daleko od pad.
  - _promote_primary("group", gid) deterministic ordering — bierze next(iter(members)) (niedeterministyczne). Phase 3 follow-up.
  - Hit-test priority — jeśli user skarży się "klikam na bbox a wybiera grupa", możliwe odwrócenie group ↔ bbox order w _build_hit_from_press.
  - Spec rev 3 nie covers Ctrl+R / Alt+R dla grup — keyboard ops na grupie nie są well-defined.

  📌 Ślady do rozważenia (idee, nie wymagania)

  - Cost analyzer istnieje ale czeka na pełne UI integration (już w PerformancePanel).
  - Push-and-shove routing — eksperyment już mergowany, można rozszerzyć.
  - KiCAD live sync — działa dla .net, można rozszerzyć na .kicad_sch / .kicad_pcb.

  ---
  Moja rekomendacja co najpierw

  Jeśli pytasz co ma największą wartość per godzina pracy:

  1. Synthetic bench fixtures — odblokuje rzetelne benchmarki (3 testy bench failują dziś przez broken m4 fixture). ~4-6h, ale czysty zysk dla automated quality tracking.
  2. Main-thread CRITICAL blocker (autoroute commit O(N²)) — jeśli kiedyś chcesz autoroute >300 wires, to dziś rozwala UX (UI zamarza). ~3-4h.
  3. Hierarchical autoroute — twój własny pomysł, prerequisites gotowe. Duża wartość user-facing (grupy stają się jednostką projektowania). ~6-10h.


