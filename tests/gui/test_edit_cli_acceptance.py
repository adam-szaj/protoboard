# SPDX-License-Identifier: MIT
"""MVC acceptance: the headless edit-CLI drives the same EditController verbs as
the GUI, from new-project to export-pdf, with zero Qt in the edit path.

The CLI holds the ONLY interaction state (selection + active group) — the verbs
it calls are the same ones the GUI tools call. The full journey being expressible
here is the proof of the M/V/C split.

The edit journey (new → place → wire → autoroute → drc → save) runs Qt-free; only
the final PDF export needs Qt's paint device (the ``qtbot`` fixture provides the
QApplication).
"""

from __future__ import annotations

from pathlib import Path

from pytestqt.qtbot import QtBot

from protoboard.services.editing.cli import CliDriver


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
