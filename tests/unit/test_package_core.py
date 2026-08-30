# SPDX-License-Identifier: MIT
"""Tests for the parametric package core: offset normalisation + axial solver.

Values are checked against the worked examples in ``docs/footprints/spec.md``
(resistor DIN0207, electrolytic, TO-92, DIP-8) — the model must reproduce the
hand-tuned numbers from physics.
"""

import math

import pytest
from geometry_utils import _pt, _ve

from protoboard.domain.components.package.offsets import (
    cumulative_positions,
    expand_offsets,
    scale_offsets,
)
from protoboard.domain.components.package.solver import (
    RASTER_MM,
    MountOrientation,
    OffsetRange,
    axial_board_offset_range,
)
from protoboard.geometry import Vector


class TestExpandOffsets:
    def test_bare_vectors(self) -> None:
        assert expand_offsets([[0, 1], [3, 0]]) == [_ve(0, 1), _ve(3, 0)]

    def test_run_length(self) -> None:
        assert expand_offsets([[3, [0, 1]]]) == [_ve(0, 1), _ve(0, 1), _ve(0, 1)]

    def test_dip8_mixed_form(self) -> None:
        # DIP-8: 3×↓, single column-jump, 3×↑ — the spec's canonical example.
        raw = [[3, [0, 1]], [3, 0], [3, [0, -1]]]
        expanded = expand_offsets(raw)
        assert expanded == [
            _ve(0, 1),
            _ve(0, 1),
            _ve(0, 1),
            _ve(3, 0),
            _ve(0, -1),
            _ve(0, -1),
            _ve(0, -1),
        ]
        assert len(expanded) == 7  # N-1 for 8 pins

    def test_run_length_count_must_be_positive(self) -> None:
        with pytest.raises(ValueError):
            expand_offsets([[0, [0, 1]]])

    def test_bare_vector_needs_two_numbers(self) -> None:
        with pytest.raises(ValueError):
            expand_offsets([[1, 2, 3]])

    def test_single_vector_sugar(self) -> None:
        # Spec §2.3: a 2-pin component may give its single pair as a bare
        # vector — [1, 0] ≡ [[1, 0]] (both top-level elements are numbers,
        # so the form is unambiguous). This is the LED-family regression:
        # {unit: 1.25, value: [1, 0]} used to abort the whole family file.
        assert expand_offsets([1, 0]) == [_ve(1, 0)]
        assert expand_offsets([0.5, -1.5]) == [_ve(0.5, -1.5)]
        # The wrapped form is unchanged, and a lone number is still an error.
        assert expand_offsets([[1, 0]]) == [_ve(1, 0)]
        with pytest.raises(ValueError):
            expand_offsets([1])


class TestScaleAndCumulative:
    def test_scale_by_unit(self) -> None:
        # TO-92 body: unit 1.25 mm.
        scaled = scale_offsets([Vector(1, 0), Vector(1, 0)], 1.25)
        assert scaled == [_ve(1.25, 0), _ve(1.25, 0)]

    def test_cumulative_pin1_at_origin(self) -> None:
        pos = cumulative_positions([Vector(0, 1), Vector(0, 1)])
        assert pos == [_pt(0, 0), _pt(0, 1), _pt(0, 2)]

    def test_dip8_positions(self) -> None:
        steps = expand_offsets([[3, [0, 1]], [3, 0], [3, [0, -1]]])
        pos = cumulative_positions(steps)
        assert len(pos) == 8
        assert pos[0] == _pt(0, 0)  # pin 1
        assert pos[3] == _pt(0, 3)  # pin 4 (bottom of left column)
        assert pos[4] == _pt(3, 3)  # pin 5 (after column jump)
        assert pos[7] == _pt(3, 0)  # pin 8 (top of right column, beside pin 1)


class TestAxialSolver:
    # Resistor DIN0207: L=6.3, D=2.5 (r_body=1.25), lead_r=0.27,
    # lead_length≈32, ecc=0. Spec says: horizontal min → 4 holes,
    # vertical min → 1 hole, max → ~28-29 holes (against raster 2.5).
    L = 6.3
    R_BODY = 1.25
    LEAD_R = 0.27
    LEAD_LEN = 32.0

    def _range(self, orientation: MountOrientation) -> OffsetRange:
        return axial_board_offset_range(
            length_mm=self.L,
            r_body_mm=self.R_BODY,
            lead_r_mm=self.LEAD_R,
            lead_length_mm=self.LEAD_LEN,
            orientation=orientation,
        )

    def test_horizontal_min_is_4_holes(self) -> None:
        r = self._range(MountOrientation.HORIZONTAL)
        assert r.r_min_mm == pytest.approx(6.3 + 6 * 0.27)  # 7.92
        assert math.ceil(r.r_min_mm / RASTER_MM) == 4

    def test_vertical_min_is_1_hole(self) -> None:
        r = self._range(MountOrientation.VERTICAL)
        assert r.r_min_mm == pytest.approx(1.25 + 3 * 0.27)  # 2.06
        assert math.ceil(r.r_min_mm / RASTER_MM) == 1

    def test_max_from_lead_length(self) -> None:
        r = self._range(MountOrientation.HORIZONTAL)
        assert r.r_max_mm == pytest.approx(7.92 + 2 * 32.0)  # 71.92
        # ~28-29 holes — in the neighbourhood of the legacy hand-tuned max=23.
        assert 28 <= round(r.r_max_mm / RASTER_MM) <= 29

    def test_eccentricity_reduces_vertical_min(self) -> None:
        # Eccentricity is a 2D vector; its x (lead-axis) projection reduces r_min.
        base = self._range(MountOrientation.VERTICAL)
        ecc = axial_board_offset_range(
            length_mm=self.L,
            r_body_mm=self.R_BODY,
            lead_r_mm=self.LEAD_R,
            lead_length_mm=self.LEAD_LEN,
            body_eccentricity_mm=Vector(0.5, 0.0),
            orientation=MountOrientation.VERTICAL,
        )
        assert ecc.r_min_mm == pytest.approx(base.r_min_mm - 0.5)

    def test_eccentricity_perpendicular_component_ignored(self) -> None:
        # The y component (perpendicular to the lead axis) does not change r_min.
        base = self._range(MountOrientation.VERTICAL)
        ecc = axial_board_offset_range(
            length_mm=self.L,
            r_body_mm=self.R_BODY,
            lead_r_mm=self.LEAD_R,
            lead_length_mm=self.LEAD_LEN,
            body_eccentricity_mm=Vector(0.0, 0.9),
            orientation=MountOrientation.VERTICAL,
        )
        assert ecc.r_min_mm == pytest.approx(base.r_min_mm)


class TestOffsetRange:
    def test_rejects_inverted(self) -> None:
        with pytest.raises(ValueError):
            OffsetRange(5.0, 2.0)

    def test_contains_is_euclidean_ring(self) -> None:
        r = OffsetRange(2.5, 7.5)
        assert r.contains_mm(5.0)
        assert not r.contains_mm(1.0)
        assert not r.contains_mm(8.0)
