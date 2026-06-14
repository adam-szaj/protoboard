# SPDX-License-Identifier: MIT
"""Tests for reach.py — the Package-derived replacement for bendable_pins."""

from ruamel.yaml import YAML

from protoboard.domain.components.package import load_packages
from protoboard.domain.components.package.reach import (
    is_pin_bendable,
    pin_reaches,
)
from protoboard.domain.components.package.solver import RASTER_MM
from protoboard.geometry import Point
from protoboard.paths import BUILTIN_ASSETS_DIR

_yaml = YAML(typ="safe")


def _builtin() -> dict:
    data = _yaml.load((BUILTIN_ASSETS_DIR / "packages" / "generic.yaml").read_text())
    return load_packages(data["packages"])


class TestPinReaches:
    def test_all_pins_present_including_pin1(self) -> None:
        # The ORIGIN is fixed; every pin (incl. pin 1) may bend its lead.
        reaches = pin_reaches(_builtin()["CP_Elec_6.3x11"])
        assert 1 in reaches
        assert 2 in reaches

    def test_default_is_pin_home_origin_is_zero(self) -> None:
        c = _builtin()["CP_Elec_6.3x11"]
        reaches = pin_reaches(c)
        assert reaches[2].default == c.pin_positions()[1]
        assert reaches[2].origin == Point(0.0, 0.0)   # fixed origin
        assert reaches[1].origin == Point(0.0, 0.0)

    def test_electrolytic_reachable_within_bend(self) -> None:
        c = _builtin()["CP_Elec_6.3x11"]  # ring 1..5 holes from ORIGIN [0,0]
        reach = pin_reaches(c)[2]
        # measured from the fixed origin at (0,0):
        assert reach.is_reachable(Point(5.0, 0.0), raster_mm=RASTER_MM) is True   # 5 holes = r_max
        assert reach.is_reachable(Point(1.0, 0.0), raster_mm=RASTER_MM) is True   # 1 hole = r_min
        assert reach.is_reachable(Point(0.0, 0.0), raster_mm=RASTER_MM) is False  # on origin
        assert reach.is_reachable(Point(6.0, 0.0), raster_mm=RASTER_MM) is False  # beyond r_max


class TestIsPinBendable:
    def test_rigid_dip_not_bendable(self) -> None:
        d = _builtin()["DIP-8"]   # bend=0 → rigid
        assert not is_pin_bendable(d, 2)
        assert not is_pin_bendable(d, 8)

    def test_electrolytic_bendable(self) -> None:
        c = _builtin()["CP_Elec_6.3x11"]  # bend=4
        assert is_pin_bendable(c, 2)

    def test_axial_solver_bendable(self) -> None:
        r = _builtin()["R_Axial_DIN0207"]  # solver, lead_length 32 → wide ring
        assert is_pin_bendable(r, 2)

    def test_to92_bendable(self) -> None:
        t = _builtin()["TO-92"]   # bend 3.54
        assert is_pin_bendable(t, 2)
        assert is_pin_bendable(t, 3)

    def test_unknown_pin_not_bendable(self) -> None:
        assert not is_pin_bendable(_builtin()["DIP-8"], 99)
