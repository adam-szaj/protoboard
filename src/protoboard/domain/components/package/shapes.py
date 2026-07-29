# SPDX-License-Identifier: MIT
"""Package shapes — Cylinder / Prism implement the Package protocol.

Geometry is computed from data (``dims`` + ``leads``); see
``docs/footprints/package-model.md`` and ``docs/footprints/package_interface_sketch.py``.
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
from protoboard.geometry import (
    Insets,
    Point,
    Rect,
    Transform2D,
    Vector,
    Vector3D,
    flip_reflection_from_pins,
)
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

    ``lineage`` is the package's ancestry, root-first: the ``shape`` name
    followed by every name on the resolved ``extends`` chain (and the ``kind``
    for generator-built bodies), e.g. ``("cylinder", "cylinder_axial",
    "DO-35")``.  Renderer dispatch walks it leaf-first so the most specific
    registered renderer wins and unknown names fall back towards the shape
    root (see ``gui/renderers/packages``).  Metadata only — geometry never
    reads it.
    """

    schema: ParamSchema | None
    raw_values: Mapping[str, Any] | None
    lineage: tuple[str, ...]

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

    def reach_offset_range(self) -> OffsetRange:
        """The ring of spacings a movable terminal may legally take, regardless
        of mount orientation — the union over every orientation the part can be
        mounted in.

        This is the **place-tool / re-drag** reach (where pin 2 *may* land),
        distinct from :meth:`board_offset_range`, which gives the spacing for one
        chosen orientation (and drives the default ghost layout). For most parts
        the two coincide; an axial part is the exception (it can mount flat or
        upright), so :class:`Cylinder` widens this to span both. The default here
        returns the (orientation-independent) ``board_offset_range``.
        """
        return self.board_offset_range()

    @abstractmethod
    def bbox(self) -> Rect:
        ...

    def flip_transform(self) -> Transform2D:
        """Reflection (local frame) for a physical board-side flip.

        A flip is a 180° rotation about an axis lying in the board plane; its
        2-D projection is a reflection.  The axis runs ALONG the pin row — so the
        pins stay on their pads — and passes through the pin-cloud centre, so the
        transform permutes pins onto other pins' positions and never off-grid.

        The default derives the axis from the pin geometry
        (:func:`flip_reflection_from_pins`); a package whose physical flip does
        not follow its dominant pin axis may override this.
        """
        return flip_reflection_from_pins(self.pin_positions())

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
        Vector(v.dx / RASTER_MM, v.dy / RASTER_MM) for v in leads.body_offset_mm
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


def lead_axis_extents(package: Package) -> tuple[float, float]:
    """Body bbox extents in holes, split ``(along, across)`` the pin-row axis.

    "Along" is the DOMINANT axis of the pin cloud in the package-local frame
    (a tie keeps the historical along=y convention). The one rule shared by
    the radial / SMD renderers and any consumer that needs body length vs
    width *relative to the leads* — ``Prism.bbox`` orients its explicit dims
    by the same rule, so geometry and mask stay consistent.
    """
    bb = package.bbox()
    pb = _pins_bbox(package.pin_positions())
    if pb.lx > pb.ly:
        return bb.lx, bb.ly
    return bb.ly, bb.lx


def bbox_or_none(package: Package) -> Rect | None:
    """``package.bbox()``, or ``None`` when no body extent is derivable.

    The degrade contract shared by masking and rendering: a package with no
    explicit dims and no margin has no body rectangle — the part occupies /
    draws only its pins (or a caller-chosen generic fallback).
    """
    try:
        return package.bbox()
    except ValueError:
        return None


def lead_axis_extents_or_none(package: Package) -> tuple[float, float] | None:
    """:func:`lead_axis_extents`, or ``None`` when no dims are derivable.

    Same degrade contract as :func:`bbox_or_none`, split along/across the
    pin-row axis.
    """
    try:
        return lead_axis_extents(package)
    except ValueError:
        return None


def _pins_bbox(positions: list[Point]) -> Rect:
    xs = [p.x for p in positions]
    ys = [p.y for p in positions]
    x0, y0 = min(xs), min(ys)
    return Rect.of(x0, y0, max(xs) - x0, max(ys) - y0)


