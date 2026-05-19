# Recording engine commands in GUI session to use them in headless tests.

## Rationale

Idea jest taka, żeby móc zapisać historię sesji w GUI, kolejne komendy wykonane na modelu danych, tak, by można było ich użyć w headless testach,
łatwiejszy debug przez AI i łatwiejszy sposób raportowania błędów przez innych użytkowników.

## Realizacja (MVP)

Auto-recording wpięty w `CommandStack` — każdy `execute / undo / redo` jest zapisywany
do `<project>/session_commands.yaml`. Brak GUI; działa zawsze gdy sesja ma `project_path`.

### Format

```yaml
version: 1
project: my_project
events:
  - kind: execute
    command:
      type: place_component
      component_id: <uuid>
      placement: {component_id: <uuid>, position: {x: 1.0, y: 2.0}, rotation: 0, side: top}
      instance:  {id: <uuid>, definition_ref: R, refdes: R1, value: 10k}
  - kind: execute
    command:
      type: add_wire
      wire: {id: <uuid>, path: [{x: 0, y: 0}, {x: 1, y: 0}], insulated: true, side: top}
  - kind: undo
  - kind: redo
```

### Komponenty

| Moduł | Rola |
|---|---|
| `services/recording/serialization.py` | Registry `type` → (to_dict, from_dict) dla wszystkich komend (PlaceComponent, MoveComponent, AddWire/AddWireWithNets, SplitStrip, ResizeBoard, net_cmds, group_cmds, MacroCommand) |
| `services/recording/recorder.py` | `CommandRecorder` — append-only YAML log; wpięty jako observer w `CommandStack` |
| `services/recording/replay.py` | `replay_commands(state, events)` — odtwarza w fresh `CommandStack` |
| `services/recording/headless.py` | `ReplayState.from_project()` — lekki kontener bez Qt |
| `tools/replay_commands.py` | CLI: `tools/replay_commands.py <project_dir> [log_path]` |

### Lifecycle

- `app.py::_open_session` → `session.start_recording()` po new/open
- `MainWindow._open_recent_path` → stop poprzedniego, start nowego
- `MainWindow._save_as` → re-anchor recorder do nowej ścieżki
- `MainWindow.closeEvent` → `session.stop_recording()`

### Git integration

Każdy Save tworzy **dwa commity** w projekcie (auto-init przy pierwszym save):

1. **Session log commit** — staguje tylko `session_commands.yaml`. Wiadomość: `Session log: N commands`. Po sukcesie recorder jest resetowany do pustego logu (sekwencja komend od tego commita w przód).
2. **Project snapshot commit** — `git add -A` reszty (board/layout/schematic + wyzerowany log). Wiadomość: `Save: <project_name>`.

Dzięki temu historia git czyta się jako "wykonano N komend → osiągnięto snapshot". Każdy snapshot w git jest pełnym, samowystarczalnym stanem projektu; log między commitami opisuje *drogę* która do niego doprowadziła. Z dowolnego commita można odtworzyć stan projektu (przez checkout) i opcjonalnie replay logu z następnego commita.

Implementacja: `services/persistence/git_repo.py` (cienki wrapper na subprocess + git CLI, bez GitPython), `MainWindow._commit_save`. Błędy git są surfaceowane w status barze bez rollbacku save'a.

### Replay

```python
from protoboard.services.recording import ReplayState, replay_commands
state = ReplayState.from_project(project_dir, library)
result = replay_commands(state, project_dir / "session_commands.yaml")
# result.executed, result.undone, result.redone, result.skipped
```

## Roadmap (poza MVP)

- Asercje w YAML jako osobna sekcja `assertions:` (komponent at pos, connection between pins, DRC issue) — patrz pierwsza wersja dokumentu.
- DRC validation default po replay.
- UI do dodawania asercji.
