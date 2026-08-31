# SPDX-License-Identifier: MIT
"""QGraphicsScene renderer for the board (pads, strips, box frames).

Two-tier API:

- ``build_board_display_list(board, theme, layers)`` returns a pure
  data structure (``BoardDisplayList``) describing every primitive the
  board layer would draw — pads, holes, strip tracks, box frames,
  isolated-strip Xs.  No Qt scene state involved.

- ``paint_board(painter, display_list)`` walks the list and emits raw
  ``QPainter`` calls.  Used by the upcoming background-cache pipeline
  to rasterise the board into an offscreen pixmap (drawBackground
  refactor — see plan).

- ``render_board(scene, board, …)`` is the legacy entry point used by
  the current BoardView.refresh() path.  It builds the same display
  list and then converts every primitive into the corresponding
  ``QGraphicsItem`` so existing tests / drag interactions keep working
  unchanged.  When the background-cache lands this entry point will
  go away.
"""

from __future__ import annotations

import uuid
from typing import TYPE_CHECKING

if TYPE_CHECKING:
    from protoboard.domain.components.definition import ComponentDefinition
    from protoboard.domain.components.instance import ComponentInstance
    from protoboard.domain.layout.placement import PlacementMap
from dataclasses import dataclass, field

from PySide6.QtCore import QPointF, QRectF, Qt
from PySide6.QtGui import QBrush, QColor, QPainter, QPen
from PySide6.QtWidgets import (
    QGraphicsEllipseItem,
    QGraphicsLineItem,
    QGraphicsRectItem,
    QGraphicsScene,
)

from protoboard.controller.view_options.layer_state import Z_BOARD, LayerState
from protoboard.controller.view_options.theme import DEFAULT_THEME, Theme
from protoboard.domain.board.board import Board
from protoboard.domain.board.boxes import PadAtom, StripAtom
from protoboard.domain.board.layout_engine import LayoutNode
from protoboard.geometry import Point
from protoboard.services.rendering.qt_geometry import to_qrectf

# Per-pad colour mapping passed to ``build_board_display_list``.
# Key is the pad centre (cx, cy) rounded to 3 decimals; value is either
# a hex colour string (the resolved net colour, honouring manual
# ``net.color`` overrides) or ``None`` if the pad carries a component
# pin but no schematic net (rendered as ``theme.net_color_unassigned``).
# Pads not in the map fall back to ``theme.pad_color`` (no pin on them).
PadNetMap = dict[tuple[float, float], str | None]


def _pad_key(cx: float, cy: float) -> tuple[float, float]:
    """Stable key for pad lookup; rounds to avoid float-equality misses."""
    return (round(cx, 3), round(cy, 3))


def _resolve_pad_color(
    cx: float,
    cy: float,
    base_color: QColor,
    pad_net_map: PadNetMap | None,
    theme: Theme,
    alpha: float,
) -> QColor:
    """Return the pad fill colour given the net mapping.

    Priority:
    - pad in map with a hex colour → that colour (resolved from net.color
      override or palette by the caller).
    - pad in map with ``None`` (pin without a net) → ``theme.net_color_unassigned``.
    - pad not in map → *base_color* (theme default, no pin).

    All colours are multiplied by *alpha* (board layer alpha).
    """
    if pad_net_map is None:
        return base_color
    entry = pad_net_map.get(_pad_key(cx, cy), _NO_KEY)
    if entry is _NO_KEY:
        return base_color
    if entry is None:
        c = QColor(theme.net_color_unassigned)
    else:
        c = QColor(entry)
    c.setAlphaF(c.alphaF() * alpha)
    return c


_NO_KEY = object()  # sentinel for dict.get to distinguish missing vs None

# Re-export BOARD_BG so board_view.py can import it without knowing about theme.
BOARD_BG = QColor(DEFAULT_THEME.board_bg)

_DEFAULT_LAYERS = LayerState()


