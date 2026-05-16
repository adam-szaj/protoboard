# Routing

## 1. Model kosztów

```python
@dataclass(frozen=True)
class RoutingCostModel:
    free_pad: float = 1.0
    same_net_pad: float = 0.1
    occupied_by_other_net: float = inf
    direction_change: float = 1.0
    diagonal_multiplier: float = sqrt(2)
    crossing_bare_bare: float = inf       # goły × goły = zwarcie
    crossing_insulated_bare: float = 5.0  # izolowany × goły = jumper
    layer_change: float = 2.0
    under_component_top: float = 3.0
```

Konfiguracja per projekt w `routing.yaml`:

```yaml
preferred_side: bottom
allow_layer_change: true
max_rip_up_passes: 3
max_optimize_iterations: 10
cost_model:
  direction_change: 1.0
  crossing_insulated_bare: 5.0
  layer_change: 2.0
  under_component_top: 3.0
```

`RoutingSettings` (frozen dataclass, `domain/routing_settings.py`). Edycja przez Preferences → Routing.

## 2. Algorytm A*

Węzły = `(x, y, side)`. Krawędzie = 8-connectivity. Via = plated-through pad (koszt `layer_change`).

**Wielowarstwowość:** `PathStep = (Point, side)`. Wynik dzielony przez `split_path_by_side()` na `WireSegment` per warstwa.

**Tie-breaker `preferred_side`:** przy równym koszcie wygrywa strona z `RoutingSettings.preferred_side` (domyślnie bottom).

**Steiner tree (greedy nearest-neighbour):** seed = para o minimalnej odległości manhattańskiej. W każdym kroku para (connected, unconnected) o min dystansie; A* dostaje jeden konkretny source.

**`near_pin_pads`:** kara korytarzowa wokół pinów niekompletnych sieci; wykluczane są piny komponentów już w bieżącej sieci (Panama Canal przez DIP8).

**Iteracyjna optymalizacja:** do `max_optimize_iterations` przebiegów od czystego stanu, zatrzymanie przy konwergencji (fingerprint drutów niezmieniony).

**Blokowanie nakładania drutów:** `_build_wire_occupied()` side-aware — insulated blokuje tylko swoją stronę, bare blokuje obie.

**Logi diagnostyczne:** flaga `_DEEP_DEBUG` w `autorouter.py`.

## 3. Narzędzia domenowe — wire_utils

`domain/layout/wire_utils.py`:
- `board_pads_for_side(board, side)` → `set[Point]`
- `plated_through_pads(board)` → `set[Point]`
- `insert_pad_nodes(path, pad_positions)` → `list[Point]` — wstawia węzły na padach leżących na odcinkach trasy

## 4. Autorouter

`services/routing/autorouter.py` — `autoroute()`, `_run_one_pass`. Dostępny przez Tools → Autoroute All Nets (F6). Wynik w `MacroCommand` → pełne undo.

Natywna implementacja A* w Rust (`services/routing/astar_rs/`, PyO3 + rayon); fallback do czystego Pythona gdy niedostępna. Szczegóły: [performance.md](performance.md).
