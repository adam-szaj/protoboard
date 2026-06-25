# SPDX-License-Identifier: MIT
"""ComponentPlacement and pin_world_position."""

from __future__ import annotations

import uuid
from dataclasses import dataclass, field
from typing import TYPE_CHECKING

import structlog

from protoboard.domain.components.footprints import Footprint
from protoboard.types import Side
from protoboard.geometry import Point, Rect
from protoboard.services.call_counters import counted

if TYPE_CHECKING:
    from protoboard.domain.board.board import Board
    from protoboard.domain.components.definition import ComponentDefinition
    from protoboard.domain.components.instance import ComponentInstance
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
    label_offset: Point = field(default_factory=lambda: Point(0.05, 0.05))
    pin_offsets: dict[int, Point] = field(default_factory=dict)
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


def _rotate(p: Point, rotation: int) -> Point:
    """Rotate (x, y) clockwise by `rotation` degrees around the origin."""
    x, y = p.x, p.y
    if rotation == 0:
        return p
    if rotation == 90:
        return Point(y, -x)
    if rotation == 180:
        return Point(-x, -y)
    # 270
    return Point(-y, x)


def unrotate(p: Point, rotation: int) -> Point:
    """Reverse a clockwise rotation — equivalent to rotating by -rotation."""
    return _rotate(p, (360 - rotation) % 360)


_BODY_MARGIN = 0.5      # raster units — rigid packages (DIP, SIL, connectors)
_PAD_MARGIN  = 0.1      # raster units — flexible-lead parts (axial, TO-92, …)


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
    m = _PAD_MARGIN if component_class in _FLEXIBLE_LEAD_CLASSES else _BODY_MARGIN
    return Rect(
        x=min_x - m,
        y=min_y - m,
        width=(max_x - min_x) + 2 * m,
        height=(max_y - min_y) + 2 * m,
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
    off1 = placement.pin_offsets.get(1, Point(0.0, 0.0))
    off2 = placement.pin_offsets.get(2, Point(0.0, 0.0))
    # local position of each pin = fp_pos + offset
    loc1 = Point(fp1.x + off1.x, fp1.y + off1.y)
    loc2 = Point(fp2.x + off2.x, fp2.y + off2.y)
    # new offset for pin n = (other pin's old local pos) - fp_pos[n]
    new_off1 = Point(loc2.x - fp1.x, loc2.y - fp1.y)
    new_off2 = Point(loc1.x - fp2.x, loc1.y - fp2.y)

    new_pl = placement.copy()
    new_pl.pin_offsets = dict(placement.pin_offsets)
    new_pl.pin_offsets[1] = new_off1
    new_pl.pin_offsets[2] = new_off2
    new_pl.vertical_flipped = not placement.vertical_flipped
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
        rel = Point(rel.x + off.x, rel.y + off.y)
    # Components mounted on the bottom side are physically flipped — their
    # local X axis is mirrored relative to the top view.  We apply this flip
    # before rotation so that rotation=0 on bottom means "same anchor point,
    # mirrored geometry", matching real-world PCB conventions.
    if placement.side == "bottom":
        rel = Point(-rel.x, rel.y)
    rotated = _rotate(rel, placement.rotation)
    return Point(
        placement.position.x + rotated.x,
        placement.position.y + rotated.y,
    )


def resolve_pin_world_position(
    comp_id: uuid.UUID,
    pin_number: int,
    *,
    placements: dict[uuid.UUID, ComponentPlacement],
    instances: "dict[uuid.UUID, ComponentInstance]",
    components: "dict[str, ComponentDefinition]",
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
        return pin_world_position(defn.footprint, placement, pin_number)
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
    pad_set: frozenset[tuple[float, float]] = frozenset(
        (center.x, center.y)
        for atom, center in board.atoms()
        if not isinstance(atom, GapAtom)
    )
    misses = validate_pins_on_pads(defn.footprint, placement, pad_set)
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

    new_bbox = component_bbox(defn.footprint, new_pl, defn.component_class)

    n_pins = defn.footprint.pin_count()
    new_pin_positions: set[tuple[float, float]] = set()
    for pin_num in range(1, n_pins + 1):
        p = pin_world_position(defn.footprint, new_pl, pin_num)
        new_pin_positions.add((round(p.x, 4), round(p.y, 4)))

    for other_id, other_pl in layout.placements.items():
        if other_id == comp_id:
            continue
        other_inst = instances.get(other_id)
        if other_inst is None:
            continue
        other_defn = components.get(other_inst.definition_ref)
        if other_defn is None:
            continue

        other_bbox = component_bbox(
            other_defn.footprint, other_pl, other_defn.component_class,
        )
        if new_bbox.intersects(other_bbox):
            # Only block when no pin of the moved component coincides with a pin
            # of the other — that would be a shared-pad scenario which the GUI
            # conflict resolver handles separately.
            other_pin_positions: set[tuple[float, float]] = set()
            for pin_num in range(1, other_defn.footprint.pin_count() + 1):
                p = pin_world_position(other_defn.footprint, other_pl, pin_num)
                other_pin_positions.add((round(p.x, 4), round(p.y, 4)))
            if new_pin_positions.isdisjoint(other_pin_positions):
                return True

    return False
