# SPDX-License-Identifier: MIT
"""Headless smoke driver for the Protoboard Designer desktop app.

Launches the REAL application (same startup path as ``protoboard.app.main``:
routing plugins, app settings, component library, ``MainWindow``) under
``QT_QPA_PLATFORM=offscreen``, loads the ``tests/fixtures/tangled`` project
instead of the blocking WelcomeDialog, then drives one user flow end-to-end:

  select component → zoom → fit → camera bottom/top → mirror →
  [?] shortcuts overlay via a real key press → full scene rebuild

A screenshot is saved after every step; each is checked against a
"blank frame" heuristic.  Exit code 0 = every step passed.

Usage (from the repo root):

    uv run python .claude/skills/run-protoboard-designer/driver.py

Screenshots land in ``$PROTOBOARD_SHOTS_DIR`` (default:
``$TMPDIR/protoboard-driver-shots``); the path of every shot is printed.
"""
from __future__ import annotations

import os
import sys
import traceback
from pathlib import Path

# Must be set BEFORE importing protoboard — the package __init__ defaults
# QT_QPA_PLATFORM to xcb, which needs a real X display.
os.environ["QT_QPA_PLATFORM"] = "offscreen"

SHOTS = Path(
    os.environ.get("PROTOBOARD_SHOTS_DIR")
    or Path(os.environ.get("TMPDIR", "/tmp")) / "protoboard-driver-shots"
)
SHOTS.mkdir(parents=True, exist_ok=True)

import logging

import protoboard

print(f"protoboard package: {protoboard.__file__}")
expected_root = Path(__file__).resolve().parents[3]
actual_root = Path(protoboard.__file__).resolve().parents[2]
if expected_root != actual_root:
    # Worktree trap: a stale editable install can shadow this checkout's src.
    print(f"WARNING: importing from {actual_root}, driver lives in {expected_root}")

from protoboard.logging_config import configure_logging

configure_logging(verbose=False, module_overrides={"protoboard.gui": logging.INFO})

# Real startup side-effects: routers + plugin discovery (as in app.main()).
import protoboard.services.routing.builtin  # noqa: F401
import protoboard.services.routing.builtin_enhanced  # noqa: F401
import protoboard.services.routing.builtin_optimizers  # noqa: F401
from protoboard.services.routing.discovery import discover_all_plugins

discover_all_plugins()

from PySide6.QtCore import Qt
from PySide6.QtTest import QTest
from PySide6.QtWidgets import QApplication

app = QApplication([sys.argv[0]])
app.setApplicationName("Protoboard Designer")

from protoboard.controller.app_settings import load_app_settings
from protoboard.gui.main_window import MainWindow
from protoboard.gui.renderers.packages import load_renderer_plugins
from protoboard.gui.session import EditorSession
from protoboard.paths import BUILTIN_LIBRARY_DIR
from protoboard.services.library.loader import load_library
from protoboard.services.library.package_family_loader import (
    load_full_package_catalogue,
)
from protoboard.services.library.plugin_discovery import load_generator_plugins
from protoboard.services.persistence.project_yaml import (
    load_project,
    set_migration_families,
    set_migration_library,
)

app_settings = load_app_settings()
library_dirs = [BUILTIN_LIBRARY_DIR] if BUILTIN_LIBRARY_DIR.exists() else []
load_renderer_plugins(library_dirs)
load_generator_plugins(library_dirs)
library = load_library(library_dirs, load_full_package_catalogue())

set_migration_library(library)
set_migration_families(load_full_package_catalogue())
project_dir = (expected_root / "tests" / "fixtures" / "tangled").resolve()
(board, layout, schematic, instances,
 routing_settings, layer_overrides, axis_label_settings) = load_project(project_dir)

session = EditorSession(board=board, library=library)
session.layout = layout
session.schematic = schematic
session.instances = instances
session.routing_settings = routing_settings
session.axis_label_settings = axis_label_settings
session.layer_overrides = layer_overrides
# No recent_projects.add / start_recording — read-only smoke run.

window = MainWindow(session, app_settings=app_settings)
window.resize(1500, 950)
window.show()
QTest.qWait(300)

view = window._board_view
failures: list[str] = []
_step_n = 0


def snap(name: str) -> None:
    global _step_n
    _step_n += 1
    path = SHOTS / f"{_step_n:02d}_{name}.png"
    pix = window.grab()
    pix.save(str(path))
    img = pix.toImage()
    colors = {
        img.pixel(x, y)
        for x in range(0, img.width(), max(1, img.width() // 40))
        for y in range(0, img.height(), max(1, img.height() // 40))
    }
    blank = len(colors) < 8
    print(f"SNAP {path}: {img.width()}x{img.height()} "
          f"distinct_colors={len(colors)}{' BLANK!' if blank else ''}")
    if blank:
        failures.append(f"{name}: screenshot looks blank")


def step(name: str, fn) -> None:
    try:
        fn()
        QTest.qWait(150)
        snap(name)
        print(f"PASS {name}")
    except Exception as exc:  # noqa: BLE001
        traceback.print_exc()
        failures.append(f"{name}: {exc}")
        print(f"FAIL {name}: {exc}")


# -- step 0: initial state ---------------------------------------------------
n_items = len(view.scene().items())
n_comps = len(session.layout.placements)
n_wires = len(session.layout.wires)
print(f"scene_items={n_items} placements={n_comps} wires={n_wires} "
      f"active_tool={type(view.active_tool).__name__}")
if n_items < 10:
    failures.append("scene nearly empty after startup refresh")
snap("initial")

# -- highlight service: panel->board selection ---------------------------------
comp_id = next(iter(session.layout.placements))


def do_select() -> None:
    session.request_select_component(comp_id)
    assert view.tools.active_tool is not None
    sel = getattr(view.tools.active_tool, "_selected_ids", set())
    assert comp_id in sel, f"component {comp_id} not selected: {sel}"


step("select_component", do_select)

# -- camera service -------------------------------------------------------------
step("zoom_in", lambda: view.zoom_by(1.6))
step("fit_board", lambda: view.fit_board())


def do_camera_bottom() -> None:
    view.toggle_camera()
    assert view.camera_side == "bottom", view.camera_side


step("camera_bottom", do_camera_bottom)
step("camera_top_again", lambda: view.toggle_camera())


def do_mirror() -> None:
    view.toggle_mirror_h()
    assert view.is_mirrored


step("mirror_h", do_mirror)
step("mirror_off", lambda: view.toggle_mirror_h())

# -- tool host: [?] overlay through the real key path -----------------------------


def do_overlay_open() -> None:
    view.setFocus()
    QTest.keyClick(view, Qt.Key.Key_Question, Qt.KeyboardModifier.ShiftModifier)
    assert view.tools._shortcuts_overlay_visible(), "[?] overlay did not open"


step("shortcuts_overlay", do_overlay_open)


def do_overlay_close() -> None:
    QTest.keyClick(view, Qt.Key.Key_Escape)
    assert not view.tools._shortcuts_overlay_visible(), "[?] overlay did not close"


step("overlay_closed", do_overlay_close)

# -- composer: force a full scene rebuild -----------------------------------------


def do_rebuild() -> None:
    before = len(view.scene().items())
    view.mark_board_layer_dirty()
    view.refresh()
    after = len(view.scene().items())
    assert after >= 10, f"scene collapsed after rebuild: {after} items"
    print(f"rebuild: {before} -> {after} scene items")


step("full_rebuild", do_rebuild)

window.close()
print("RESULT:", "OK" if not failures else f"FAILURES: {failures}")
sys.exit(0 if not failures else 1)
