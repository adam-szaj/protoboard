# SPDX-License-Identifier: MIT
"""Tests for the package-family catalogue loader."""

from __future__ import annotations

from pathlib import Path

from protoboard.domain.components.package.family import PackageFamily
from protoboard.domain.components.package.shapes import Cylinder, Disc, Prism
from protoboard.services.library.package_family_loader import load_package_families


class TestBuiltinCatalogue:
    def test_loads_builtin_families(self) -> None:
        fams = load_package_families()
        # The ported catalogue carries the legacy bodies.
        assert "DO-35" in fams
        assert "DIN-0207" in fams
        assert "Ceramic-Disc-D5" in fams
        assert "LED-5mm" in fams
        assert "HC-49" in fams

    def test_every_variant_builds(self) -> None:
        for fam in load_package_families().values():
            assert isinstance(fam, PackageFamily)
            for v in fam.variants:
                # A real package with usable geometry, not a render-only stub.
                v.package.bbox()
                v.package.pin_positions()

    def test_do35_default_and_colour(self) -> None:
        do35 = load_package_families()["DO-35"]
        assert do35.default_variant().variant_id == "typ"
        pkg = do35.default_variant().package
        assert isinstance(pkg, Cylinder)
        assert pkg.color == "#f4ece0"

    def test_shapes_map_correctly(self) -> None:
        fams = load_package_families()
        assert isinstance(fams["DO-35"].default_variant().package, Cylinder)
        assert isinstance(fams["Ceramic-Disc-D5"].default_variant().package, Disc)
        assert isinstance(fams["HC-49"].default_variant().package, Prism)

    def test_disc_profile_override(self) -> None:
        from protoboard.domain.components.package.body import DiscProfile
        tant = load_package_families()["Tantalum-Drop-D3.5"].default_variant()
        assert tant.package.profile is DiscProfile.CANDY  # type: ignore[attr-defined]


class TestLoaderRobustness:
    def test_missing_directory_returns_empty(self, tmp_path: Path) -> None:
        assert load_package_families(tmp_path / "nope") == {}

    def test_malformed_file_is_skipped(self, tmp_path: Path) -> None:
        good = tmp_path / "good.yaml"
        good.write_text(
            "families:\n"
            "  - family: X\n"
            "    extends: cylinder_axial\n"
            "    color: '#abcdef'\n"
            "    variants:\n"
            "      - id: typ\n"
            "        dims: {diameter: 2.0, length: 4.0}\n"
            "        leads: {lead_r: 0.2, lead_length: 6.0}\n",
            encoding="utf-8",
        )
        bad = tmp_path / "bad.yaml"
        bad.write_text("this: is not a families list\n", encoding="utf-8")
        fams = load_package_families(tmp_path)
        assert "X" in fams           # good file still loads
        assert len(fams) == 1        # bad file skipped, not fatal

    def test_variant_missing_id_raises_for_that_file(self, tmp_path: Path) -> None:
        # A family whose variant lacks an id is malformed → that file is skipped.
        f = tmp_path / "f.yaml"
        f.write_text(
            "families:\n"
            "  - family: Y\n"
            "    extends: cylinder_axial\n"
            "    variants:\n"
            "      - dims: {diameter: 2.0, length: 4.0}\n",
            encoding="utf-8",
        )
        assert load_package_families(tmp_path) == {}
