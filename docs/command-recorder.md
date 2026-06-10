# Command recorder — capturing GUI sessions for headless replay

## Rationale

The recorder captures the stream of `Command`s executed against the model
during a GUI session so that the same sequence can be replayed in headless
tests, used for AI-assisted debugging, or attached to a bug report.

## Implementation (MVP)

Auto-recording is wired into `CommandStack`: every `execute / undo / redo`
is appended to `<project>/session_commands.yaml`. There is no UI — the
recorder is always on whenever the session has a `project_path`.

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
      wire: {id: <uuid>, fragments: [{side: top, path: [{x: 0, y: 0}, {x: 1, y: 0}]}], insulated: true}
  - kind: undo
  - kind: redo
```

### Components

| Module | Role |
|---|---|
| `services/recording/serialization.py` | Registry of `type` → `(to_dict, from_dict)` covering every command (`PlaceComponent`, `MoveComponent`, `AddWire`/`AddWireWithNets`, `MoveDragSet`, `SplitStrip`, `ResizeBoard`, net commands, group commands, `MacroCommand`, …). |
| `services/recording/recorder.py` | `CommandRecorder` — append-only YAML log; attached to `CommandStack` as an observer. |
| `services/recording/replay.py` | `replay_commands(state, events)` — replays into a fresh `CommandStack`. |
| `services/recording/headless.py` | `ReplayState.from_project()` — a lightweight container that does not require Qt. |
| `tools/replay_commands.py` | CLI: `tools/replay_commands.py <project_dir> [log_path]`. |

### Lifecycle

- `app.py::_open_session` → `session.start_recording()` after new / open.
- `MainWindow._open_recent_path` → stop the previous recorder, start a new one.
- `MainWindow._save_as` → re-anchor the recorder to the new path.
- `MainWindow.closeEvent` → `session.stop_recording()`.

### Git integration

Every save creates **two commits** in the project directory (the repo is
auto-initialised on the first save):

1. **Session log commit** — stages `session_commands.yaml` only. Message:
   `Session log: N commands`. After success, the recorder resets to an
   empty log (the next sequence starts from this commit).
2. **Project snapshot commit** — `git add -A` for the rest (board, layout,
   schematic, the freshly emptied log). Message: `Save: <project_name>`.

So the git history reads as "executed N commands → reached this snapshot".
Each snapshot is self-contained; the log between commits describes the
*path* that produced it. From any commit you can check out the project as
it was and optionally replay the log of the following commit.

Implementation: `services/persistence/git_repo.py` (a thin subprocess
wrapper, no GitPython) + `MainWindow._commit_save`. Git errors surface in
the status bar but never roll the save back.

### Replay

```python
from protoboard.services.recording import ReplayState, replay_commands
state = ReplayState.from_project(project_dir, library)
result = replay_commands(state, project_dir / "session_commands.yaml")
# result.executed, result.undone, result.redone, result.skipped
```

## Roadmap (beyond MVP)

- An optional `assertions:` block in the YAML (component at position,
  pins connected, DRC issue present).
- DRC validation by default after replay.
- UI for adding assertions.
