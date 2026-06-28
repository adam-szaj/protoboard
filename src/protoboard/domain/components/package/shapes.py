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

from abc import ABC, abstractmethod
from collections.abc import Mapping
from dataclasses import dataclass, field
from typing import Any

from protoboard.domain.components.package.body import (
    DiscProfile,
    EdgeStyle,
    LeadArrangement,
    MountStyle,
)
from protoboard.domain.components.package.offsets import cumulative_positions
from protoboard.domain.components.package.schema import ParamSchema
from protoboard.domain.components.package.solver import (
    RASTER_MM,
    MountOrientation,
    OffsetRange,
    axial_board_offset_range,
)
from protoboard.geometry import Insets, Point, Rect, Vector, Vector3D
from protoboard.services.debug_trace import traced

# Sentinels for board_offset source (see spec): a literal list of step vectors,
# or one of these strings.
BOARD_DEFAULT = "default"  # same vector as body, read in the grid domain
BOARD_SOLVE = "solve"  # axial solver (body=0)


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

    direction: tuple[Vector3D, ...]
    body_offset_mm: tuple[Vector, ...]
    board_offset: tuple[Vector, ...] | str
    lead_length_mm: float | None = None
    lead_r_mm: float | None = None
    body_eccentricity_mm: Vector = Vector(0.0, 0.0)

    def __post_init__(self) -> None:
        import sys
        import traceback
        try:
            if self.body_offset_mm and not isinstance(self.body_offset_mm[0],
                                                      Vector):
                raise TypeError(
                    f"We are expecting touple of Vectors got: {self.body_offset_mm}"
                )  # .with_traceback(traceback.extract_stack())
            if self.board_offset and not (isinstance(self.board_offset, str)
                                          or isinstance(
                                              self.board_offset[0], Vector)):
                raise TypeError(
                    f"We are expecting touple of Vectors got: {self.board_offset}"
                )  # .with_traceback(traceback.extract_stack())
        except TypeError as exc:
            print(f"Got nasty exception: {exc}", file=sys.stderr)
            traceback.print_exception(exc)
            raise

    @property
    def pin_count(self) -> int:
        return len(self.direction)


class Package(ABC):
    """Geometry of a component derived from package data (see spec).

    Abstract base for the concrete shapes (``Cylinder`` / ``Prism`` / ``Disc``).
    Besides the geometry contract (the abstract methods below), it carries the
    package's *parameter metadata* so the UI can show editable/derived fields:

    - ``schema``: the parsed ``ParamSchema`` (field declarations) — ``None`` for
      a synthetic body built without a ``schema:`` block.
    - ``raw_values``: the resolved value tree (``dims`` / ``leads`` after
      ``extends`` + default-fill) the schema describes. Paired with ``schema``,
      ``schema.fields_view(raw_values)`` yields the per-field editability the
      Properties panel renders. Geometry never reads these; they exist only so
      the resolved definition survives ``build_package`` for the UI.

    Concrete shapes declare ``schema`` / ``raw_values`` as their own trailing
    dataclass fields (default ``None``, ``compare=False``) — keeping the existing
    positional field order intact — and this base unifies their type.
    """

    schema: ParamSchema | None
    raw_values: Mapping[str, Any] | None

    @abstractmethod
    def pin_count(self) -> int:
        ...

    @abstractmethod
    def pin_positions(
        self,
        *,
        orientation: MountOrientation = MountOrientation.HORIZONTAL
    ) -> list[Point]:
        ...

    @abstractmethod
    def board_offset_range(
        self,
        *,
        orientation: MountOrientation = MountOrientation.HORIZONTAL
    ) -> OffsetRange:
        ...

    @abstractmethod
    def bbox(self) -> Rect:
        ...

    @abstractmethod
    def min_radial_pitch_mm(self) -> float:
        ...

    @abstractmethod
    def min_axial_pitch_mm(self) -> float:
        ...


