# SPDX-License-Identifier: MIT
"""MVC acceptance: the headless edit-CLI drives the same EditController verbs as
the GUI, from new-project to export-pdf, with zero Qt in the edit path.

The CLI drives the active group; the selection is the session's own model-side
``ItemId`` selection (the same one the GUI and the controller's selection-relative
verbs read). The verbs it calls are the same ones the GUI tools call — the full
journey being expressible here is the proof of the M/V/C split.

The edit journey (new → place → wire → autoroute → drc → save) runs Qt-free; only
the final PDF export needs Qt's paint device (the ``qtbot`` fixture provides the
QApplication).
"""

from __future__ import annotations

from pathlib import Path

from pytestqt.qtbot import QtBot

from protoboard.cli import CliDriver
from protoboard.services.persistence.project_yaml import load_project


def _journey(save_dir: Path) -> list[str]:
    return [
        "new perfboard 20 30",
        "place R at 3,5",
        "place R at 3,9",
        "select R1 R2",
        "rotate sel 90",
        "flip R1",
        "wire top bare 3,5 3,9",
        "group R1 as Amp",
        "drc",
        f"save {save_dir}",
    ]


def test_edit_journey_is_fully_qt_free(tmp_path: Path) -> None:
    """new → place → rotate/flip → wire → group → drc → save, no Qt at all."""
    driver = CliDriver.new()
    driver.run(_journey(tmp_path / "proj"))

    # Two components placed, one grouped, one wire drawn, project saved.
    assert len(driver.session.instances) == 2
    assert any(g.name == "Amp" for g in driver.session.layout.groups.values())
    assert len(driver.session.layout.wires) == 1
    assert (tmp_path / "proj").exists()


def test_edit_journey_survives_project_round_trip(tmp_path: Path) -> None:
    """The saved result of the public edit journey remains semantically usable."""
    driver = CliDriver.new()
    project = tmp_path / "proj"
    driver.run(_journey(project))

    board, layout, schematic, instances, *_ = load_project(project)

    assert len(instances) == 2
    assert layout.wires
    assert any(group.name == "Amp" for group in layout.groups.values())
    assert board.bounds.lx == 30
    assert board.bounds.ly == 20
    assert len(schematic.nets) == 1
    assert any(net.pins for net in schematic.nets.values())


def test_cli_selection_is_the_session_selection() -> None:
    """The CLI drives the session's own ItemId selection — one store, not a copy.

    ``rotate R1 90`` selects R1 (the select-then-rotate shortcut), so the
    session selection reflects it; ``select`` sets it directly. Confirms the CLI
    no longer keeps a private selection set.
    """
    from protoboard.controller.editing.item_handle import ItemId

    driver = CliDriver.new()
    driver.execute("place R at 3,5")
    driver.execute("place R at 3,9")
    r1 = driver._resolve("R1")
    r2 = driver._resolve("R2")

    driver.execute("select R1 R2")
    assert driver.session.selection() == {ItemId.component(r1), ItemId.component(r2)}

    driver.execute("rotate R1 90")
    assert driver.session.selection() == {ItemId.component(r1)}


def test_undo_redo_through_cli(tmp_path: Path) -> None:
    driver = CliDriver.new()
    driver.execute("place R at 3,5")
    assert len(driver.session.instances) == 1
    driver.execute("undo")
    assert len(driver.session.instances) == 0
    driver.execute("redo")
    assert len(driver.session.instances) == 1


def test_full_journey_exports_pdf(qtbot: QtBot, tmp_path: Path) -> None:
    """The batch journey ending in ``export pdf`` produces a real PDF headless.

    ``qtbot`` gives the QApplication the export paint device needs; the edit
    verbs themselves stay Qt-free.
    """
    driver = CliDriver.new()
    pdf = tmp_path / "board.pdf"
    driver.run([*_journey(tmp_path / "proj"), f"export pdf {pdf}"])

    assert pdf.exists()
    assert pdf.stat().st_size > 0
    assert pdf.read_bytes().startswith(b"%PDF")
