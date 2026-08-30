# SPDX-License-Identifier: MIT
"""ParametricPinsDialog — placement-time pin-count collection."""

from __future__ import annotations

from pathlib import Path

from protoboard.domain.components.package.kind import KIND_GENERATORS
from protoboard.gui.dialogs.parametric_pins_dialog import ParametricPinsDialog
from protoboard.services.library.loader import _component_from_dict


def _pin_header_base(families: dict):
    return _component_from_dict(
        {
            "name": "PinHeader",
            "component_class": "connector",
            "package": {"kind": "pin_header"},
        },
        Path("test.yaml"),
        families,
        families,
    )


def _dialog(qtbot) -> ParametricPinsDialog:
    families: dict = {}
    dlg = ParametricPinsDialog(
        _pin_header_base(families),
        KIND_GENERATORS["pin_header"](),
        families,
    )
    qtbot.addWidget(dlg)
    return dlg


class TestParametricPinsDialog:
    def test_offers_count_and_rows(self, qtbot) -> None:
        dlg = _dialog(qtbot)
        assert dlg._count.value() == 4  # generator preview count
        assert "rows" in dlg._param_spins

    def test_accept_builds_concrete_definition(self, qtbot) -> None:
        dlg = _dialog(qtbot)
        dlg._count.setValue(6)
        dlg._param_spins["rows"].setValue(2)
        dlg._try_accept()
        concrete = dlg.concrete_definition()
        assert concrete is not None
        assert concrete.name == "PinHeader_2x03"
        assert len(concrete.meta_pins) == 6
        assert not concrete.parametric_pins

    def test_unrealisable_combination_keeps_dialog_open(self, qtbot) -> None:
        dlg = _dialog(qtbot)
        dlg._count.setValue(5)
        dlg._param_spins["rows"].setValue(2)  # odd count on a 2xN header
        dlg._try_accept()
        assert dlg.concrete_definition() is None
        assert dlg._error.isVisible() or dlg._error.text()
