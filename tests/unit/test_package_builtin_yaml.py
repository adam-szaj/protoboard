# SPDX-License-Identifier: MIT
"""load_packages: abstract bases are skipped, named packages instantiate.

Uses a self-contained inline catalogue (the abstract bases + a couple of named
parts) rather than the builtin ``assets/packages/generic.yaml`` — the generic
file now carries only abstract bases; placed parts resolve named bodies through
the package-family catalogue (``assets/package_families/``).  These tests guard
the loader + shape builders, which are catalogue-independent.
"""

import math

from protoboard.domain.components.package import (
    BOARD_SOLVE,
    Cylinder,
    MountOrientation,
    Prism,
    load_packages,
)

_CATALOGUE: list[dict] = [
    {"name": "cylinder", "abstract": True, "shape": "cylinder",
     "dims": {"diameter": None, "length": None, "edge_radius": 0.0}},
    {"name": "prism", "abstract": True, "shape": "prism"},
    {"name": "cylinder_axial", "abstract": True, "extends": "cylinder",
     "leads": {"direction": [[0, 0, -1], [0, 0, 1]], "body_offset": 0,
               "board_offset": "solve", "lead_length": None, "lead_r": None,
               "body_eccentricity": 0.0}},
    {"name": "prism_dil", "abstract": True, "extends": "prism",
     "leads": {"direction": [0, 0, -1], "board_offset": "default"},
     "bend": 0.0},
    {"name": "R_Axial_DIN0207", "extends": "cylinder_axial",
     "dims": {"diameter": 2.5, "length": 6.3},
     "leads": {"lead_r": 0.27, "lead_length": 32.0}},
    {"name": "DIP-8", "extends": "prism_dil",
     "dims": {"width": 6.25, "length": 9.20, "height": 4.0},
     "leads": {"body_offset": {"unit": 2.5,
               "value": [[3, [0, 1]], [3, 0], [3, [0, -1]]]}}},
]


def _load() -> dict:
    return load_packages(_CATALOGUE)


def test_abstract_bases_skipped_named_built() -> None:
    pkgs = _load()
    # Named packages instantiate; abstract bases are skipped.
    for name in ("R_Axial_DIN0207", "DIP-8"):
        assert name in pkgs
    for name in ("cylinder", "prism", "cylinder_axial", "prism_dil"):
        assert name not in pkgs


def test_axial_resistor_reproduces_spec() -> None:
    r = _load()["R_Axial_DIN0207"]
    assert isinstance(r, Cylinder)
    assert r.leads.board_offset == BOARD_SOLVE
    rng = r.board_offset_range(orientation=MountOrientation.HORIZONTAL)
    assert math.ceil(rng.r_min_mm / 2.5) == 4


def test_dip8_topology() -> None:
    d = _load()["DIP-8"]
    assert isinstance(d, Prism)
    pins = d.pin_positions()
    assert len(pins) == 8
    assert pins[7].x == 3.0  # right column, beside pin 1
