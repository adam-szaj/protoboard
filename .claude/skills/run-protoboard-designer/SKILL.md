---
name: run-protoboard-designer
description: Run, launch, smoke-test or screenshot the Protoboard Designer desktop app (PySide6 GUI). Headless driver launches the real MainWindow offscreen with a fixture project, drives selection/zoom/camera/mirror/[?]-overlay/rebuild and saves screenshots. Use for "run the app", "start protoboard", "check the GUI works", "take a screenshot".
---

# Run Protoboard Designer

PySide6 (Qt 6) desktop app. All paths below are relative to the repo root.
The agent path is a committed driver that launches the **real**
`MainWindow` (same startup as `protoboard.app.main`: routing plugins, app
settings, component library) headlessly and drives one user flow with
screenshots — no display, no window manager, no xvfb needed.

## Prerequisites

Nothing beyond the project venv. `uv run` resolves it; PySide6 (incl.
`QtTest`) is a base dependency. No `apt-get` was needed on this machine.

## Run (agent path) — the driver

```bash
uv run python .claude/skills/run-protoboard-designer/driver.py
```

- Exit code 0 = all steps passed; each step prints `PASS <name>` and a
  `SNAP <path>` line. Non-zero → the failing steps are listed in the
  final `RESULT: FAILURES: [...]` line.
- Screenshots land in `$PROTOBOARD_SHOTS_DIR` (default
  `/tmp/protoboard-driver-shots/`), numbered `01_initial.png` …
  `11_full_rebuild.png`. **Read them** (the Read tool renders PNG) —
  `01_initial` must show the board with 8 JST connectors and coloured
  nets; a grey frame means the scene did not build.
- The flow covers every BoardView service: scene build (composer),
  panel→board selection with cyan outline + net dimming (highlight),
  zoom/fit/camera-side/mirror (camera), `[?]` overlay via a real
  Shift+/ key press and Esc close (tool host), full board-layer
  rebuild, and screenshots exercise the paint pipeline.
- The driver is read-only: it loads `tests/fixtures/tangled` directly
  (bypassing the Welcome dialog), and deliberately skips
  `recent_projects.add()` and `session.start_recording()` so it never
  touches the user's config or writes recording files.

To exercise something else (a new tool, a dialog), extend the driver
with another `step("name", fn)` — steps assert + screenshot + report.

## Run (human path)

```bash
uv run protoboard          # real display; opens the Welcome dialog
uv run protoboard -v       # verbose (DEBUG) logging
```

Blocks on the Welcome dialog (`WelcomeDialog.exec()`), so it is useless
headless — that is exactly why the driver builds the session itself.

## Test

```bash
uv run --extra dev pytest tests/gui -q -p no:randomly    # ~2.5 min, 1237 tests
```

(Tests need `--extra dev` for pytest-qt; the driver does not.)

## Gotchas

- **`QT_QPA_PLATFORM` must be set before `import protoboard`** — the
  package `__init__` does `setdefault("QT_QPA_PLATFORM", "xcb")`, which
  needs a real X display. The driver hard-sets `offscreen` first.
- **Worktree trap:** a stale editable install can make `protoboard`
  import from the MAIN checkout instead of the worktree you're editing.
  The driver prints `protoboard package: <path>` and warns on mismatch —
  check that line first when a change "does nothing".
- **`main()` cannot be driven directly** — it blocks in
  `WelcomeDialog.exec()` before the window exists. The driver replays
  the same startup sequence (plugins → settings → library →
  `load_project` with `set_migration_library/families` → `EditorSession`
  → `MainWindow`) without the dialog.
- **Benign noise** you will see and can ignore:
  `library.unknown_component_class ... component_class=led_bar`
  (open-tag classes load as OTHER by design) and offscreen's
  `This plugin does not support propagateSizeHints()`.
- `uv` prints `VIRTUAL_ENV=venv does not match ... .venv` — harmless;
  it targets the project `.venv` regardless.
- Blank-frame detection: `window.grab()` succeeds even when nothing
  painted, so the driver samples a 40×40 grid and fails a step whose
  screenshot has <8 distinct colours.

## Troubleshooting

- `ModuleNotFoundError: pytestqt` when running **tests** → you forgot
  `--extra dev` (the dev extras are in `[project.optional-dependencies]`,
  not the default group).
- Driver hangs → almost certainly a modal dialog opened (`QMessageBox`
  in `_open_session` error paths, or a tool dialog). Run with a real
  display to see it, or check the last `PASS`/log line to locate the step.