def _apply_alpha(color: QColor, alpha: float) -> QColor:
    """Multiply the colour's existing alpha channel by *alpha*.

    The previous implementation called ``setAlphaF(alpha)``, which
    replaced the colour's per-pixel alpha entirely — so any user-set
    alpha in ``theme.pad_color`` (e.g. ``#FFFF0080`` for translucent
    yellow pads) was silently overwritten by the layer's whole-board
    ghost factor.  Multiplying instead preserves the user's alpha when
    the layer is fully visible (alpha=1.0) and still attenuates pads
    correctly when the board is ghosted under a routing/placement tool.
    """
    c = QColor(color)
    c.setAlphaF(c.alphaF() * alpha)
    return c


# ---------------------------------------------------------------------------
# Primitive descriptors — pure data, painted by ``paint_board``
# ---------------------------------------------------------------------------


@dataclass(slots=True)
class _Pad:
    """One pad ring + matching hole.  Both circles share the outline pen."""

    cx: float
    cy: float
    r: float
    fill: QColor
    outline_pen: QPen
    hole_fill: QColor

    @property
    def hr(self) -> float:
        return self.r * 0.45


@dataclass(slots=True)
class _Line:
    """Single straight stroke (isolated-strip X arm)."""

    x1: float
    y1: float
    x2: float
    y2: float
    pen: QPen


@dataclass(slots=True)
class _FilledRect:
    """Filled and outlined rectangle (strip track underlay)."""

    rect: QRectF
    brush: QBrush
    pen: QPen


@dataclass(slots=True)
class _FrameRect:
    """Stroked-only rectangle (box frame outline)."""

    rect: QRectF
    pen: QPen


@dataclass(slots=True)
class BoardDisplayList:
    """Flat descriptors for everything the board layer paints.

    Order of buckets in this dataclass mirrors the Z-order used by
    ``paint_board``: tracks (under pads) → frame outlines → pad rings
    → isolated-strip X strokes (above pad rings) → holes (top-most).
    """

    filled_rects: list[_FilledRect] = field(default_factory=list)
    frame_rects: list[_FrameRect] = field(default_factory=list)
    pads: list[_Pad] = field(default_factory=list)
    lines: list[_Line] = field(default_factory=list)


# ---------------------------------------------------------------------------
# Build phase — produce a display list from board + theme + layers
# ---------------------------------------------------------------------------


def _build_pad(
    dl: BoardDisplayList,
    cx: float,
    cy: float,
    r: float,
    color: QColor,
    theme: Theme,
    *,
    pad_net_map: PadNetMap | None = None,
    board_alpha: float = 1.0,
) -> None:
    fill = _resolve_pad_color(cx, cy, color, pad_net_map, theme, board_alpha)
    outline = QColor(theme.pad_outline_color)
    outline.setAlphaF(fill.alphaF())
    outline_pen = QPen(outline, theme.pad_outline_width)
    hole_color = QColor(theme.hole_color)
    hole_color.setAlphaF(fill.alphaF())
    dl.pads.append(_Pad(cx, cy, r, fill, outline_pen, hole_color))


def _build_strip(
    dl: BoardDisplayList,
    atom: StripAtom,
    center: Point,
    strip_color: QColor,
    isolated_color: QColor,
    theme: Theme,
    *,
    pad_net_map: PadNetMap | None = None,
    board_alpha: float = 1.0,
) -> None:
    length = atom.length if isinstance(atom.length, int) else 1

    if atom.isolated:
        cx, cy = center.x, center.y
        # Isolated strip cell keeps its theme isolated_color regardless of
        # net mapping — the "X" semantics override per-net colouring.
        _build_pad(dl, cx, cy, theme.strip_radius, isolated_color, theme)
        r = theme.strip_radius * 0.7
        pen = QPen(QColor(theme.isolated_x_color), theme.isolated_x_width)
        for dx, dy in ((r, r), (-r, r)):
            dl.lines.append(_Line(cx - dx, cy - dy, cx + dx, cy + dy, pen))
        return

    track_h = theme.strip_radius * 1.1
    track_color = QColor(strip_color)
    track_pen = QPen(QColor(theme.strip_track_outline), theme.strip_track_outline_width)
    if atom.orientation == "horizontal":
        start_x = center.x - length / 2.0 + 0.5
        end_x = start_x + length - 1
        dl.filled_rects.append(
            _FilledRect(
                QRectF(
                    start_x - theme.strip_radius,
                    center.y - track_h,
                    (end_x - start_x) + 2 * theme.strip_radius,
                    2 * track_h,
                ),
                QBrush(track_color),
                track_pen,
            )
        )
        for i in range(length):
            _build_pad(
                dl,
                start_x + i,
                center.y,
                theme.strip_radius,
                strip_color,
                theme,
                pad_net_map=pad_net_map,
                board_alpha=board_alpha,
            )
    else:
        start_y = center.y - length / 2.0 + 0.5
        end_y = start_y + length - 1
        track_w = track_h
        dl.filled_rects.append(
            _FilledRect(
                QRectF(
                    center.x - track_w,
                    start_y - theme.strip_radius,
                    2 * track_w,
                    (end_y - start_y) + 2 * theme.strip_radius,
                ),
                QBrush(track_color),
                track_pen,
            )
        )
        for i in range(length):
            _build_pad(
                dl,
                center.x,
                start_y + i,
                theme.strip_radius,
                strip_color,
                theme,
                pad_net_map=pad_net_map,
                board_alpha=board_alpha,
            )


