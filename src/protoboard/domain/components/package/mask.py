# SPDX-License-Identifier: MIT
"""Collision masks — side-aware per-hole bits + AND-based collision.

See ``docs/footprints/package-model.md`` and ``package_interface_sketch.py``. Collision
between two components = bitwise AND of their masks per hole; non-zero → physical
conflict. The bits are arranged so this AND suffices: a body that BLOCKS mounting
beneath it lights the *pin-side* bits in its holes, so "low part under blocking
body" falls out of the same AND.

Pure domain (no Qt). Sides use the project's LayerId TOP/BOTTOM strings.
"""

from __future__ import annotations

from enum import IntFlag

from protoboard.domain.board.layer import BOTTOM, TOP, LayerId
from protoboard.domain.components.package.shapes import Package, bbox_or_none
from protoboard.geometry import Point


class PadCell(IntFlag):
    """Side-aware bits for a single hole (grid domain).

    Bits encode physical MOUNT-side occupancy (which side something is inserted
    from), not electrical presence — THT through-hole electrical connectivity
    lives in PadIndex/connectivity, not here. Collision = AND of two masks ≠ 0.

    A blocking body lights PIN_{side} in its holes (see build_pad_mask), so a
    part mounted under it collides via the same AND; a high-standoff body sets
    only BODY_{side}, leaving the pin-side bits free.
    """

    PIN         = 1 << 0   # a pad/pin occupies this hole (pin-pin: check net)
    PIN_TOP     = 1 << 1   # pin mounted from the top side
    PIN_BOTTOM  = 1 << 2   # pin mounted from the bottom side
    BODY_TOP    = 1 << 3   # body mounted from the top side
    BODY_BOTTOM = 1 << 4   # body mounted from the bottom side


def _pin_side_bit(side: LayerId) -> PadCell:
    if side == TOP:
        return PadCell.PIN_TOP
    if side == BOTTOM:
        return PadCell.PIN_BOTTOM
    raise ValueError(f"unknown side: {side!r}")


def _body_side_bit(side: LayerId) -> PadCell:
    if side == TOP:
        return PadCell.BODY_TOP
    if side == BOTTOM:
        return PadCell.BODY_BOTTOM
    raise ValueError(f"unknown side: {side!r}")


def has_collision(a: PadCell, b: PadCell) -> bool:
    """Two holes collide iff their masks share any bit. The PIN bit is a
    'check the net' signal (same-net pin-pin may be legal), not always hard."""
    return bool(a & b)


def masks_collide(
    a: dict[Point, PadCell], b: dict[Point, PadCell]
) -> dict[Point, PadCell]:
    """Per-hole bitwise AND of two component masks; entries with non-zero result.

    The returned dict is the collision set: each key is a contested hole, each
    value the offending shared bits. Empty dict → no collision.
    """
    out: dict[Point, PadCell] = {}
    for hole, bits_a in a.items():
        bits_b = b.get(hole)
        if bits_b is None:
            continue
        shared = bits_a & bits_b
        if shared:
            out[hole] = shared
    return out


def _hole_key(p: Point) -> Point:
    """Snap a continuous position to its hole (integer raster cell)."""
    return Point(round(p.x), round(p.y))


def body_holes(package: Package) -> list[Point]:
    """Local-frame holes (grid domain) covered by the package body, minus pins.

    The integer holes under the body bbox that are *not* a pin hole — i.e. the
    pads a placed component would mark with a ``ComponentBody`` blocker. Returned
    in the package's own frame (pin 1 at the origin, no rotation); the caller
    applies the placement transform. Pure geometry, side-independent: which board
    side the body sits on and whether it blocks routing are the caller's concern.

    A package whose body extent cannot be derived (no explicit dims and no
    margin) covers no holes — the part occupies only its pins.
    """
    pin_holes = {_hole_key(p) for p in package.pin_positions()}
    bb = bbox_or_none(package)
    if bb is None:
        return []
    x0, y0 = round(bb.x), round(bb.y)
    x1, y1 = round(bb.x + bb.width), round(bb.y + bb.height)
    out: list[Point] = []
    for hx in range(x0, x1 + 1):
        for hy in range(y0, y1 + 1):
            hole = Point(float(hx), float(hy))
            if hole in pin_holes:
                continue
            out.append(hole)
    return out


def build_pad_mask(
    package: Package,
    *,
    mount_side: LayerId = TOP,
    body_blocks: bool = False,
) -> dict[Point, PadCell]:
    """Build the side-aware pad mask for a placed component.

    - Each pin hole gets ``PIN | PIN_{mount_side}``.
    - Each hole under the body bbox gets ``BODY_{mount_side}``.
    - If ``body_blocks`` (metal / flush body, nothing fits under it), body holes
      ALSO light ``PIN_{mount_side}`` — so a part mounted on that side under the
      body collides via the same AND. A high-standoff body leaves this unset.

    Positions are snapped to holes (``round``); the package returns continuous
    grid coordinates (physics domain) and the mask lives on the discrete grid.
    """
    mask: dict[Point, PadCell] = {}
    pin_bit = _pin_side_bit(mount_side)
    body_bit = _body_side_bit(mount_side)

    # Pins.
    for pos in package.pin_positions():
        hole = _hole_key(pos)
        mask[hole] = mask.get(hole, PadCell(0)) | PadCell.PIN | pin_bit

    # Body footprint. A package with no derivable body extent (no explicit
    # dims, no margin) masks its pins only.
    bb = bbox_or_none(package)
    if bb is None:
        return mask
    x0, y0 = round(bb.x), round(bb.y)
    x1, y1 = round(bb.x + bb.width), round(bb.y + bb.height)
    for hx in range(x0, x1 + 1):
        for hy in range(y0, y1 + 1):
            hole = Point(float(hx), float(hy))
            bits = body_bit
            if body_blocks:
                bits |= pin_bit
            mask[hole] = mask.get(hole, PadCell(0)) | bits

    return mask
