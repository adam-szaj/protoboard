# SPDX-License-Identifier: MIT
"""ComponentPlacement and pin_world_position."""

from __future__ import annotations

import uuid
from collections.abc import Callable, Iterable, Iterator
from dataclasses import dataclass, field
from typing import TYPE_CHECKING, overload

import structlog

from protoboard.domain.components.footprints import Footprint
from protoboard.types import Side
from protoboard.geometry import Point, Rect, Transform2D, Vector, round_coord
from protoboard.services.call_counters import counted

if TYPE_CHECKING:
    from protoboard.domain.board.board import Board
    from protoboard.domain.components.definition import ComponentDefinition
    from protoboard.domain.components.instance import ComponentInstance
    from protoboard.domain.components.package.family import PackageFamily
    from protoboard.domain.layout.layout import Layout

_log = structlog.get_logger(__name__)

# Component classes whose leads can be bent freely — only the pads matter for
# collision/selection purposes, not the floating body between them.
_FLEXIBLE_LEAD_CLASSES: frozenset[str] = frozenset({
    "resistor", "capacitor_polar", "capacitor_nonpolar", "inductor",
    "diode", "diode_zener", "diode_led",
    "transistor_npn", "transistor_pnp",
    "transistor_nmos", "transistor_pmos",
    "transistor_njfet", "transistor_pjfet",
    "fuse",
})

_VALID_ROTATIONS = frozenset({0, 90, 180, 270})


@dataclass
class ComponentPlacement:
    component_id: uuid.UUID
    position: Point
    rotation: int # TODO change to Rorate2D
    side: Side
    label_offset: Vector = field(default_factory=lambda: Vector(0.05, 0.05))
    pin_offsets: dict[int, Vector] = field(default_factory=dict)
    # Pin numbers (1-based) intentionally sharing a pad with another component.
    shared_pins: frozenset[int] = field(default_factory=frozenset)
    vertical_flipped: bool = False
    locked: bool = False

    def __hash__(self) -> int:
        return hash(self.component_id)

    def __eq__(self, other: object) -> bool:
        if not isinstance(other, ComponentPlacement):
            return NotImplemented
        return self.component_id == other.component_id

    def copy(self) -> ComponentPlacement:
        return ComponentPlacement(
            component_id=self.component_id,
            position=self.position,
            rotation=self.rotation,
            side=self.side,
            label_offset=self.label_offset,
            pin_offsets=dict(self.pin_offsets),
            shared_pins=self.shared_pins,
            vertical_flipped=self.vertical_flipped,
            locked=self.locked,
        )

    def __post_init__(self) -> None:
        if self.rotation not in _VALID_ROTATIONS:
            raise ValueError(
                f"rotation must be one of {sorted(_VALID_ROTATIONS)}; "
                f"got {self.rotation}"
            )
        if self.side not in ("top", "bottom"):
            raise ValueError(f"side must be 'top' or 'bottom'; got {self.side!r}")