def _build_frames(
    dl: BoardDisplayList,
    node: LayoutNode,
    *,
    is_root: bool,
    theme: Theme,
    alpha: float,
) -> None:
    from protoboard.domain.board.boxes import BoardGrid, HBox, VBox

    if is_root or isinstance(node.box, (HBox, VBox, BoardGrid)):
        rect = to_qrectf(node.bounds)
        if is_root:
            c = QColor(theme.board_frame_color)
            c.setAlphaF(c.alphaF() * alpha)
            pen = QPen(c, theme.board_frame_width)
        else:
            c = QColor(theme.board_sub_frame_color)
            c.setAlphaF(c.alphaF() * alpha)
            pen = QPen(c, theme.board_sub_frame_width)
            pen.setStyle(Qt.PenStyle.DashLine)
        dl.frame_rects.append(_FrameRect(rect, pen))

    for child in node.children:
        _build_frames(dl, child, is_root=False, theme=theme, alpha=alpha)


def build_board_display_list(
    board: Board,
    *,
    show_frames: bool = True,
    theme: Theme = DEFAULT_THEME,
    layers: LayerState = _DEFAULT_LAYERS,
    pad_net_map: PadNetMap | None = None,
) -> BoardDisplayList:
    """Return a flat descriptor list for everything the board layer draws.

    Pure: no Qt scene state, no scene allocation.  Suitable for either
    rasterising into an offscreen pixmap (background-cache pipeline) or
    converting back into ``QGraphicsItem`` instances (legacy scene path,
    via :func:`render_board`).

    *pad_net_map* maps pad centre ``(cx, cy)`` to its electrical net.  A
    ``None`` value means a component pin sits on the pad but the pin is
    unassigned (rendered as ``theme.net_color_unassigned`` — same as a wire
    with no net).  Pads missing from the map keep ``theme.pad_color``.
    Enables "ghost components" workflows: with the components layer hidden,
    pads still carry net colours so the user can read connectivity.
    """
    dl = BoardDisplayList()
    board_alpha = layers.alpha_for_board()
    if board_alpha == 0.0:
        return dl

    pad_color = _apply_alpha(QColor(theme.pad_color), board_alpha)
    strip_color = _apply_alpha(QColor(theme.strip_color), board_alpha)
    isolated_color = _apply_alpha(QColor(theme.isolated_color), board_alpha)

    if show_frames:
        _build_frames(dl, board.layout, is_root=True, theme=theme, alpha=board_alpha)
    for atom, pos in board.atoms():
        if isinstance(atom, PadAtom):
            # Standalone PadAtom: net colouring moved to the pin-pads
            # layer (see ``gui/renderers/pin_pad_renderer.py``).  The
            # board layer paints the bare ring + hole only.  Passing
            # ``pad_net_map=None`` here makes ``_resolve_pad_color``
            # fall back to ``theme.pad_color`` regardless of pin presence.
            _build_pad(
                dl,
                pos.x,
                pos.y,
                theme.pad_radius,
                pad_color,
                theme,
                pad_net_map=None,
                board_alpha=board_alpha,
            )
        elif isinstance(atom, StripAtom):
            # Strip tracks DO keep net colouring (A3 from the design
            # discussion): the whole strip is electrically the net, so
            # the track colour visually signals which net it carries.
            # Pin-pads layer adds the pin-specific overlay on top.
            _build_strip(
                dl,
                atom,
                pos,
                strip_color,
                isolated_color,
                theme,
                pad_net_map=pad_net_map,
                board_alpha=board_alpha,
            )
    return dl


