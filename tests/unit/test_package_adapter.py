# SPDX-License-Identifier: MIT
"""Tests for the Package → Footprint adapter."""

from protoboard.domain.components.footprints import Footprint
from protoboard.domain.components.package import load_packages
from protoboard.domain.components.package.adapter import PackageFootprint
from protoboard.domain.components.package.solver import MountOrientation

# Self-contained fixtures — the abstract bases plus the two parts these adapter
# tests exercise (a rigid DIP and an axial resistor).  Inline so the test owns
# its geometry rather than depending on named packages in the builtin
# generic.yaml (placed parts resolve through the package-family catalogue).
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


def _builtin() -> dict:
    return load_packages(_CATALOGUE)


def test_is_footprint_template() -> None:
    pkg = _builtin()["DIP-8"]
    fp = PackageFootprint(pkg)
    assert isinstance(fp, Footprint)


def test_pin_count_and_positions_match_package() -> None:
    pkg = _builtin()["DIP-8"]
    fp = PackageFootprint(pkg)
    assert fp.pin_count() == pkg.pin_count() == 8
    assert fp.pin_positions() == pkg.pin_positions()


def test_orientation_threaded_to_package() -> None:
    # Axial resistor: horizontal vs vertical give different pin-2 spacing.
    r = _builtin()["R_Axial_DIN0207"]
    h = PackageFootprint(r, MountOrientation.HORIZONTAL).pin_positions()
    v = PackageFootprint(r, MountOrientation.VERTICAL).pin_positions()
    assert h[1].x > v[1].x  # horizontal spacing wider than vertical


def test_pin1_at_origin() -> None:
    # Pin 1 sits at the local origin for every package (generic invariant).
    fp = PackageFootprint(_builtin()["DIP-8"])
    assert fp.pin_positions()[0].x == 0.0
    assert fp.pin_positions()[0].y == 0.0