def _body_offset_in_holes(leads: Leads) -> list[Vector]:
    """The body_offset vectors expressed in the grid domain (holes).

    body_offset is in mm; dividing by RASTER_MM gives hole coordinates. For
    radial/DIP this is the canonical case where one vector serves both domains.
    """
    return [
        Vector(v.x / RASTER_MM, v.y / RASTER_MM) for v in leads.body_offset_mm
    ]


def _resolve_board_steps(
    leads: Leads,
    *,
    solved_range: OffsetRange | None,
) -> list[Vector]:
    """Resolve board_offset to concrete step vectors (holes), pin i → i+1.

    For the ``BOARD_SOLVE`` sentinel the caller (the shape, which knows the body
    dimensions) supplies ``solved_range``; the default board spacing is then its
    ``r_min`` laid along +x. ``default`` reads body_offset in the grid domain;
    an explicit list is returned as-is.
    """
    bo = leads.board_offset
    if isinstance(bo[0], Point):
        raise TypeError(f"We are expecting Vector, got: {bo}")

    if isinstance(bo, str):
        if bo == BOARD_DEFAULT:
            return _body_offset_in_holes(leads)
        if bo == BOARD_SOLVE:
            if solved_range is None:
                raise ValueError(
                    "BOARD_SOLVE requires a solved_range from the shape")
            # Two-pin axial; multi-pin solve is out of scope here.
            return [Vector(solved_range.r_min_mm / RASTER_MM, 0.0)]
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
class Cylinder(Package):
    """shape: cylinder — resistor, diode, electrolytic, TO-92. Implements Package.

    A *flat side* (D-shaped cross-section, e.g. the real TO-92 transistor) is
    declared by ``flat_radius_mm`` + ``flat_normal``: the body face is a full
    circle with one chord cut off. ``flat_radius_mm`` is the distance from the
    circle centre to that chord (``0 < r < diameter/2``); ``flat_normal`` is the
    outward normal of the flat face in the package's *local* frame — the same
    frame the lead offsets use (the renderer rotates it with the part). A flat
    side only makes sense for a radial (top-view) body; the renderer ignores it
    for an axial side view.
    """

    diameter_mm: float
    length_mm: float
    leads: Leads
    bend_holes: float = 0.0
    edge_radius_mm: float = 0.0
    arrangement: LeadArrangement = LeadArrangement.AXIAL
    edge: EdgeStyle = EdgeStyle.ROUNDED
    color: str | None = None  # #rrggbb fill; None → renderer default
    flat_radius_mm: float | None = None  # None → full circle (no flat side)
    flat_normal: Vector = Vector(0.0, 0.0)  # outward normal of the flat face
    # Parameter metadata (UI only; never read by geometry). See ``Package``.
    schema: ParamSchema | None = field(default=None, compare=False, hash=False)
    raw_values: Mapping[str, Any] | None = field(
        default=None, compare=False, hash=False)

    def __post_init__(self) -> None:
        if self.flat_radius_mm is not None:
            if not 0.0 < self.flat_radius_mm < self.diameter_mm / 2.0:
                raise ValueError(
                    "flat_radius_mm must satisfy 0 < r < diameter/2 "
                    f"(got r={self.flat_radius_mm}, diameter={self.diameter_mm})"
                )
            if self.flat_normal.x == 0.0 and self.flat_normal.y == 0.0:
                raise ValueError(
                    "flat_normal must be non-zero when flat_radius_mm is set")

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
        self,
        *,
        orientation: MountOrientation = MountOrientation.HORIZONTAL
    ) -> list[Point]:
        solved = (self.board_offset_range(orientation=orientation)
                  if self.leads.board_offset == BOARD_SOLVE else None)
        steps = _resolve_board_steps(self.leads, solved_range=solved)
        return cumulative_positions(steps)

    @traced("Cylinder.board_offset_range")
    def board_offset_range(
        self,
        *,
        orientation: MountOrientation = MountOrientation.HORIZONTAL
    ) -> OffsetRange:
        if self.leads.board_offset == BOARD_SOLVE:
            return axial_board_offset_range(
                length_mm=self.length_mm,
                r_body_mm=self.r_body_mm,
                lead_r_mm=_require(self.leads.lead_r_mm, "lead_r_mm"),
                lead_length_mm=_require(self.leads.lead_length_mm,
                                        "lead_length_mm"),
                body_eccentricity_mm=self.leads.body_eccentricity_mm,
                orientation=orientation,
            )
        # bend: symmetric radius, lower-clamped at body_offset (spec).
        base_mm = abs(self.leads.body_offset_mm[0].x
                      ) if self.leads.body_offset_mm else 0.0
        return OffsetRange(base_mm, base_mm + self.bend_holes * RASTER_MM)

    def bbox(self) -> Rect:
        # Body extent on the grid: a square of side = diameter, in holes.
        side = self.diameter_mm / RASTER_MM
        return Rect(0.0, 0.0, side, side)


