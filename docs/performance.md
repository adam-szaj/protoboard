# Optymalizacje wydajności

## Tło

Projekt "micek" (143 komponenty, 100 netów) był punktem odniesienia dla wszystkich
pomiarów. Na początku:

- Autoroute All Nets: **~2.5 minuty**, UI zablokowany przez cały czas
- Przesuwanie komponentów: przycięcia co ~600 ms (DRC blokował wątek UI)
- GUI po puszczeniu myszy: ~150 ms blokady (synchroniczny rebuild 4 paneli)

Po czterech etapach optymalizacji:

- Autoroute All Nets: **~15 sekund**, UI responsywny (worker w QThread)
- Przesuwanie komponentów: brak odczuwalnych blokad
- DRC: 7.8 ms → 1.5 ms

---

## Etap 1 — Inkrementalny ConnectivityGraph

**Problem:** Autorouter wywoływał `ConnectivityGraph(board, layout, ...)` przy każdym
segmencie A* (każdej próbie poprowadzenia kabla). Przy 600 segmentach na projekt
dawało to 600 pełnych przebudów grafu networkx — O(V+E) każda, łącznie ~2.5 minuty.

**Rozwiązanie:** Nowa implementacja w `domain/connectivity/incremental.py` zastąpiła
networkx strukturą union-find z obsługą cofania.

Kluczowe właściwości:

| Operacja | Złożoność | Poprzednio |
|---|---|---|
| `add_wire(wire)` | O(\|path\|) | O(V+E) — pełny rebuild |
| `remove_wire(wire)` | O(K) — rozmiar evicted set | O(V+E) |
| `are_connected(a, b)` | O(1) — porównanie canonical_key | O(V+E) — `nx.has_path` |

Szczegóły implementacji: [connectivity.md](connectivity.md).

**Efekt:** Greedy pass autorouter: 2.5 minuty → **5 sekund** (30×).

---

## Etap 2 — Natywny A* w Rust (PyO3 + rayon)

**Problem:** CPython GIL blokował prawdziwy paralelizm wątków. `ThreadPoolExecutor`
ze 16 wątkami dawał dokładnie 1 aktywny wątek w danej chwili.

**Rozwiązanie:** Natywna implementacja A* w Rust (`services/routing/astar_rs/`):

- `pyo3` — bindings Python ↔ Rust; `py.allow_threads()` zwalnia GIL na czas obliczeń
- `rayon` — work-stealing thread pool; każdy "wszechświat" A* to osobny task rayon
- Skalowanie `f64 → i32` (mnożnik 1000) — unika haszowania float w `HashMap`
- `Arc<Vec<PathStep>>` dla ścieżek — clone przy rozgałęzieniu = tylko atomowy refcount

Python odpada do implementacji czystego Pythona gdy Rust nie jest dostępny
(`try: import protoboard_astar_rs`).

Optymalizacja wieloprzebiegowa uruchamia 10 równoległych instancji autorouter
w osobnych procesach (`ProcessPoolExecutor`) — każda z inną kolejnością netów
(omija GIL przez izolację procesów).

**Efekt:** Całkowity czas autoroutingu micek: 2.5 min → **~15 sekund**.

---

## Etap 3 — Debounce paneli bocznych

**Problem:** Sygnał `layout_changed` był bezpośrednio podłączony do `refresh()`
czterech paneli bocznych (netlist, groups, nets editor, components). Każdy panel
robi `clear()` + przebudowę `QTreeWidget` — łącznie ~150 ms blokady synchronicznie
w wątku UI przy każdym mouse-release.

**Rozwiązanie:** `_panels_timer` (`QTimer`, singleShot, 100 ms) w `MainWindow`.

```
layout_changed → _panels_timer.start()   # natychmiastowe, ~0 ms
100 ms później → _refresh_panels()       # jeden rebuild, amortyzowany
```

Jeśli kolejny `layout_changed` przyjdzie przed upływem 100 ms (szybka seria
ruchów), timer jest restartowany — panele odświeżają się dopiero gdy seria się
skończy.

**Efekt:** Brak odczuwalnych blokad po puszczeniu myszy. Dla serii ruchów
(np. strzałki klawiaturowe): N rebuilds → 1 rebuild.

---

## Etap 4 — Cached ConnectivityGraph w sesji

**Problem:** `DRCEngine.check()` budował nowy `ConnectivityGraph` od zera przy każdym
uruchomieniu DRC (auto-DRC timer co 600 ms). Przy micek: 6.3 ms samo budowanie
grafu z 7.8 ms całkowitego kosztu DRC. Przy szybkiej serii ruchów (drag & drop
kilku komponentów) DRC odpalał się między ruchami i blokował UI.

**Rozwiązanie:** Graf trzymany jako `_graph: ConnectivityGraph | None` w `EditorSession`:

```python
# Przy layout_changed — synchronicznie, ~0 ms:
session.invalidate_graph()   # _graph = None

# Przy DRC (600 ms później) — raz per okno DRC:
session.connectivity_graph() # buduje jeśli _graph is None, zwraca cached
```

`DRCEngine.check_with_graph()` przyjmuje gotowy graf zamiast budować własny.
Stary `check()` pozostał dla kompatybilności (buduje graf sam i deleguje).

**Efekt:**

| | Poprzednio | Po |
|---|---|---|
| DRC (zimny start) | 7.8 ms | 7.8 ms (pierwszy raz po ruchu) |
| DRC (ponowny, bez zmian) | 7.8 ms | **1.5 ms** |
| Seria ruchów | blokada co 600 ms | brak blokad — jeden build na końcu serii |

---

## Podsumowanie

| Metryka | Przed | Po | Zmiana |
|---|---|---|---|
| Autoroute micek | ~2.5 min | ~15 s | **10×** |
| DRC (z przebudową grafu) | 7.8 ms | 1.5 ms | **5×** |
| Blokada GUI po mouse-release | ~150 ms | ~0 ms | — |
| Blokada GUI — seria ruchów | co 600 ms | brak | — |