# ---------------------------------------------------------------------------
# Paint phase — render a display list via raw QPainter
# ---------------------------------------------------------------------------


def paint_board(painter: QPainter, display_list: BoardDisplayList) -> None:
    """Render *display_list* via direct ``QPainter`` calls.

    Bucket order matches the Z-order of the scene-item path: tracks are
    drawn first (under pads), then frame outlines, then pad rings, then
    isolated-strip Xs, finally holes on top.  This routine assumes the
    painter is already transformed into world coordinates (the caller
    sets up scale / translate before invoking it).
    """
    # Filled track rectangles (under pads on strips).
    for fr in display_list.filled_rects:
        painter.setBrush(fr.brush)
        painter.setPen(fr.pen)
        painter.drawRect(fr.rect)
    # Frame outlines.
    painter.setBrush(Qt.BrushStyle.NoBrush)
    for fr2 in display_list.frame_rects:
        painter.setPen(fr2.pen)
        painter.drawRect(fr2.rect)
    # Pad rings.
    for p in display_list.pads:
        painter.setBrush(QBrush(p.fill))
        painter.setPen(p.outline_pen)
        painter.drawEllipse(
            QRectF(
                p.cx - p.r,
                p.cy - p.r,
                2 * p.r,
                2 * p.r,
            )
        )
    # Isolated-strip X strokes (above pad rings).
    for line in display_list.lines:
        painter.setPen(line.pen)
        painter.drawLine(QPointF(line.x1, line.y1), QPointF(line.x2, line.y2))
    # Holes (top-most).
    for p in display_list.pads:
        painter.setBrush(QBrush(p.hole_fill))
        painter.setPen(p.outline_pen)
        painter.drawEllipse(
            QRectF(
                p.cx - p.hr,
                p.cy - p.hr,
                2 * p.hr,
                2 * p.hr,
            )
        )


# ---------------------------------------------------------------------------
# Legacy scene-based renderer — converts display list to QGraphicsItems
# ---------------------------------------------------------------------------


def render_board(
    scene: QGraphicsScene,
    board: Board,
    *,
    show_frames: bool = True,
    theme: Theme = DEFAULT_THEME,
    layers: LayerState = _DEFAULT_LAYERS,
    pad_net_map: PadNetMap | None = None,
) -> None:
    """Add board pad/strip items to the scene (does not clear first).

    When the active tool ghosts components (routing / placement) the board is
    also rendered with reduced alpha so back-side elements remain readable
    underneath it.

    This entry point survives for the existing ``BoardView.refresh()``
    path until the background-cache refactor migrates the scene off of
    these items entirely.  Internally it just builds a display list and
    converts each primitive to its matching ``QGraphicsItem``.
    """
    dl = build_board_display_list(
        board,
        show_frames=show_frames,
        theme=theme,
        layers=layers,
        pad_net_map=pad_net_map,
    )
    # Filled rects (strip tracks) — drawn under pads.
    for fr in dl.filled_rects:
        item = QGraphicsRectItem(fr.rect)
        item.setBrush(fr.brush)
        item.setPen(fr.pen)
        item.setZValue(Z_BOARD - 0.1)
        scene.addItem(item)
    # Frame rects (box outlines) — drawn just under board pads.
    for fr2 in dl.frame_rects:
        frame_item = QGraphicsRectItem(fr2.rect)
        frame_item.setBrush(QBrush(QColor(0, 0, 0, 0)))
        frame_item.setPen(fr2.pen)
        frame_item.setZValue(Z_BOARD - 0.2)
        scene.addItem(frame_item)
    # Pads (ring + hole).
    for p in dl.pads:
        ring = QGraphicsEllipseItem(
            QRectF(p.cx - p.r, p.cy - p.r, 2 * p.r, 2 * p.r),
        )
        ring.setBrush(QBrush(p.fill))
        ring.setPen(p.outline_pen)
        ring.setZValue(Z_BOARD)
        scene.addItem(ring)
        hole = QGraphicsEllipseItem(
            QRectF(p.cx - p.hr, p.cy - p.hr, 2 * p.hr, 2 * p.hr),
        )
        hole.setBrush(QBrush(p.hole_fill))
        hole.setPen(p.outline_pen)
        hole.setZValue(Z_BOARD + 0.05)
        scene.addItem(hole)
    # Isolated-strip X strokes.
    for line in dl.lines:
        line_item = QGraphicsLineItem(line.x1, line.y1, line.x2, line.y2)
        line_item.setPen(line.pen)
        line_item.setZValue(Z_BOARD)
        scene.addItem(line_item)


