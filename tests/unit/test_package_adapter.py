# SPDX-License-Identifier: MIT
"""Tests for the Package → Footprint adapter."""

from ruamel.yaml import YAML

from protoboard.domain.components.footprints import Footprint
from protoboard.domain.components.package import load_packages
from protoboard.domain.components.package.adapter import PackageFootprint
from protoboard.domain.components.package.solver import MountOrientation
from protoboard.paths import BUILTIN_ASSETS_DIR

_yaml = YAML(typ="safe")


def _builtin() -> dict:
    data = _yaml.load((BUILTIN_ASSETS_DIR / "packages" / "generic.yaml").read_text())
    return load_packages(data["packages"])


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
