# SPDX-License-Identifier: MIT
import pathlib
import re

CONTROLLER = pathlib.Path(__file__).parents[2] / "src" / "protoboard" / "controller"


def test_controller_does_not_import_gui():
    offenders = []
    for py in CONTROLLER.rglob("*.py"):
        for m in re.finditer(
            r"^\s*(from|import)\s+protoboard\.gui", py.read_text(), re.MULTILINE
        ):
            offenders.append(f"{py}: {m.group(0)}")
    assert not offenders, "controller/ imports gui:\n" + "\n".join(offenders)
