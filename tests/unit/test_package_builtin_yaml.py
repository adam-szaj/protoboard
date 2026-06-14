# SPDX-License-Identifier: MIT
"""Loads the builtin packages YAML and checks all named packages instantiate."""

import math

from ruamel.yaml import YAML

from protoboard.domain.components.package import (
    BOARD_SOLVE,
    Cylinder,
    MountOrientation,
    Prism,
    load_packages,
)
from protoboard.paths import BUILTIN_ASSETS_DIR

_yaml = YAML(typ="safe")


def _load_builtin() -> dict:
    path = BUILTIN_ASSETS_DIR / "packages" / "generic.yaml"
    data = _yaml.load(path.read_text())
    return load_packages(data["packages"])


def test_all_named_packages_build() -> None:
    pkgs = _load_builtin()
    # Abstract bases are skipped; named ones present.
    for name in ("DO-35", "R_Axial_DIN0207", "CP_Elec_6.3x11", "TO-92", "DIP-8"):
        assert name in pkgs
    for name in ("cylinder", "prism", "cylinder_axial", "prism_dil"):
        assert name not in pkgs


def test_resistor_reproduces_spec() -> None:
    pkgs = _load_builtin()
    r = pkgs["R_Axial_DIN0207"]
    assert isinstance(r, Cylinder)
    assert r.leads.board_offset == BOARD_SOLVE
    rng = r.board_offset_range(orientation=MountOrientation.HORIZONTAL)
    assert math.ceil(rng.r_min_mm / 2.5) == 4


def test_dip8_topology() -> None:
    pkgs = _load_builtin()
    d = pkgs["DIP-8"]
    assert isinstance(d, Prism)
    pins = d.pin_positions()
    assert len(pins) == 8
    assert pins[7].x == 3.0  # right column, beside pin 1
