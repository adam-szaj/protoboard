# Incremental Connectivity Graph

## 1. Problem i motywacja

Router potrzebuje odpowiedzi na jedno pytanie w kółko: **czy pad A i pad B są już elektrycznie połączone?** W naiwnej implementacji opartej na networkx każdy nowy drut wymaga przebudowania całego grafu od zera — O(V + E) gdzie V to wszystkie pady planszy (600+ dla micek), a E to wszystkie krawędzie (druty, paski, połączenia plated-through). Na projekcie micek (143 komponenty, 100 netów) greedy pass wywoływał tę przebudowę ~600 razy, co dawało łącznie ~2.5 minuty.

Inkrementalna struktura zastępuje pełną przebudowę operacjami O(path_len) na drut:
- `add_wire(wire)` — scala zbiory, O(|path|)
- `remove_wire(wire)` — rozdziela zbiory, O(|evicted_set|)
- `are_connected(a, b)` — sprawdzenie połączenia, O(1)

Efekt: greedy pass z ~2.5 minuty do ~5 sekund (30× przyspieszenie).

---

## 2. Model danych

### ElectricSet — zbiór połączonych punktów

Każda grupa elektrycznie połączonych padów tworzy jeden `ElectricSet`. Zawiera:

```
ElectricSet:
  id: int                      # unikalny identyfikator
  _items: list[NodeEntry]      # wszystkie pady w zbiorze, w kolejności dołączania
  canonical_key: (x, y, side)  # min(x, y, side) ze wszystkich padów — stały reprezentant
```

`canonical_key` to leksykograficzne minimum spośród wszystkich kluczy `(x, y, side)` w zbiorze. Dwie funkcje zwracają `canonical_key` tego samego obiektu wtedy i tylko wtedy, gdy dwa pady są połączone — stąd O(1) porównanie:

```python
node_for_pin(ref_a) == node_for_pin(ref_b)   # True iff elektrycznie połączone
```

### NodeEntry — jeden punkt elektryczny

```
NodeEntry:
  key: (x, y, side)      # pozycja na siatce i strona płytki
  electric_set: ElectricSet  # referencja do zbioru (mutowalna przy merge/split)
```

Każdy pad planszy (każda dziura na każdej stronie) ma dokładnie jeden `NodeEntry`. Pin komponentu bez drutu ma własny jednoelementowy `ElectricSet` — "wire-stub" o zerowej długości.

### Indeksy

```
PadIndex:  (x, y, side) → NodeEntry          # O(1) lookup po pozycji
PinIndex:  PinRef → NodeEntry                 # O(1) lookup po pinie komponentu
WireIndex: wire.id → WireRecord               # historia merge dla rollback
```

### WireRecord — historia scalenia

Każde `add_wire()` produkuje `WireRecord` zawierający listę wykonanych scaleń:

```
WireRecord:
  wire_id: UUID
  merges: list[MergeRecord]     # w kolejności wykonania (LIFO przy remove)
  is_loop: bool                 # True gdy drut nie scalał (oba końce już połączone)
  intermediate_keys: list[...]  # węzły stworzone tylko dla tego drutu

MergeRecord:
  merged: ElectricSet   # zbiór który przeżył (większy)
  evicted: ElectricSet  # zbiór który został wchłonięty
  split_idx: int        # merged._items[split_idx:] pochodzi z evicted
```

---

## 3. Operacja merge (add_wire)

Gdy drut łączy dwa punkty z różnych zbiorów, wykonywane jest **union by size** — mniejszy zbiór jest wchłaniany przez większy:

```
Przed merge:
  SetA._items = [P1, P2, P3]   canonical = (1.5, 0.5, "top")
  SetB._items = [P4, P5]       canonical = (3.5, 0.5, "top")

Drut łączy P3 (należy do SetA) z P4 (należy do SetB).
SetA jest większy → SetB jest wchłaniany.

Po merge:
  SetA._items = [P1, P2, P3, P4, P5]   split_idx = 3
                 ←── stare SetA ──→←─SetB─→
  SetA.canonical = min((1.5,...), (3.5,...)) = (1.5, 0.5, "top")

  Każdy NodeEntry z dawnego SetB: entry.electric_set = SetA
  SetB jest "martwy" — wyjęty z _all_sets, ale żyje w MergeRecord dla rollback
```

Dla drutu z N punktami pośrednich operacja merge jest wykonywana sekwencyjnie dla par (path[0], path[1]), (path[1], path[2]) itd. — każda para może scalić inne zbiory. Wszystkie scalenia są zapisywane w `WireRecord.merges`.

**Wykrywanie pętli:** przed każdym merge sprawdzamy `is a.electric_set is b.electric_set` (porównanie tożsamości obiektu, O(1)). Jeśli tak — drut tworzy pętlę redundantną, żaden merge nie jest wykonywany.

```
Drut łączy P1 i P3 — oba już w SetA.
WireRecord.is_loop = True, merges = []
```

---

## 4. Operacja split (remove_wire)

Cofnięcie merge polega na odtworzeniu oryginalnego podziału listy `_items`:

```
Przed splitem (po add_wire):
  SetA._items = [P1, P2, P3, P4, P5]   split_idx = 3

Split:
  items_merged = SetA._items[:3]  = [P1, P2, P3]
  items_evicted = SetA._items[3:] = [P4, P5]

  SetA._items = [P1, P2, P3]
  SetB._items = [P4, P5]          (SetB "wskrzeszony" z MergeRecord)

  Dla P4, P5: entry.electric_set = SetB
  SetB wraca do _all_sets
  canonical dla obu zbiorów przeliczony przez min()
```

`WireRecord.merges` jest odtwarzany w odwrotnej kolejności (LIFO) — ostatni merge jest cofany pierwszy. To gwarantuje poprawność gdy jeden drut wykonał wiele scaleń.

**Przypadek out-of-order:** jeśli scalony zbiór (`MergeRecord.merged`) nie jest już "żywy" — tzn. został późnej wchłonięty przez kolejny drut — LIFO nie zadziała. W tym przypadku wykonywany jest **local rebuild**: węzły dotknięte przez usuwany drut są resetowane do singletonów, a następnie przebudowywane przez przejście po wszystkich pozostałych `WireRecord`. Koszt: O(K) gdzie K = rozmiar dotkniętego zbioru, a nie O(pełny graf).

---

## 5. Canonical key i kompatybilność wsteczna

Stary `ConnectivityGraph` zwracał z `node_for_pin()` krotkę `(x, y, side)` reprezentującą węzeł networkx. Nowa implementacja zwraca `ElectricSet.canonical_key` — leksykograficzne minimum `(x, y, side)` ze wszystkich padów w zbiorze.

Semantyka jest identyczna: dwa wywołania zwracają tę samą wartość wtedy i tylko wtedy, gdy oba pady są elektrycznie połączone. Kod używający wyłącznie porównania `==` działa bez zmian.

`net_status.py` używał wcześniej `nx.has_path(graph._g, a, b)` — zastąpione przez `a == b` (porównanie canonical_key), co jest O(1) zamiast O(V+E).

`astar.py` używał `graph._g.nodes` — zastąpione przez `graph.all_node_keys()` (iterator po `PadIndex`).

---

## 6. Struktura w kontekście autoroutera

```
Przed greedy pass:
  graph = ConnectivityGraph(board, layout_bez_drutów, components, instances)
  for wire in user_wires:
      graph.add_wire(wire)         ← O(|path|) każdy

Dla każdego segmentu każdego netu:
  ┌─ sprawdź połączenie: graph.node_for_pin(ref_a) == graph.node_for_pin(ref_b)
  │
  ├─ jeśli niepołączone → uruchom A* → routed_wire
  │   graph.add_wire(routed_wire)  ← O(|path|), NIE O(pełny graf)
  │
  └─ rip-up gdy brak ścieżki:
      graph.remove_wire(ripped)    ← O(|evicted_set|)
      working_wires.remove(ripped)

Poprzednio: ConnectivityGraph(board, layout_with_all_wires, ...)  ← O(V+E) × 600
Teraz: add_wire() × 600  ← O(path_len) × 600
```

---

## 7. Złożoność operacji

| Operacja | Złożoność | Uwagi |
|---|---|---|
| `ConnectivityGraph.__init__()` | O(V + E) | Jednorazowe budowanie z Board+Layout |
| `add_wire(wire)` | O(\|path\|) | Sekwencyjne merge par punktów |
| `remove_wire(wire)` — LIFO | O(K) | K = rozmiar evicted set |
| `remove_wire(wire)` — out-of-order | O(K + E_local) | Local rebuild |
| `are_connected(a, b)` | O(1) | Porównanie `is` na ElectricSet |
| `node_for_pin(ref)` | O(1) | Lookup w PinIndex + canonical_key |
| `node_for_pad(pos, side)` | O(1) | Lookup w PadIndex + canonical_key |
| `electrical_nets()` | O(P) | P = liczba pinów w PinIndex |

---

## 8. Ograniczenia

**Out-of-order rip-up** — autorouter może usunąć dowolny drut, niekoniecznie ostatnio dodany. Gdy usuwany drut nie jest "na szczycie" historii merge (tzn. scalony zbiór był potem wchłonięty przez inny drut), wykonywany jest local rebuild. Jest to poprawne ale kosztowniejsze — O(K) zamiast O(K przez LIFO).

**Multi-hop wire** — drut przez N punktów wykonuje N-1 merge. Każdy jest zapisany oddzielnie w `WireRecord.merges`. Rollback odwraca je LIFO — poprawne o ile żaden z N-1 scalonych zbiorów nie był potem wchłonięty przez inny drut.

**Wątkowość** — struktura nie jest thread-safe. `add_wire`/`remove_wire` muszą być wywoływane sekwencyjnie. Odczyt (`node_for_pin`, `are_connected`) jest bezpieczny współbieżnie o ile żaden wątek jednocześnie nie modyfikuje struktury. W autorouter spełnione — A* (równoległy w Rust) tylko czyta `all_node_keys()`, a mutacje grafu są sekwencyjne w głównej pętli greedy.
