# Benchmarki automatyczne

Projekt referencyjny: `m4/` (139 komponentów, 597 drutów).

## Uruchomienie

```bash
uv run pytest tests/benchmarks/ --benchmark-only -v

# Zapis baseline po celowej optymalizacji:
uv run pytest tests/benchmarks/ --benchmark-only --benchmark-save=baseline

# Weryfikacja regresji (fail przy >20% wolniej):
uv run pytest tests/benchmarks/ --benchmark-only \
    --benchmark-compare=baseline \
    --benchmark-compare-fail=mean:20%
```

Baseline: `.benchmarks/Linux-CPython-3.14-64bit/0001_baseline.json`.

## Pokryte ścieżki

| Test | Oczekiwany rząd | Co mierzy |
|---|---|---|
| `test_bench_hit_test_component` | < 50 µs | Spatial index — klik na komponent |
| `test_bench_hit_test_pin_for_comp` | < 20 µs | Scan pinów jednego komponentu |
| `test_bench_hit_test_wire` | < 200 µs | Spatial index — klik na drut |
| `test_bench_set_highlighted_net` | < 5 ms | setOpacity na grupach, bez rebuildu |
| `test_bench_spatial_index_build` | < 10 ms | Budowa indeksów po layout_changed |
| `test_bench_connectivity_graph_build` | < 200 ms | Pełny rebuild grafu |
| `test_bench_refresh_warm_nothing_changed` | < 150 ms | Warm refresh, grupy reużyte |
| `test_bench_refresh_warm_one_comp_moved` | < 200 ms | Warm refresh, 1 grupa przebudowana |

## Profilowanie aplikacji (live)

**Py-Spy** — attach do działającego procesu, nie blokuje Qt event loop:

```bash
uv run python -m protoboard &
py-spy record -o profile.svg --pid $(pgrep -f protoboard) --duration 30
py-spy top --pid $(pgrep -f protoboard)
```

**cProfile + SnakeViz:**

```bash
uv run python -m cProfile -o profile.out -m protoboard
uv run snakeviz profile.out
```

## Architektura optymalizacji GUI

Kluczowe optymalizacje do zachowania przy refaktoringu:

**Spatial index** (`session.comp_spatial_index()`, `session.wire_spatial_index()`): budowany leniwie po `layout_changed`; `_hit_test()` skanuje tylko komórki ±1.

**Group reuse** (`update_component_groups`, `update_wire_groups`): `QGraphicsItemGroup` per komponent/drut przeżywa między refreshami. Fingerprint = (pozycja, rotacja, sieć, warstwa). Highlight: `setOpacity()` na istniejących grupach — O(N), zero kreacji itemów.

**Board layer caching** (`_board_layer_dirty`): `render_board()` wywoływany tylko przy zmianie tematu, strony kamery, transform lub alpha płytki.

**Debounce refresh** (`_refresh_timer`, 16 ms): `layout_changed` → `_schedule_refresh()`.

**Cached ConnectivityGraph** (`session.connectivity_graph()`): unieważniany przez `invalidate_graph()` (podłączony do `layout_changed`); budowany leniwie.

**`user_data_map`** w `ConnectivityGraph`: `dict[int, object]` — klucz = `ElectricSet.id`; przygotowany do przyszłych optymalizacji highlight, jeszcze nieużywany.

Historia etapów optymalizacji (inkrementalny graf, Rust A*): [performance.md](performance.md).