class PlacementCollection:
    """Dict-keyed placement storage that COMPOSES group transforms on read.

    Stores each member's **group-relative (local)** placement.  On any *value*
    read (``[cid]``, ``.get``, ``.values``, ``.items``, value-iteration) it
    returns the **effective (world)** placement — the stored local one folded
    through the composed ancestor-group transform via an injected ``compose_fn``
    (the owning :class:`Layout` supplies it; the collection itself does not know
    about groups, avoiding an import cycle).

    Key-only ops (``for cid in placements``, ``.keys``, ``in``, ``len``,
    ``.pop``, ``del``) and writes operate on the raw local dict — writers store
    verbatim local placements.  Persistence uses ``local_items`` to serialize
    the stored locals without composing.

    Modeled on :class:`WireCollection`.  When ``compose_fn`` is unset (or every
    group transform is identity) reads return the stored placement unchanged, so
    the shim is transparent — the state through Phase 2 and for every ungrouped
    component.
    """

    def __init__(
        self,
        initial: dict[uuid.UUID, ComponentPlacement] | None = None,
    ) -> None:
        self._by_id: dict[uuid.UUID, ComponentPlacement] = dict(initial or {})
        self._compose: Callable[[uuid.UUID, ComponentPlacement], ComponentPlacement] | None = None

    def set_compose_fn(
        self,
        fn: Callable[[uuid.UUID, ComponentPlacement], ComponentPlacement] | None,
    ) -> None:
        """Register the group-transform composer (called on every value read)."""
        self._compose = fn

    def _composed(
        self, cid: uuid.UUID, pl: ComponentPlacement
    ) -> ComponentPlacement:
        if self._compose is None:
            return pl
        return self._compose(cid, pl)

    # ----- value reads (composed) -----

    def __getitem__(self, cid: uuid.UUID) -> ComponentPlacement:
        return self._composed(cid, self._by_id[cid])

    @overload
    def get(self, cid: uuid.UUID) -> ComponentPlacement | None: ...
    @overload
    def get(
        self, cid: uuid.UUID, default: ComponentPlacement
    ) -> ComponentPlacement: ...

    def get(
        self, cid: uuid.UUID, default: ComponentPlacement | None = None
    ) -> ComponentPlacement | None:
        pl = self._by_id.get(cid)
        if pl is None:
            return default
        return self._composed(cid, pl)

    def values(self) -> list[ComponentPlacement]:
        return [self._composed(cid, pl) for cid, pl in self._by_id.items()]

    def items(self) -> list[tuple[uuid.UUID, ComponentPlacement]]:
        return [(cid, self._composed(cid, pl)) for cid, pl in self._by_id.items()]

    # ----- raw local access (no compose) — persistence / migration -----

    def local_get(self, cid: uuid.UUID) -> ComponentPlacement | None:
        return self._by_id.get(cid)

    def local_items(self) -> list[tuple[uuid.UUID, ComponentPlacement]]:
        return list(self._by_id.items())

    def local_values(self) -> list[ComponentPlacement]:
        return list(self._by_id.values())

    # ----- key-only ops (no compose) -----

    def __iter__(self) -> Iterator[uuid.UUID]:
        return iter(self._by_id)

    def __len__(self) -> int:
        return len(self._by_id)

    def __contains__(self, cid: object) -> bool:
        return cid in self._by_id

    def __bool__(self) -> bool:
        return bool(self._by_id)

    def keys(self) -> list[uuid.UUID]:
        return list(self._by_id.keys())

    # ----- writes (store verbatim local) -----

    def __setitem__(self, cid: uuid.UUID, pl: ComponentPlacement) -> None:
        self._by_id[cid] = pl

    def __delitem__(self, cid: uuid.UUID) -> None:
        del self._by_id[cid]

    def pop(
        self, cid: uuid.UUID, *default: ComponentPlacement | None
    ) -> ComponentPlacement | None:
        return self._by_id.pop(cid, *default)

    def clear(self) -> None:
        self._by_id.clear()

    def update(
        self, other: PlacementMap | dict[uuid.UUID, ComponentPlacement]
    ) -> None:
        """Bulk-store local placements (verbatim, no compose)."""
        items = other.local_items() if isinstance(other, PlacementCollection) \
            else other.items()
        for cid, pl in items:
            self._by_id[cid] = pl

    def setdefault(
        self, cid: uuid.UUID, default: ComponentPlacement
    ) -> ComponentPlacement:
        if cid not in self._by_id:
            self._by_id[cid] = default
        return self._composed(cid, self._by_id[cid])

    def copy(self) -> PlacementCollection:
        """A shallow copy of the stored (local) placements; compose_fn reset."""
        return PlacementCollection(dict(self._by_id))


# A read surface accepting either a plain dict (loaders, snapshots, local scratch
# dicts) or the compose-on-read collection (``Layout.placements``). Helpers that
# only READ placements (origin/ownership/conflict queries, renderers) take this so
# both a dict and the collection are accepted; the type change stops at the read
# boundary instead of rippling to every caller.
PlacementMap = dict[uuid.UUID, ComponentPlacement] | PlacementCollection