def _body_centre_holes(pins: list[Point], eccentricity_mm: Vector) -> Point:
    """Body symmetry-axis position (holes): pin centroid + lead eccentricity.

    ``body_eccentricity`` is the vector from the geometric midpoint of the
    lead axis to the body's symmetry axis (spec §2.7), given in mm.
    """
    n = len(pins)
    return Point(
        sum(p.x for p in pins) / n + eccentricity_mm.dx / RASTER_MM,
        sum(p.y for p in pins) / n + eccentricity_mm.dy / RASTER_MM,
    )


def _oriented_rect_aabb(
    centre: Point, along: float, across: float, axis: Vector,
) -> Rect:
    """AABB (local frame) of a rectangle ``along × across`` oriented on *axis*.

    The rectangle is centred on *centre* with its ``along`` extent parallel to
    the (non-zero) *axis*; the AABB projects the oriented half-extents onto
    the local axes: ``half = (along·|u| + across·|v|) / 2`` per component,
    where ``u`` is the unit axis and ``v`` its perpendicular.
    """
    length = axis.norm()
    ux, uy = axis.dx / length, axis.dy / length
    half_x = (along * abs(ux) + across * abs(uy)) / 2.0
    half_y = (along * abs(uy) + across * abs(ux)) / 2.0
    return Rect.of(centre.x - half_x, centre.y - half_y, 2 * half_x, 2 * half_y)


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
    # Ancestry for renderer dispatch (see ``Package``). Not part of geometry.
    lineage: tuple[str, ...] = field(default=(), compare=False, hash=False)

    def __post_init__(self) -> None:
        if self.flat_radius_mm is not None:
            if not 0.0 < self.flat_radius_mm < self.diameter_mm / 2.0:
                raise ValueError(
                    "flat_radius_mm must satisfy 0 < r < diameter/2 "
                    f"(got r={self.flat_radius_mm}, diameter={self.diameter_mm})"
                )
            if self.flat_normal.dx == 0.0 and self.flat_normal.dy == 0.0:
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

    @traced()
    def pin_positions(
        self,
        *,
        orientation: MountOrientation = MountOrientation.HORIZONTAL
    ) -> list[Point]:
        solved = (self.board_offset_range(orientation=orientation)
                  if self.leads.board_offset == BOARD_SOLVE else None)
        steps = _resolve_board_steps(self.leads, solved_range=solved)
        return cumulative_positions(steps)

    @traced()
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
        base_mm = abs(self.leads.body_offset_mm[0].dx
                      ) if self.leads.body_offset_mm else 0.0
        return OffsetRange(base_mm, base_mm + self.bend_holes * RASTER_MM)

    def reach_offset_range(self) -> OffsetRange:
        """Axial reach spans BOTH mount orientations (the union).

        An axial part can lie flat (horizontal: ``r_min = L + 6·lead_r``, a wide
        spacing) OR stand upright with the leads bent down (vertical: ``r_min =
        r_body − ecc + 3·lead_r``, a much smaller spacing — pin 2 on an adjacent
        pad). The orientation is chosen by the spacing the user actually places,
        so the legal reach is ``[min of the two r_min, max of the two r_max]`` —
        a continuous band from the upright minimum out to the flat maximum.
        Without this the place tool only ever saw the horizontal ring and an
        upright resistor/diode could not be placed on adjacent pads.
        For the non-solve (``bend``) case there is a single ring → unchanged.
        """
        if self.leads.board_offset != BOARD_SOLVE:
            return self.board_offset_range()
        horiz = self.board_offset_range(orientation=MountOrientation.HORIZONTAL)
        vert = self.board_offset_range(orientation=MountOrientation.VERTICAL)
        return OffsetRange(
            min(horiz.r_min_mm, vert.r_min_mm),
            max(horiz.r_max_mm, vert.r_max_mm),
        )

    def bbox(self) -> Rect:
        """Local-frame body extent over the default pin layout.

        Centred on the pin centroid + lead eccentricity — the same place the
        renderer puts the body — NOT anchored at pin 1 (the old pin-1-anchored
        square claimed a phantom row of holes in the collision mask, flagging
        adjacent radial parts as BODY_OVERLAP).  RADIAL: the top view is a
        disc → a d×d square around the centre.  AXIAL: an L×d capsule along
        the pin axis, projected to an AABB.
        """
        d = self.diameter_mm / RASTER_MM
        pins = self.pin_positions()
        centre = _body_centre_holes(pins, self.leads.body_eccentricity_mm)
        if (self.arrangement is LeadArrangement.RADIAL or len(pins) < 2):
            return Rect.of(centre.x - d / 2.0, centre.y - d / 2.0, d, d)
        axis = pins[-1] - pins[0]
        if axis.norm() < 1e-9:
            return Rect.of(centre.x - d / 2.0, centre.y - d / 2.0, d, d)
        return _oriented_rect_aabb(
            centre, self.length_mm / RASTER_MM, d, axis)


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
    # Ancestry for renderer dispatch (see ``Package``). Not part of geometry.
    lineage: tuple[str, ...] = field(default=(), compare=False, hash=False)

    def pin_count(self) -> int:
        return self.leads.pin_count

    def min_radial_pitch_mm(self) -> float:
        # Width perpendicular to the lead axis bounds two same-face leads;
        # fall back to the body bbox width when width is margin-derived.
        w = self.width_mm if self.width_mm is not None else self.bbox(
        ).lx * RASTER_MM
        return (w + _lead_diameter_mm(self.leads)) / 2.0

    def min_axial_pitch_mm(self) -> float:
        ln = self.length_mm if self.length_mm is not None else self.bbox(
        ).ly * RASTER_MM
        return ln + _lead_diameter_mm(self.leads)

    @traced()
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

    @traced()
    def board_offset_range(
        self,
        *,
        orientation: MountOrientation = MountOrientation.HORIZONTAL
    ) -> OffsetRange:
        if self.leads.board_offset == BOARD_SOLVE:
            raise ValueError(
                "prism does not support the axial solver (body=0)")
        base_mm = abs(self.leads.body_offset_mm[0].dx
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
        # "Along the pin axis" is the DOMINANT axis of the pin cloud in the
        # local frame (a devboard's rows run along y, a TO-220's / SMD chip's
        # single row along x) — L goes on that axis, W across it. A square or
        # empty cloud keeps the historical W×L = x×y mapping.
        # ``body_offset_mm`` shifts the centre for asymmetric parts.
        w = self.width_mm / RASTER_MM
        h = self.length_mm / RASTER_MM
        pb = _pins_bbox(self.pin_positions())
        if pb.lx > pb.ly:
            w, h = h, w  # pin row runs along x → length on x, width on y
        centre = pb.center
        off = self.body_offset_mm
        return Rect.of(
            centre.x + off.dx / RASTER_MM - w / 2.0,
            centre.y + off.dy / RASTER_MM - h / 2.0,
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
        return Rect.of(
            pb.x - m_l,
            pb.y - m_t,
            pb.lx + m_l + m_r,
            pb.ly + m_t + m_b,
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
    # Ancestry for renderer dispatch (see ``Package``). Not part of geometry.
    lineage: tuple[str, ...] = field(default=(), compare=False, hash=False)

    def pin_count(self) -> int:
        return self.leads.pin_count

    @traced()
    def pin_positions(
        self,
        *,
        orientation: MountOrientation = MountOrientation.HORIZONTAL
    ) -> list[Point]:
        if self.leads.board_offset == BOARD_SOLVE:
            raise ValueError("disc does not support the axial solver (body=0)")
        steps = _resolve_board_steps(self.leads, solved_range=None)
        return cumulative_positions(steps)

    @traced()
    def board_offset_range(
        self,
        *,
        orientation: MountOrientation = MountOrientation.HORIZONTAL
    ) -> OffsetRange:
        if self.leads.board_offset == BOARD_SOLVE:
            raise ValueError("disc does not support the axial solver (body=0)")
        base_mm = abs(self.leads.body_offset_mm[0].dx
                      ) if self.leads.body_offset_mm else 0.0
        return OffsetRange(base_mm, base_mm + self.bend_holes * RASTER_MM)

    def bbox(self) -> Rect:
        """Local-frame body extent over the default pin layout.

        An edge-mounted disc seen from above is a lens ``diameter`` long along
        the lead axis and ``thickness`` across it, centred on the pin centroid
        + lead eccentricity (mirrors the renderer's ``_radial_disc_side``).
        """
        d = self.diameter_mm / RASTER_MM
        pins = self.pin_positions()
        centre = _body_centre_holes(pins, self.leads.body_eccentricity_mm)
        if len(pins) < 2:
            return Rect.of(centre.x - d / 2.0, centre.y - d / 2.0, d, d)
        axis = pins[-1] - pins[0]
        if axis.norm() < 1e-9:
            return Rect.of(centre.x - d / 2.0, centre.y - d / 2.0, d, d)
        return _oriented_rect_aabb(
            centre, d, self.thickness_mm / RASTER_MM, axis)

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
