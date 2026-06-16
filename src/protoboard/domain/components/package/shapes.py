# SPDX-License-Identifier: MIT
"""Package shapes — Cylinder / Prism implement the Package protocol.

Geometry is computed from data (``dims`` + ``leads``); see
``docs/footprints/spec.md`` and ``docs/footprints/package_interface_sketch.py``.
The loader normalises YAML into ``Leads`` before reaching this layer — this
module knows nothing about YAML syntax.

Two domains (spec): pin positions live on the grid (holes), board_offset_range
is in mm (physics). Conversion is the consumer's job.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Protocol, runtime_checkable

from protoboard.domain.components.package.body import (
    DiscProfile,
    EdgeStyle,
    LeadArrangement,
)
from protoboard.domain.components.package.offsets import cumulative_positions
from protoboard.domain.components.package.solver import (
    RASTER_MM,
    MountOrientation,
    OffsetRange,
    axial_board_offset_range,
)
from protoboard.geometry import Insets, Point, Rect
from protoboard.services.debug_trace import traced

# Sentinels for board_offset source (see spec): a literal list of step vectors,
# or one of these strings.
BOARD_DEFAULT = "default"  # same vector as body, read in the grid domain
BOARD_SOLVE = "solve"      # axial solver (body=0)


@dataclass(frozen=True, slots=True)
class Leads:
    """Unpacked lead geometry (loader output; mm/holes already resolved).

    - ``direction``: one 3-vector per pin (loader expands the 3 YAML forms).
    - ``body_offset_mm``: N-1 step vectors already scaled by unit → mm.
    - ``board_offset``: either explicit N-1 step vectors in *holes*, or a
      sentinel (``BOARD_DEFAULT`` / ``BOARD_SOLVE``).
    - ``lead_length_mm`` / ``lead_r_mm``: solver inputs (mm); None until filled.
    - ``body_eccentricity_mm``: lead-exit eccentricity vs body axis — a 2D
      vector (mm) from the geometric centre of the lead axis to the body axis.
      A scalar in YAML is sugar for ``(s, 0)``. The loader also accepts the
      absolute ``body_axis_pos`` form and converts it to this vector at load.
    """

    direction: tuple[tuple[float, float, float], ...]
    body_offset_mm: tuple[Point, ...]
    board_offset: tuple[Point, ...] | str
    lead_length_mm: float | None = None
    lead_r_mm: float | None = None
    body_eccentricity_mm: Point = Point(0.0, 0.0)

    @property
    def pin_count(self) -> int:
        return len(self.direction)


@runtime_checkable
class Package(Protocol):
    """Geometry of a component derived from package data (see spec)."""

    def pin_count(self) -> int: ...

    def pin_positions(
        self, *, orientation: MountOrientation = MountOrientation.HORIZONTAL
    ) -> list[Point]: ...

    def board_offset_range(
        self, *, orientation: MountOrientation = MountOrientation.HORIZONTAL
    ) -> OffsetRange: ...

    def bbox(self) -> Rect: ...

    def min_radial_pitch_mm(self) -> float: ...

    def min_axial_pitch_mm(self) -> float: ...


def _body_offset_in_holes(leads: Leads) -> list[Point]:
    """The body_offset vectors expressed in the grid domain (holes).

    body_offset is in mm; dividing by RASTER_MM gives hole coordinates. For
    radial/DIP this is the canonical case where one vector serves both domains.
    """
    return [Point(v.x / RASTER_MM, v.y / RASTER_MM) for v in leads.body_offset_mm]


def _resolve_board_steps(
    leads: Leads,
    *,
    solved_range: OffsetRange | None,
) -> list[Point]:
    """Resolve board_offset to concrete step vectors (holes), pin i → i+1.

    For the ``BOARD_SOLVE`` sentinel the caller (the shape, which knows the body
    dimensions) supplies ``solved_range``; the default board spacing is then its
    ``r_min`` laid along +x. ``default`` reads body_offset in the grid domain;
    an explicit list is returned as-is.
    """
    bo = leads.board_offset
    if isinstance(bo, str):
        if bo == BOARD_DEFAULT:
            return _body_offset_in_holes(leads)
        if bo == BOARD_SOLVE:
            if solved_range is None:
                raise ValueError("BOARD_SOLVE requires a solved_range from the shape")
            # Two-pin axial; multi-pin solve is out of scope here.
            return [Point(solved_range.r_min_mm / RASTER_MM, 0.0)]
        raise ValueError(f"unknown board_offset sentinel: {bo!r}")
    return list(bo)


def _require(value: float | None, name: str) -> float:
    if value is None:
        raise ValueError(f"{name} is required (null) — fill before use")
    return value


def _pins_bbox(positions: list[Point]) -> Rect:
    xs = [p.x for p in positions]
    ys = [p.y for p in positions]
    x0, y0 = min(xs), min(ys)
    return Rect(x0, y0, max(xs) - x0, max(ys) - y0)


def _lead_diameter_mm(leads: Leads) -> float:
    """Lead wire diameter (mm). ``2·lead_r``; falls back to a 0.5 mm default
    (the legacy ``TwoPinBodySpec`` default) when the radius is unspecified."""
    if leads.lead_r_mm is None:
        return 0.5
    return 2.0 * leads.lead_r_mm


@dataclass(frozen=True, slots=True)
class Cylinder:
    """shape: cylinder — resistor, diode, electrolytic. Implements Package."""

    diameter_mm: float
    length_mm: float
    leads: Leads
    bend_holes: float = 0.0
    edge_radius_mm: float = 0.0
    arrangement: LeadArrangement = LeadArrangement.AXIAL
    edge: EdgeStyle = EdgeStyle.ROUNDED
    color: str | None = None  # #rrggbb fill; None → renderer default

    @property
    def r_body_mm(self) -> float:
        return self.diameter_mm / 2.0

    def pin_count(self) -> int:
        return self.leads.pin_count

    def min_radial_pitch_mm(self) -> float:
        # Two leads exiting the same face must clear the widest cross-section.
        return (self.diameter_mm + _lead_diameter_mm(self.leads)) / 2.0

    def min_axial_pitch_mm(self) -> float:
        # Pads sit at the body ends; spacing is at least one body length.
        return self.length_mm + _lead_diameter_mm(self.leads)

    @traced("Cylinder.pin_positions")
    def pin_positions(
        self, *, orientation: MountOrientation = MountOrientation.HORIZONTAL
    ) -> list[Point]:
        solved = (
            self.board_offset_range(orientation=orientation)
            if self.leads.board_offset == BOARD_SOLVE
            else None
        )
        steps = _resolve_board_steps(self.leads, solved_range=solved)
        return cumulative_positions(steps)

    @traced("Cylinder.board_offset_range")
    def board_offset_range(
        self, *, orientation: MountOrientation = MountOrientation.HORIZONTAL
    ) -> OffsetRange:
        if self.leads.board_offset == BOARD_SOLVE:
            return axial_board_offset_range(
                length_mm=self.length_mm,
                r_body_mm=self.r_body_mm,
                lead_r_mm=_require(self.leads.lead_r_mm, "lead_r_mm"),
                lead_length_mm=_require(self.leads.lead_length_mm, "lead_length_mm"),
                body_eccentricity_mm=self.leads.body_eccentricity_mm,
                orientation=orientation,
            )
        # bend: symmetric radius, lower-clamped at body_offset (spec).
        base_mm = abs(self.leads.body_offset_mm[0].x) if self.leads.body_offset_mm else 0.0
        return OffsetRange(base_mm, base_mm + self.bend_holes * RASTER_MM)

    def bbox(self) -> Rect:
        # Body extent on the grid: a square of side = diameter, in holes.
        side = self.diameter_mm / RASTER_MM
        return Rect(0.0, 0.0, side, side)


@dataclass(frozen=True, slots=True)
class Prism:
    """shape: prism — DIP, TO-92, devboard (optional margin)."""

    leads: Leads
    width_mm: float | None = None   # None → bbox from pins + margin
    length_mm: float | None = None
    height_mm: float | None = None
    bend_holes: float = 0.0
    corner_radius_mm: float = 0.0
    margin_mm: Insets | None = None
    arrangement: LeadArrangement = LeadArrangement.AXIAL
    edge: EdgeStyle = EdgeStyle.SHARP
    color: str | None = None  # #rrggbb fill; None → renderer default

    def pin_count(self) -> int:
        return self.leads.pin_count

    def min_radial_pitch_mm(self) -> float:
        # Width perpendicular to the lead axis bounds two same-face leads;
        # fall back to the body bbox width when width is margin-derived.
        w = self.width_mm if self.width_mm is not None else self.bbox().width * RASTER_MM
        return (w + _lead_diameter_mm(self.leads)) / 2.0

    def min_axial_pitch_mm(self) -> float:
        ln = self.length_mm if self.length_mm is not None else self.bbox().height * RASTER_MM
        return ln + _lead_diameter_mm(self.leads)

    @traced("Prism.pin_positions")
    def pin_positions(
        self, *, orientation: MountOrientation = MountOrientation.HORIZONTAL
    ) -> list[Point]:
        if self.leads.board_offset == BOARD_SOLVE:
            raise ValueError("prism does not support the axial solver (body=0)")
        steps = _resolve_board_steps(self.leads, solved_range=None)
        return cumulative_positions(steps)

    @traced("Prism.board_offset_range")
    def board_offset_range(
        self, *, orientation: MountOrientation = MountOrientation.HORIZONTAL
    ) -> OffsetRange:
        if self.leads.board_offset == BOARD_SOLVE:
            raise ValueError("prism does not support the axial solver (body=0)")
        base_mm = abs(self.leads.body_offset_mm[0].x) if self.leads.body_offset_mm else 0.0
        return OffsetRange(base_mm, base_mm + self.bend_holes * RASTER_MM)

    def bbox(self) -> Rect:
        if self.margin_mm is not None and self.width_mm is None:
            return self._bbox_from_pins(self.margin_mm)
        if self.width_mm is None or self.length_mm is None:
            raise ValueError("prism needs explicit dims or a margin to derive bbox")
        w = self.width_mm / RASTER_MM
        h = self.length_mm / RASTER_MM
        return Rect(0.0, 0.0, w, h)

    def _bbox_from_pins(self, margin: Insets) -> Rect:
        pins = self.pin_positions()
        pb = _pins_bbox(pins)  # in holes
        # margin is in mm, from pin centres → convert to holes.
        m_l = margin.left / RASTER_MM
        m_r = margin.right / RASTER_MM
        m_t = margin.top / RASTER_MM
        m_b = margin.bottom / RASTER_MM
        return Rect(
            pb.x - m_l,
            pb.y - m_t,
            pb.width + m_l + m_r,
            pb.height + m_t + m_b,
        )


@dataclass(frozen=True, slots=True)
class Disc:
    """shape: disc — ceramic disc cap, button cell, disc varistor.

    A flat puck whose face is normal to the lead axis; radial-mounted (both
    leads exit the same face). ``diameter_mm`` is the face, ``thickness_mm`` the
    depth along the lead axis. ``profile`` picks the rim silhouette.
    """

    diameter_mm: float
    thickness_mm: float
    leads: Leads
    bend_holes: float = 0.0
    profile: DiscProfile = DiscProfile.FLAT
    arrangement: LeadArrangement = LeadArrangement.RADIAL
    edge: EdgeStyle = EdgeStyle.ROUNDED
    color: str | None = None  # #rrggbb fill; None → renderer default

    def pin_count(self) -> int:
        return self.leads.pin_count

    @traced("Disc.pin_positions")
    def pin_positions(
        self, *, orientation: MountOrientation = MountOrientation.HORIZONTAL
    ) -> list[Point]:
        if self.leads.board_offset == BOARD_SOLVE:
            raise ValueError("disc does not support the axial solver (body=0)")
        steps = _resolve_board_steps(self.leads, solved_range=None)
        return cumulative_positions(steps)

    @traced("Disc.board_offset_range")
    def board_offset_range(
        self, *, orientation: MountOrientation = MountOrientation.HORIZONTAL
    ) -> OffsetRange:
        if self.leads.board_offset == BOARD_SOLVE:
            raise ValueError("disc does not support the axial solver (body=0)")
        base_mm = abs(self.leads.body_offset_mm[0].x) if self.leads.body_offset_mm else 0.0
        return OffsetRange(base_mm, base_mm + self.bend_holes * RASTER_MM)

    def bbox(self) -> Rect:
        side = self.diameter_mm / RASTER_MM
        return Rect(0.0, 0.0, side, side)

    def min_radial_pitch_mm(self) -> float:
        return (self.diameter_mm + _lead_diameter_mm(self.leads)) / 2.0

    def min_axial_pitch_mm(self) -> float:
        return self.thickness_mm + _lead_diameter_mm(self.leads)


# Dispatch by `shape` (registry), NOT subclassing the `extends` chain (spec).
SHAPE_REGISTRY: dict[str, type] = {
    "cylinder": Cylinder,
    "prism": Prism,
    "disc": Disc,
}
