# Routing

## 1. Model kosztów

`RoutingCostModel` jest **dict-backed** (`weights: dict[str, float]`) z
zachowaną backward-compatibility dla legacy attribute access
(`cost_model.direction_change` itd.).  Routery deklarują które klucze
rozumieją przez `Router.supported_cost_keys()` — UI presetów filtruje
listę pokazywanych pól do unii kluczy wszystkich zarejestrowanych
routerów.

**Kluczowe API:**

```python
cm = RoutingCostModel(weights={"direction_change": 1.0, "bend_45": 0.5, ...})
cm.get("bend_45", 0.0)             # forward-compat lookup
cm.direction_change                # legacy attribute (delegates to weights)
cm.with_overrides(bend_45=2.0)     # immutable, returns new instance
```

### 1.1 Klucze (canonical w `COST_KEY_DOCS`)

| Klucz | Zakres | Znaczenie |
|---|---|---|
| `free_pad` | 1.0 | Bazowy koszt ortho-stepa |
| `same_net_pad` | 0.1 | Krok na pad bieżącej sieci (zniżka) |
| `occupied_by_other_net` | inf | Pin innej sieci (zablokowane) |
| `direction_change` | 1.0 | Stary fallback — używany gdy `bend_*` nieobecne |
| `diagonal_multiplier` | √2 | Mnożnik dla diagonali |
| `crossing_bare_bare` | inf | Goły × goły = zwarcie |
| `crossing_bare_bare_diagonal` | 2.0 | Dwie diagonale bare w cell-centrum (bare_diagonal_mode) |
| `crossing_insulated_bare` | 5.0 | Izolowany × goły = jumper |
| `layer_change` | 2.0 | Via na plated-through padzie |
| `under_component_top` | 3.0 | Step na top pod komponentem |
| `bare_diagonal_mode` | 0/1 | Strict parity-filter dla bare diagonali |
| `max_reroute_factor` | 0.0 | Push-and-shove ceiling (enhanced) |
| `same_net_wire_reuse_cost` | 0.0 | Same-net teleport via existing wire |

**Nowe knoby ("clean routing", opt-in, defaults 0.0):**

| Klucz | "clean_routing" | Znaczenie |
|---|---|---|
| `bend_45` | 0.5 | Kara za bend 45° (cos > 0.6) |
| `bend_90` | 1.5 | Kara za bend 90° (cos > -0.1) |
| `bend_135` | 4.0 | Kara za bend 135° (sharp turn) |
| `diagonal_preferred_family` | 0.0 | Bonus za diagonal w preferowanej rodzinie |
| `diagonal_dispreferred_family` | 0.5 | Kara za diagonal w "złej" rodzinie |
| `wire_over_foreign_wire_pad` | 0.5 | Step na cell z vertexem obcego netu |
| `via_on_foreign_pad` | 3.0 | Via na padzie z via obcego netu |
| `redundant_layer_change` | 1.5 | Kara za via (× 2 gdy odchodzi od dominującej strony) |

### 1.2 Bendy zależne od kąta

A* klasyfikuje każdy bend przez cosinus między `(prev_dx, prev_dy)` a
`(next_dx, next_dy)`:

- `cos > 0.6`  → bend 45° (np. E → NE)
- `cos > -0.1` → bend 90° (np. E → N)
- `cos <= -0.1` → bend 135° (np. E → NW, ostry zwrot)

Gdy wszystkie `bend_*` są `direction_change` (default), zachowanie
1:1 odpowiada legacy (jedna stała kara per turn).

### 1.3 Diagonal parity × side × family

Mapping preferowanej rodziny diagonali per (cell parity, side):

| Cell parity `(x+y) mod 2` | Side | Preferowana rodzina |
|---|---|---|
| even (0) | top | NE/SW (`/`) |
| even (0) | bottom | NW/SE (`\`) |
| odd (1)  | top | NW/SE (`\`) |
| odd (1)  | bottom | NE/SW (`/`) |

Cel geometryczny: gdy dwie diagonale spotykają się w międzypadowej
przestrzeni jednego cell, są wzajemnie ortogonalne (X-style cross) i
fizycznie się nie kolidują — można je przepleść między padami bez
zwarcia.

### 1.4 Presety

Klucze są bundle'owane w **presetach** — nazwanych dict'ach
przechowywanych w `app_settings.yaml` (`cost_presets:` sekcja).

**Built-in (read-only):**
- `default` — odpowiada historycznym defaultom (wszystkie nowe
  knoby = 0.0)
- `clean_routing` — wartości z tabeli 1.1 (kolumna "clean_routing")

User-defined presety: edycja przez **Preferences → Routing → Edit
presets…**.  Nie wolno cieniować nazw built-in (loader je ignoruje).

### 1.5 Konfiguracja per projekt w `routing.yaml`

```yaml
preferred_side: bottom
allow_layer_change: true
max_rip_up_passes: 3
max_optimize_iterations: 10
cost_preset: clean_routing     # nazwa presetu (built-in lub user)
cost_overrides:                # nadpisania per-projekt
  bend_45: 0.3
cost_model:                    # legacy named-field fallback
  direction_change: 1.0
  crossing_insulated_bare: 5.0
  layer_change: 2.0
  under_component_top: 3.0
```

Resolution order (right wins): built-in `default` → preset → legacy
fields → `cost_overrides`.  Stary projekt bez `cost_preset` /
`cost_overrides` ładuje się bit-identycznie jak dotąd.

`RoutingSettings` (immutable, `domain/routing_settings.py`).  Edycja
przez **Preferences → Routing**.

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
