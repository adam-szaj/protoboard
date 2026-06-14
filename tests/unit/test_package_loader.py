# SPDX-License-Identifier: MIT
"""Tests for the package loader: extends/null/direction/run-length → shapes.

End-to-end from YAML-like dicts (as ruamel would produce) to Package instances,
reproducing the worked examples in ``docs/footprints/spec.md``.
"""

import math

import pytest

from protoboard.domain.components.package.loader import (
    PackageDefError,
    expand_direction,
    load_packages,
    resolve_extends,
)
from protoboard.domain.components.package.shapes import (
    BOARD_SOLVE,
    Cylinder,
    Prism,
)
from protoboard.domain.components.package.solver import MountOrientation
from protoboard.geometry import Point


def _pt(x: float, y: float) -> Point:
    return Point(float(x), float(y))


# Generic bases shared by the worked examples (mirrors spec's generics).
GENERICS = [
    {"name": "cylinder", "abstract": True, "shape": "cylinder",
     "dims": {"diameter": None, "length": None}},
    {"name": "prism", "abstract": True, "shape": "prism"},
    {"name": "cylinder_axial", "abstract": True, "extends": "cylinder",
     "leads": {"direction": [[0, 0, -1], [0, 0, 1]], "body_offset": 0,
               "board_offset": "solve"}},
    {"name": "cylinder_radial", "abstract": True, "extends": "cylinder",
     "leads": {"direction": [[0, 0, -1], [0, 0, -1]],
               "board_offset": "default"}, "bend": 0.0},
    {"name": "prism_dil", "abstract": True, "extends": "prism",
     "leads": {"direction": [0, 0, -1], "board_offset": "default"},
     "bend": 0.0},
]


class TestExpandDirection:
    def test_single_vector_all_pins(self) -> None:
        assert expand_direction([0, 0, -1], 3) == [(0, 0, -1)] * 3

    def test_per_pin_list(self) -> None:
        assert expand_direction([[0, 0, -1], [0, 0, 1]], 2) == [
            (0, 0, -1), (0, 0, 1)
        ]

    def test_grouped(self) -> None:
        raw = [[[1, 2, 3, 4], [0, 0, -1]], [[5, 6, 7, 8], [0, 0, 1]]]
        out = expand_direction(raw, 8)
        assert out[:4] == [(0, 0, -1)] * 4
        assert out[4:] == [(0, 0, 1)] * 4

    def test_grouped_missing_pin_raises(self) -> None:
        with pytest.raises(ValueError):
            expand_direction([[[1, 2], [0, 0, -1]]], 3)


class TestResolveExtends:
    def test_merge_chain(self) -> None:
        reg = {d["name"]: d for d in GENERICS}
        reg["DO-35"] = {"name": "DO-35", "extends": "cylinder_axial",
                        "dims": {"diameter": 2.0, "length": 4.0},
                        "leads": {"lead_r": 0.27}}
        merged = resolve_extends("DO-35", reg)
        assert merged["dims"]["diameter"] == 2.0
        assert merged["leads"]["board_offset"] == "solve"  # from cylinder_axial
        assert merged["leads"]["lead_r"] == 0.27            # from DO-35
        assert "extends" not in merged

    def test_cyclic_raises(self) -> None:
        reg = {"a": {"name": "a", "extends": "b"},
               "b": {"name": "b", "extends": "a"}}
        with pytest.raises(PackageDefError):
            resolve_extends("a", reg)


class TestLoadWorkedExamples:
    def _load(self, extra: list[dict]) -> dict:
        return load_packages(GENERICS + extra)

    def test_resistor_axial(self) -> None:
        pkgs = self._load([
            {"name": "R_Axial_DIN0207", "extends": "cylinder_axial",
             "dims": {"diameter": 2.5, "length": 6.3},
             "leads": {"lead_r": 0.27, "lead_length": 32.0}},
        ])
        r = pkgs["R_Axial_DIN0207"]
        assert isinstance(r, Cylinder)
        assert r.leads.board_offset == BOARD_SOLVE
        rng = r.board_offset_range(orientation=MountOrientation.HORIZONTAL)
        assert math.ceil(rng.r_min_mm / 2.5) == 4
        assert math.ceil(rng.r_min_mm / 2.5) == 4

    def test_electrolytic_default_and_bend(self) -> None:
        pkgs = self._load([
            {"name": "CP_Elec_6.3x11", "extends": "cylinder_radial",
             "dims": {"diameter": 6.3, "length": 11.0},
             "leads": {"body_offset": {"unit": 2.5, "value": [[1, 0]]}},
             "bend": 4.0},
        ])
        c = pkgs["CP_Elec_6.3x11"]
        assert c.pin_positions() == [_pt(0, 0), _pt(1, 0)]
        rng = c.board_offset_range()
        assert rng.r_min_mm == pytest.approx(2.5)
        assert rng.r_max_mm == pytest.approx(2.5 + 4 * 2.5)

    def test_to92_three_pins(self) -> None:
        pkgs = self._load([
            {"name": "TO-92", "extends": "prism",
             "dims": {"width": 4.5, "length": 4.5, "height": 5.2},
             "leads": {"direction": [[0, 0, -1], [0, 0, -1], [0, 0, -1]],
                       "body_offset": {"unit": 1.25, "value": [[1, 0], [1, 0]]},
                       "board_offset": "default"},
             "bend": 3.54},
        ])
        t = pkgs["TO-92"]
        assert isinstance(t, Prism)
        assert t.pin_count() == 3
        # body 1.25mm in grid domain = 0.5 hole steps
        assert t.pin_positions() == [_pt(0, 0), _pt(0.5, 0), _pt(1.0, 0)]

    def test_dip8_runlength_and_topology(self) -> None:
        pkgs = self._load([
            {"name": "DIP-8", "extends": "prism_dil",
             "dims": {"width": 9.8, "length": 10.2, "height": 4.0},
             "leads": {"body_offset": {"unit": 2.5,
                       "value": [[3, [0, 1]], [3, 0], [3, [0, -1]]]}}},
        ])
        d = pkgs["DIP-8"]
        pins = d.pin_positions()
        assert len(pins) == 8
        assert pins[4] == _pt(3, 3)  # column jump
        assert pins[7] == _pt(3, 0)

    def test_devboard_margin_css_two_values(self) -> None:
        pkgs = self._load([
            {"name": "WeAct", "extends": "prism_dil",
             "margin": [0, 2.5],
             "leads": {"body_offset": {"unit": 2.5, "value": [[0, 4]]}}},
        ])
        w = pkgs["WeAct"]
        assert isinstance(w, Prism)
        bb = w.bbox()
        assert bb.height == pytest.approx(4.0)   # 0 vertical margin
        assert bb.width == pytest.approx(2.0)    # 1+1 hole horizontal margin

    def test_abstract_not_built(self) -> None:
        pkgs = self._load([])
        assert "cylinder" not in pkgs        # abstract base
        assert "cylinder_axial" not in pkgs  # abstract intermediate
        assert "prism_dil" not in pkgs       # abstract intermediate

    def test_required_null_raises(self) -> None:
        # cylinder needs diameter/length; leaving them null must raise.
        with pytest.raises(PackageDefError):
            load_packages(GENERICS + [
                {"name": "Bad", "extends": "cylinder_axial",
                 "leads": {"lead_r": 0.2, "lead_length": 5.0}},
            ])