def build_pad_net_map(
    placements: PlacementMap,
    instances: dict[uuid.UUID, ComponentInstance],
    components: dict[str, ComponentDefinition],
    schematic: object,
    *,
    theme: Theme = DEFAULT_THEME,
    net_color_map: dict[uuid.UUID, str] | None = None,
) -> PadNetMap:
    """Return a mapping ``(cx, cy) → hex_color|None`` for every component pin.

    The colour honours manual ``net.color`` overrides via
    *net_color_map* (typically computed by
    :func:`protoboard.services.rendering.net_color.build_net_color_map`).
    If *net_color_map* is omitted, a fresh one is built from *schematic*
    using *theme* as the palette source.

    A pin entry whose net is unknown in *schematic* maps to ``None``
    (rendered as ``theme.net_color_unassigned``).  Pads with no pin on
    them are simply absent from the result (rendered as ``theme.pad_color``).

    Arguments are typed loosely to keep this module free of cycles with
    domain layout/schematic; in practice the caller passes
    ``session.layout.placements``, ``session.instances``,
    ``session.components``, ``session.schematic``.
    """
    from protoboard.domain.components.package import meta_pin_for_package_pin
    from protoboard.domain.layout.pad_conflict import (
        resolved_footprint_or_default,
    )
    from protoboard.domain.layout.placement import pin_world_position
    from protoboard.domain.schematic.net import PinRef
    from protoboard.services.rendering.net_color import build_net_color_map

    pin_to_net: dict[PinRef, uuid.UUID] = {}
    nets = getattr(schematic, "nets", None)
    if nets:
        for net in nets.values():
            for ref in net.pins:
                pin_to_net[ref] = net.id

    if net_color_map is None and nets:
        net_color_map = build_net_color_map(nets, theme=theme)
    if net_color_map is None:
        net_color_map = {}

    result: PadNetMap = {}
    for comp_id, placement in placements.items():
        inst = instances.get(comp_id)
        if inst is None:
            continue
        defn = components.get(inst.definition_ref)
        if defn is None:
            continue
        fp = resolved_footprint_or_default(defn, inst)
        for n in range(fp.pin_count()):
            pin_number = n + 1
            pos = pin_world_position(fp, placement, pin_number)
            meta_pin_index = meta_pin_for_package_pin(defn, inst, pin_number)
            if meta_pin_index is None:
                continue
            ref = PinRef(component_id=comp_id, pin_number=meta_pin_index)
            net_id = pin_to_net.get(ref)
            if net_id is None:
                # Pin without a net — None marker triggers
                # ``theme.net_color_unassigned`` in the renderer.
                result[_pad_key(pos.x, pos.y)] = None
            else:
                result[_pad_key(pos.x, pos.y)] = net_color_map.get(net_id)
    return result


__all__ = [
    "BOARD_BG",
    "BoardDisplayList",
    "PadNetMap",
    "build_board_display_list",
    "build_pad_net_map",
    "paint_board",
    "render_board",
]