def snapshot_placements(
    placements: dict[uuid.UUID, ComponentPlacement] | PlacementCollection,
    comp_ids: Iterable[uuid.UUID],
) -> dict[uuid.UUID, ComponentPlacement]:
    """Deep-copy each placement in *comp_ids* for undo.

    Single source of truth for the ``{cid: pl.copy()}`` snapshot that the
    rotate helper / drag commands previously inlined — copying preserves every
    field (position, rotation, side, offsets, flip, locked) so a later
    :func:`restore_placements` rewinds the pose exactly.  Missing ids are
    skipped.
    """
    out: dict[uuid.UUID, ComponentPlacement] = {}
    for cid in comp_ids:
        pl = placements.get(cid)
        if pl is not None:
            out[cid] = pl.copy()
    return out


def restore_placements(
    placements: PlacementMap,
    saved: dict[uuid.UUID, ComponentPlacement],
) -> None:
    """Write a batch of saved placements back (inverse of a mutation).

    The shared restore loop behind ``MoveDragSetCommand`` / ``restore_rotation``
    — they each iterated ``saved.items()`` writing ``placements[cid] = pl``.
    """
    for cid, pl in saved.items():
        placements[cid] = pl


@overload
def _rotate(p: Point, rotation: int) -> Point: ...
@overload
def _rotate(p: Vector, rotation: int) -> Vector: ...


def _rotate(p: Point | Vector, rotation: int) -> Point | Vector:
    """Rotate (x, y) clockwise by `rotation` degrees around the origin.

    Preserves the input kind: a Point rotates to a Point, a Vector (a pin
    offset) rotates to a Vector — so offset arithmetic stays in the Vector
    domain.
    """
    cls = type(p)
    x, y = (p.x, p.y) if isinstance(p, Point) else (p.dx, p.dy)
    if rotation == 0:
        return p
    if rotation == 90:
        return cls(y, -x)
    if rotation == 180:
        return cls(-x, -y)
    # 270
    return cls(-y, x)


@overload
def unrotate(p: Point, rotation: int) -> Point: ...
@overload
def unrotate(p: Vector, rotation: int) -> Vector: ...


def unrotate(p: Point | Vector, rotation: int) -> Point | Vector:
    """Reverse a clockwise rotation — equivalent to rotating by -rotation."""
    return _rotate(p, (360 - rotation) % 360)


_BODY_MARGIN = 0.5      # raster units — rigid packages (DIP, SIL, connectors)
_PAD_MARGIN  = 0.1      # raster units — flexible-lead parts (axial, TO-92, …)


def _is_flexible_body(footprint: Footprint, component_class: str) -> bool:
    """Whether the body floats above the board (only pads matter for the bbox).

    Rigidity is a property of the **package**, not the electrical class: an SMD
    resistor is class ``resistor`` but its chip body lies flat on the pads, so it
    is *not* flexible. A concrete package reports rigidity through the shared
    footprint API; bare footprints fall back to the component class (the
    ``drag_detector`` / ``auto_placer`` call sites, which never resolve the
    chosen variant).
    """
    rigid = footprint.is_rigid()
    if rigid is not None:
        return not rigid
    return component_class in _FLEXIBLE_LEAD_CLASSES


def component_bbox(
    footprint: Footprint,
    placement: ComponentPlacement,
    component_class: str = "",
) -> Rect:
    """Return the axis-aligned bounding box of the component body.

    For components with flexible leads (resistors, diodes, transistors, …)
    only the pad positions matter — the body floats above the board and does
    not block neighbouring components.  A small _PAD_MARGIN is used instead
    of _BODY_MARGIN so that 45° placements don't reserve an oversized area.
    """
    positions = [
        pin_world_position(footprint, placement, n + 1)
        for n in range(footprint.pin_count())
    ]
    xs = [p.x for p in positions]
    ys = [p.y for p in positions]
    min_x, max_x = min(xs), max(xs)
    min_y, max_y = min(ys), max(ys)
    m = _PAD_MARGIN if _is_flexible_body(footprint, component_class) else _BODY_MARGIN
    return Rect.of(
        x=min_x - m,
        y=min_y - m,
        lx=(max_x - min_x) + 2 * m,
        ly=(max_y - min_y) + 2 * m,
    )