@dataclass(frozen=True, slots=True)
class Prism(Package):
    """shape: prism — DIP, TO-92, devboard (optional margin)."""

    leads: Leads
    width_mm: float | None = None  # None → bbox from pins + margin
    length_mm: float | None = None
    height_mm: float | None = None
    bend_holes: float = 0.0
    corner_radius_mm: float = 0.0
    margin_mm: Insets | None = None
    arrangement: LeadArrangement = LeadArrangement.AXIAL
    edge: EdgeStyle = EdgeStyle.SHARP
    color: str | None = None  # #rrggbb fill; None → renderer default
    # Surface-mount parts (SMD chips) rest on pads, not in holes: the body is
    # rigid but the second terminal may land on a neighbour pad of the first.
    # A SURFACE package's reach is expressed as a free ring spanning the eight
    # neighbour pads (see ``reach.package_to_bend_specs``); ``mount_points`` is a
    # parsed-but-currently-unused declaration of an explicit neighbour set.
    mount: MountStyle = MountStyle.THROUGH_HOLE
    mount_points: tuple[Point, ...] = ()
    # Shift (mm) of the body centre off the pin-cloud centre, for parts whose
    # body is not symmetric about its pins.  Default (0, 0) → centred on the
    # pin cloud.  Only used with explicit width_mm/length_mm.
    body_offset_mm: Vector = Vector(0.0, 0.0)
    # Declared body kind (``dip_ic`` / ``dip_switch`` / …) set when this prism was
    # built from a package ``kind:`` field. The renderer reads it to draw the
    # declared look (notch vs plain) instead of guessing from the pin columns;
    # ``None`` → the renderer falls back to the column-based silhouette.
    kind: str | None = None
    # Parameter metadata (UI only; never read by geometry). See ``Package``.
    schema: ParamSchema | None = field(default=None, compare=False, hash=False)
    raw_values: Mapping[str, Any] | None = field(
        default=None, compare=False, hash=False)

    def pin_count(self) -> int:
        return self.leads.pin_count

    def min_radial_pitch_mm(self) -> float:
        # Width perpendicular to the lead axis bounds two same-face leads;
        # fall back to the body bbox width when width is margin-derived.
        w = self.width_mm if self.width_mm is not None else self.bbox(
        ).width * RASTER_MM
        return (w + _lead_diameter_mm(self.leads)) / 2.0

    def min_axial_pitch_mm(self) -> float:
        ln = self.length_mm if self.length_mm is not None else self.bbox(
        ).height * RASTER_MM
        return ln + _lead_diameter_mm(self.leads)

    @traced("Prism.pin_positions")
    def pin_positions(
        self,
        *,
        orientation: MountOrientation = MountOrientation.HORIZONTAL
    ) -> list[Point]:
        if self.leads.board_offset == BOARD_SOLVE:
            raise ValueError(
                "prism does not support the axial solver (body=0)")
        steps = _resolve_board_steps(self.leads, solved_range=None)
        return cumulative_positions(steps)

    @traced("Prism.board_offset_range")
    def board_offset_range(
        self,
        *,
        orientation: MountOrientation = MountOrientation.HORIZONTAL
    ) -> OffsetRange:
        if self.leads.board_offset == BOARD_SOLVE:
            raise ValueError(
                "prism does not support the axial solver (body=0)")
        base_mm = abs(self.leads.body_offset_mm[0].x
                      ) if self.leads.body_offset_mm else 0.0
        return OffsetRange(base_mm, base_mm + self.bend_holes * RASTER_MM)

    def bbox(self) -> Rect:
        if self.margin_mm is not None and self.width_mm is None:
            return self._bbox_from_pins(self.margin_mm)
        if self.width_mm is None or self.length_mm is None:
            raise ValueError(
                "prism needs explicit dims or a margin to derive bbox")
        # An explicit body is W (across the rows) × L (along the pin axis),
        # centred on the pin cloud — so a DIP body sits between the rows with
        # the legs showing, rather than anchored at pin 1 and overlapping them.
        # ``body_offset_mm`` shifts the centre for asymmetric parts.
        w = self.width_mm / RASTER_MM
        h = self.length_mm / RASTER_MM
        centre = _pins_bbox(self.pin_positions()).center
        off = self.body_offset_mm
        return Rect(
            centre.x + off.x / RASTER_MM - w / 2.0,
            centre.y + off.y / RASTER_MM - h / 2.0,
            w,
            h,
        )

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
class Disc(Package):
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
    # Parameter metadata (UI only; never read by geometry). See ``Package``.
    schema: ParamSchema | None = field(default=None, compare=False, hash=False)
    raw_values: Mapping[str, Any] | None = field(
        default=None, compare=False, hash=False)

    def pin_count(self) -> int:
        return self.leads.pin_count

    @traced("Disc.pin_positions")
    def pin_positions(
        self,
        *,
        orientation: MountOrientation = MountOrientation.HORIZONTAL
    ) -> list[Point]:
        if self.leads.board_offset == BOARD_SOLVE:
            raise ValueError("disc does not support the axial solver (body=0)")
        steps = _resolve_board_steps(self.leads, solved_range=None)
        return cumulative_positions(steps)

    @traced("Disc.board_offset_range")
    def board_offset_range(
        self,
        *,
        orientation: MountOrientation = MountOrientation.HORIZONTAL
    ) -> OffsetRange:
        if self.leads.board_offset == BOARD_SOLVE:
            raise ValueError("disc does not support the axial solver (body=0)")
        base_mm = abs(self.leads.body_offset_mm[0].x
                      ) if self.leads.body_offset_mm else 0.0
        return OffsetRange(base_mm, base_mm + self.bend_holes * RASTER_MM)

    def bbox(self) -> Rect:
        side = self.diameter_mm / RASTER_MM
        return Rect(0.0, 0.0, side, side)

    def min_radial_pitch_mm(self) -> float:
        return (self.diameter_mm + _lead_diameter_mm(self.leads)) / 2.0

    def min_axial_pitch_mm(self) -> float:
        return self.thickness_mm + _lead_diameter_mm(self.leads)


def is_rigid_package(package: Package) -> bool:
    """Whether *package* has fixed pin geometry (no bend, no axial solver).

    A rigid package — an SMD chip, a DIP, an HC-49 crystal — sits on a fixed
    pad pattern: its ``board_offset`` is concrete (or the ``default`` sentinel,
    read straight off the grid) and it has zero ``bend``.  Such a part overrides
    a component's bendable leads — the body, not a bent wire, fixes the pad
    spacing.

    A bendable package — an axial resistor/diode — uses the axial solver
    (``BOARD_SOLVE``) or declares a ``bend`` reach, so its lead span is variable
    and the footprint's ``bendable_pins`` still apply.
    """
    leads = getattr(package, "leads", None)
    if leads is None:
        return False
    if leads.board_offset == BOARD_SOLVE:
        return False
    return getattr(package, "bend_holes", 0.0) == 0.0


# Dispatch by `shape` (registry), NOT subclassing the `extends` chain (spec).
SHAPE_REGISTRY: dict[str, type] = {
    "cylinder": Cylinder,
    "prism": Prism,
    "disc": Disc,
}
