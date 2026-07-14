# SPDX-License-Identifier: MIT
"""body_print_text policy: R/L/C code, other classes raw, unset -> None."""

from __future__ import annotations

import pytest

from protoboard.domain.components.definition import ComponentClass
from protoboard.domain.components.value_text import body_print_text


class TestBodyPrintText:

    def test_resistor_codes(self) -> None:
        assert body_print_text(ComponentClass.RESISTOR, "22k") == "223"

    def test_inductor_codes(self) -> None:
        assert body_print_text(ComponentClass.INDUCTOR, "10") == "100"

    def test_capacitor_codes(self) -> None:
        assert body_print_text(ComponentClass.CAPACITOR_NONPOLAR, "100nF") == "104"

    def test_part_number_passthrough(self) -> None:
        assert body_print_text("transistor_bjt", "BC547") == "BC547"

    @pytest.mark.parametrize("value", ["", "?"])
    def test_unset_none(self, value: str) -> None:
        assert body_print_text("transistor_bjt", value) is None

    def test_unparseable_resistor_falls_back_to_raw(self) -> None:
        # A resistor whose value won't decode still prints something legible.
        assert body_print_text(ComponentClass.RESISTOR, "wat") == "wat"