def flip_component(
    footprint: Footprint,
    placement: ComponentPlacement,
) -> ComponentPlacement:
    """Swap pin1 and pin2 local positions via pin_offsets.

    Only valid for 2-pin footprints.  After flip:
      pin_world_position(fp, result, 1) == old pin2 world pos
      pin_world_position(fp, result, 2) == old pin1 world pos

    placement.position and rotation are untouched — the anchor (local origin)
    stays fixed, and only the pin_offsets are updated so that each pin lands
    on the other pin's previous local position.
    """
    assert footprint.pin_count() == 2, "flip_component requires a 2-pin footprint"
    fp1, fp2 = footprint.pin_positions()[0], footprint.pin_positions()[1]
    off1 = placement.pin_offsets.get(1, Vector(0.0, 0.0))
    off2 = placement.pin_offsets.get(2, Vector(0.0, 0.0))
    # local position of each pin = fp_pos + offset  (Point + Vector → Point)
    loc1 = fp1 + off1
    loc2 = fp2 + off2
    # new offset for pin n = (other pin's old local pos) − fp_pos[n]
    # (Point − Point → Vector)
    new_off1 = loc2 - fp1
    new_off2 = loc1 - fp2

    new_pl = placement.copy()
    new_pl.pin_offsets = dict(placement.pin_offsets)
    new_pl.pin_offsets[1] = new_off1
    new_pl.pin_offsets[2] = new_off2
    new_pl.vertical_flipped = not placement.vertical_flipped
    return new_pl


def flip_component_side(
    footprint: Footprint,
    placement: ComponentPlacement,
) -> ComponentPlacement:
    """Move a component to the other board side (a physical 3-D flip).

    Toggling ``side`` is the whole operation.  :func:`pin_world_position`
    reflects the bottom-side geometry about the footprint's flip axis
    (``footprint.flip_transform``), which is an *in-place* reflection — it maps
    the pin cloud onto itself.  So the pins land back on their own pads (a
    single-row part maps pin-onto-itself; a multi-row part's rows swap), with
    no change to ``position``, ``rotation`` or ``pin_offsets``.  Applying twice
    is the identity because the reflection is its own inverse.

    Pins keep their identity (no renumbering); which pad each pin occupies is a
    geometric consequence of the reflection.  The ``footprint`` argument is kept
    for API symmetry with the previous signature and future explicit-axis
    packages (the axis now lives on the footprint/package).
    """
    del footprint  # axis is applied in pin_world_position, not here
    new_pl = placement.copy()
    new_pl.side = "bottom" if placement.side == "top" else "top"
    return new_pl


def placement_transform(
    footprint: Footprint,
    placement: ComponentPlacement,
) -> Transform2D:
    """The affine local→world transform for a placed component.

    ``world = translate(position) ∘ R(rotation) ∘ M_flip(side)`` — the same
    pipeline :func:`pin_world_position` applies to each pin, expressed once as a
    matrix so callers that place whole shapes (the body renderer's silhouette)
    use the identical transform instead of reconstructing orientation from the
    world pins (which loses the flip's chirality).  ``M_flip`` is the footprint's
    in-place flip reflection on the bottom side, identity on top.  ``pin_offsets``
    are per-pin and are *not* part of this shape transform.
    """
    steps = placement.rotation // 90
    m = Transform2D.rotation(steps)
    if placement.side == "bottom":
        m = m.compose(footprint.flip_transform())
    return Transform2D.translation(
        Vector(placement.position.x, placement.position.y)
    ).compose(m)


_IDENTITY_TRANSFORM = Transform2D.identity()


