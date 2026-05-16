# CLAUDE.md — Perfboard/Stripboard Layout Designer

## 0. Cel projektu

Aplikacja desktop (Linux, PySide6) do projektowania fizycznego rozmieszczenia komponentów na płytkach prototypowych (perfboard, stripboard).

**Kluczowa różnica:** projekt jest *connectivity-aware* — model danych to graf logicznych sieci (netlist), widok fizyczny to projekcja grafu na siatkę otworów. Umożliwia DRC, import/eksport KiCAD, inteligentny routing.

**Nazwa robocza:** `protoboard-designer`.

## 1. Stack technologiczny

| Warstwa | Wybór |
|---|---|
| Język | Python 3.11+ |
| GUI | PySide6 (Qt 6), `QGraphicsScene`/`QGraphicsView` |
| Grafy | `networkx` + własny union-find (`domain/connectivity/incremental.py`) |
| Routing | własny A* (Python + opcjonalny Rust PyO3) |
| Persistence | YAML (`ruamel.yaml`) |
| KiCAD I/O | własny parser S-expression (MIT) + opcjonalny `kiutils` (GPL) |
| Testy | `pytest` + `pytest-qt` |
| Linter | `ruff` + `black` + `mypy --strict` |
| Pakowanie | `uv` + `pyproject.toml` (PEP 621) |

## 2. Licencja

- Rdzeń, parser KiCAD, GUI, modele danych: **MIT**.
- Opcjonalny moduł `kicad_integration_gpl`: **GPLv3** (`pip install protoboard-designer[kicad-gpl]`).
- SPDX header w każdym pliku: `# SPDX-License-Identifier: MIT`.

## 3. Architektura — warstwy

```
GUI Layer (PySide6)
  ↑ Qt signals
Application Layer (CommandStack, Tools, Selection)
  ↑ Commands
Domain Model (pure Python, no Qt)
  ↑
Services (DRC, Router, KiCAD I/O, Persistence)
```

**Reguła:** Domain Model nie importuje Qt. Testowalny bez display servera.

## 4. Dokumentacja szczegółowa

| Temat | Plik |
|---|---|
| Model danych (box model, pady, schematic, layout, grupy) | [docs/data-model.md](docs/data-model.md) |
| Geometria (placement, rotacja, flip, bbox) | [docs/geometry.md](docs/geometry.md) |
| Routing (A*, cost model, autorouter) | [docs/routing.md](docs/routing.md) |
| Undo/Redo, lista komend | [docs/commands.md](docs/commands.md) |
| Persistence, KiCAD I/O | [docs/persistence.md](docs/persistence.md) |
| DRC | [docs/drc.md](docs/drc.md) |
| GUI, tools, multi-selection, wire editing | [docs/gui.md](docs/gui.md) |
| Testy — strategia | [docs/testing.md](docs/testing.md) |
| Benchmarki, profilowanie, architektura optymalizacji | [docs/benchmarks.md](docs/benchmarks.md) |
| Historia optymalizacji wydajności | [docs/performance.md](docs/performance.md) |
| Connectivity (union-find, inkrementalny graf) | [docs/connectivity.md](docs/connectivity.md) |
| Fazy implementacji | [docs/phases.md](docs/phases.md) |

## 5. Struktura katalogów

```
src/protoboard/
├── units.py, geometry.py
├── domain/
│   ├── board/          # boxes.py, layout_engine.py, pads.py, board.py
│   ├── components/     # footprints.py, definition.py, instance.py
│   ├── schematic/      # net.py, schematic.py
│   ├── layout/         # placement.py, wire.py, wire_utils.py, cut.py, group.py, layout.py
│   ├── connectivity/   # graph.py, incremental.py
│   ├── routing_settings.py
│   └── commands/       # base.py, component_cmds.py, group_cmds.py
├── services/
│   ├── drc/            # engine.py, checks.py
│   ├── routing/        # cost_model.py, astar.py, autorouter.py, astar_rs/
│   ├── persistence/    # board_yaml.py, project_yaml.py
│   ├── library/        # loader.py
│   └── kicad/          # parser.py, importer.py, exporter.py
├── gui/
│   ├── main_window.py, session.py, board_view.py
│   ├── renderers/      # board_renderer.py, component_renderer.py, wire_renderer.py
│   ├── dialogs/        # preferences_dialog.py, group_target_dialog.py, pad_conflict_dialog.py
│   ├── panels/         # library_panel.py, properties_panel.py, groups_panel.py, drc_panel.py
│   └── tools/          # select_tool.py, place_tool.py, route_tool.py, cut_tool.py
└── app.py
```

## 6. Konwencje kodu

- `mypy --strict` musi przechodzić. Brak `Any` poza udokumentowanymi miejscami.
- `frozen=True` dataclasses gdziekolwiek możliwe. `@property` dla obliczanych wartości.
- Pure functions w `domain/`; side-effecty tylko w `services/persistence` i `gui/`.
- Naming: `snake_case` funkcje, `PascalCase` klasy, `SCREAMING_SNAKE` stałe.
- Docstringi Google style dla publicznych modułów i klas.
- Custom exceptions dziedziczą z `ProtoboardError` (`protoboard.errors`).
- Logging: `structlog`, domyślnie INFO, `--verbose` → DEBUG.
- Commits: Conventional Commits (`feat:`, `fix:`, `refactor:`, `test:`, `docs:`, `chore:`).

## 7. Decyzje otwarte

1. **Numeracja pinów w obróconym komponencie**: `pin_position()` zwraca pozycję względną w `Footprint`, absolutną liczy `Layout.pin_world_position(component_id, pin_number)`.
2. **Komponenty wystające poza płytkę**: dozwolone z warningiem czy zabronione twardo?
3. **Render izolacji przewodu**: izolowany = ciągła linia; goły = szara z czarnymi end-capami.
4. **Stripboard cuts**: wizualnie jako przerwa między padami (czytelniejsze) czy "X" na padzie?
5. **Format `.net` KiCAD**: legacy XML, new XML, S-expression — zależnie od wersji KiCAD.

## 8. Rekomendacje dla Claude Code

- Pracuj fazami. Nie zaczynaj Fazy N+1 dopóki Faza N nie ma zielonego CI i smoke-testu.
- Każda faza: zaktualizuj `CHANGELOG.md` na początku, commit `chore: complete phase N` na końcu.
- TDD dla layout engine i routing: najpierw failing test.
- Gdy napotkasz decyzję projektową spoza tego dokumentu — **zatrzymaj się i zapytaj**.
- Aktualizuj ten plik i pliki w `docs/` gdy decyzje są podejmowane.
- Każda nowa publiczna API musi mieć test i docstring przed commitem.