def _rotation_steps_of(t: Transform2D) -> int:
    """CW 90°-steps encoded in the linear part of a det=+1 quantised transform.

    Thin alias of :meth:`Transform2D.rotation_steps` (the math lives on the
    matrix) kept for the existing call sites in this module.
    """
    return t.rotation_steps()


def _linear(t: Transform2D) -> Transform2D:
    """The linear part of *t* (drop the translation)."""
    return Transform2D(t.a, t.b, t.c, t.d, 0.0, 0.0)


def _flip_placement(
    transform: Transform2D,
    placement: ComponentPlacement,
    footprint: Footprint,
) -> ComponentPlacement:
    """Fold a group FLIP (``det < 0``) into a member-LOCAL placement.

    A group flip reflects the whole group (and physically moves every member to
    the other board side).  Expressed as affine maps over the pin cloud the
    requirement is ``placement_transform(fp, result) == transform ∘
    placement_transform(fp, placement)``, and — because
    ``placement_transform = translate(pos) ∘ R(rot) ∘ M_flip(side)`` with
    ``M_flip`` the footprint's in-place reflection ``f`` on the bottom side —
    this solves in closed form (verified for every footprint / rotation / side /
    offset combination, no ``pin_offsets`` residual needed):

    * ``side'`` is the OPPOSITE side — a group flip toggles chirality, so the
      per-member board-side flips too;
    * the linear part ``L(R') = L_transform ∘ L(R) ∘ f`` (one reflection cancels
      the toggled ``M_flip``), which is a pure 90° rotation → read ``rotation'``;
    * ``position'`` is the residual placing pin 1 on ``transform.apply(pin1)``;
    * ``pin_offsets`` are unchanged.

    The ``footprint`` supplies both the flip axis ``f`` and pin 1's local
    position; the seam resolves it via an injected resolver because
    :class:`Layout` does not resolve footprints itself.
    """
    side2: Side = "top" if placement.side == "bottom" else "bottom"
    f = _linear(footprint.flip_transform())
    lw = _linear(transform)
    lr = Transform2D.rotation(placement.rotation // 90)
    steps = _rotation_steps_of(lw.compose(lr).compose(f))
    rotation2 = (steps * 90) % 360

    # Solve position so pin 1 lands on ``transform.apply(old pin 1)``.  A probe
    # at position (0,0) with the new (rotation, side) reports where pin 1 would
    # land; the residual to the target is the position (``position`` is added
    # after rotation/flip, uniformly for every pin, so pin 1 fixes it).
    probe = placement.copy()
    probe.position = Point(0.0, 0.0)
    probe.rotation = rotation2
    probe.side = side2
    base1 = pin_world_position(footprint, probe, 1)
    want1 = transform.apply(pin_world_position(footprint, placement, 1))
    new_pl = placement.copy()
    new_pl.position = Point(want1.x - base1.x, want1.y - base1.y)
    new_pl.rotation = rotation2
    new_pl.side = side2
    # pin_offsets unchanged.
    return new_pl


def compose_placement(
    transform: Transform2D,
    placement: ComponentPlacement,
    footprint: Footprint | None = None,
) -> ComponentPlacement:
    """Fold a group WORLD transform into a member-LOCAL placement.

    Returns the effective (world) placement such that
    ``pin_world_position(fp, result, n) == transform.apply(
    pin_world_position(fp, placement, n))`` for every pin — i.e. the member's
    pins land where the group transform sends them.

    Identity fast-path returns the input unchanged (the common case: an
    ungrouped component, or a group whose transform is identity), so the ~57
    scattered readers pay nothing when nothing is grouped.

    - **det > 0** (group rotation + translation, the only kind produced by group
      rotate/move): ``position`` maps through the transform, ``rotation`` gains
      the group's 90°-steps, ``side`` and ``pin_offsets`` are unchanged (offsets
      live inside the rotate, in the same relative frame — verified by parity).
    - **det < 0** (group FLIP): toggles ``side`` and mixes the reflection into
      ``rotation`` (see :func:`_flip_placement`).  Requires ``footprint`` (for
      the flip axis + pin 1's local position); raising when it is missing keeps
      a flip loud rather than silently wrong.
    """
    if transform == _IDENTITY_TRANSFORM:
        return placement
    if transform.determinant() < 0:
        if footprint is None:
            raise NotImplementedError(
                "group flip (det<0) placement composition needs the member's "
                "footprint; none was supplied by the read seam"
            )
        return _flip_placement(transform, placement, footprint)
    steps = _rotation_steps_of(transform)
    new_pl = placement.copy()
    new_pl.position = transform.apply(placement.position)
    new_pl.rotation = (steps * 90 + placement.rotation) % 360
    # side and pin_offsets unchanged.
    return new_pl


@counted("pin_world_position")
def pin_world_position(
    footprint: Footprint,
    placement: ComponentPlacement,
    pin_number: int,
) -> Point:
    """Return the absolute world position of the given pin (1-indexed).

    ``placement.position`` is the world coordinate of the footprint origin
    (pin 1 at local (0,0), *without* any pin_offset).  Rotation is always
    around that origin, so changing rotation never moves the anchor.

    Pipeline: local_pos = fp_pos + pin_offset → X-mirror (if bottom) →
    clockwise rotation → + placement.position.
    """
    rel = footprint.pin_positions()[pin_number - 1]
    off = placement.pin_offsets.get(pin_number)
    if off is not None:
        rel = rel + off  # Point + Vector → Point
    # A component on the bottom side is physically flipped: reflect the local
    # geometry about the footprint's flip axis (``flip_transform``) before
    # rotation.  The axis runs along the pin row — a property of the geometry,
    # not of the side — so the pins stay on their pads (single-row parts map
    # onto themselves; a multi-row part's rows swap).
    if placement.side == "bottom":
        rel = footprint.flip_transform().apply(rel)
    rotated = _rotate(rel, placement.rotation)
    return Point(
        placement.position.x + rotated.x,
        placement.position.y + rotated.y,
    )


def resolve_pin_world_position(
    comp_id: uuid.UUID,
    pin_number: int,
    *,
    placements: PlacementMap,
    instances: dict[uuid.UUID, ComponentInstance],
    components: dict[str, ComponentDefinition],
    families: dict[str, PackageFamily] | None = None,
) -> Point | None:
    """Resolve world position of *(comp_id, pin_number)* with full safety chain.

    Canonical helper for every "resolve a pin to a world Point" callsite.
    Returns ``None`` when:

    * the component has no placement,
    * the instance is unknown,
    * the definition is missing, or
    * the pin number is out of range / footprint resolution raises.

    Previously each callsite (drc/engine, drag_detector, group_cmds,
    select_tool, session.get_pin_position) inlined the same chain with
    slightly different error handling.  Bug 2737f15 (DRC SHORT pointing
    at (0,0) instead of the offending pad) was a direct consequence —
    one callsite hit the missing-defn branch and silently returned a
    sentinel pad.

    Pure function — no Qt, no side effects.
    """
    placement = placements.get(comp_id)
    if placement is None:
        return None
    inst = instances.get(comp_id)
    if inst is None:
        return None
    defn = components.get(inst.definition_ref)
    if defn is None:
        return None
    try:
        from protoboard.domain.components.package import resolved_footprint
        fp = resolved_footprint(defn, inst, families)
        return pin_world_position(fp, placement, pin_number)
    except (IndexError, ValueError):
        return None


def validate_pins_on_pads(
    footprint: Footprint,
    placement: ComponentPlacement,
    pad_positions: frozenset[tuple[float, float]],
    *,
    tol: float = 1e-4,
) -> list[tuple[int, Point]]:
    """Return list of (pin_number, world_pos) for pins that don't land on any pad.

    ``pad_positions`` is a frozenset of (x, y) tuples from the board.
    An empty return list means all pins are correctly placed.
    """
    misses: list[tuple[int, Point]] = []
    for n in range(footprint.pin_count()):
        pin_num = n + 1
        wp = pin_world_position(footprint, placement, pin_num)
        if not any(
            abs(wp.x - px) < tol and abs(wp.y - py) < tol
            for px, py in pad_positions
        ):
            misses.append((pin_num, wp))
    return misses


def warn_pins_off_pads(
    comp_id: uuid.UUID,
    placement: ComponentPlacement,
    *,
    instances: dict[uuid.UUID, ComponentInstance],
    components: dict[str, ComponentDefinition],
    board: Board,
) -> None:
    """Log a warning if any pin of the component does not land on a board pad."""
    inst = instances.get(comp_id)
    if inst is None:
        return
    defn = components.get(inst.definition_ref)
    if defn is None:
        return
    from protoboard.domain.board.boxes import GapAtom
    from protoboard.domain.components.package import resolved_footprint
    pad_set: frozenset[tuple[float, float]] = frozenset(
        (center.x, center.y)
        for atom, center in board.atoms()
        if not isinstance(atom, GapAtom)
    )
    fp = resolved_footprint(defn, inst, inst.families)
    misses = validate_pins_on_pads(fp, placement, pad_set)
    if misses:
        _log.warning(
            "placement.pins_off_pads",
            comp_id=str(comp_id),
            refdes=inst.refdes,
            placement_pos=f"({placement.position.x:.4f}, {placement.position.y:.4f})",
            rotation=placement.rotation,
            misses=[
                {"pin": pin_num, "pos": f"({wp.x:.4f}, {wp.y:.4f})"}
                for pin_num, wp in misses
            ],
        )


def _pin_position_set(
    footprint: Footprint, placement: ComponentPlacement,
) -> set[tuple[float, float]]:
    """Rounded ``(x, y)`` world position of every pin — a coincidence key set.

    Rounding to the canonical coordinate precision makes two pins on the same
    pad compare equal despite float noise, so ``isdisjoint`` answers "do these
    two components share any pad?".
    """
    return {
        (round_coord(p.x), round_coord(p.y))
        for n in range(1, footprint.pin_count() + 1)
        for p in (pin_world_position(footprint, placement, n),)
    }


def move_blocked(
    comp_id: uuid.UUID,
    new_pl: ComponentPlacement,
    *,
    instances: dict[uuid.UUID, ComponentInstance],
    components: dict[str, ComponentDefinition],
    layout: Layout,
) -> bool:
    """Return True if moving *comp_id* to *new_pl* would overlap another part.

    A move is blocked when the moved component's bounding box intersects
    another's *and* none of their pins coincide — a shared pad is a legal
    soldering scenario handled by the GUI conflict resolver, not a block.
    """
    inst = instances.get(comp_id)
    if inst is None:
        return False
    defn = components.get(inst.definition_ref)
    if defn is None:
        return False

    # Geometry resolves the moved/other components' chosen variant footprints,
    # not the definition default (pin_count stays on defn — variant-invariant).
    from protoboard.domain.components.package import resolved_footprint

    fp = resolved_footprint(defn, inst, inst.families)
    new_bbox = component_bbox(fp, new_pl, defn.component_class)
    new_pin_positions = _pin_position_set(fp, new_pl)

    for other_id, other_pl in layout.placements.items():
        if other_id == comp_id:
            continue
        other_inst = instances.get(other_id)
        if other_inst is None:
            continue
        other_defn = components.get(other_inst.definition_ref)
        if other_defn is None:
            continue

        other_fp = resolved_footprint(other_defn, other_inst, other_inst.families)
        other_bbox = component_bbox(
            other_fp, other_pl, other_defn.component_class,
        )
        if new_bbox.intersects(other_bbox):
            # Only block when no pin of the moved component coincides with a pin
            # of the other — that would be a shared-pad scenario which the GUI
            # conflict resolver handles separately.
            if new_pin_positions.isdisjoint(_pin_position_set(other_fp, other_pl)):
                return True

    return False
