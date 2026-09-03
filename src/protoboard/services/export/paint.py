# SPDX-License-Identifier: MIT
"""Headless board rendering pipeline.

Builds an off-screen :class:`QGraphicsScene` populated by the same
renderers BoardView uses, then rasterises it onto an arbitrary
:class:`QPainter` (SVG generator, PDF printer, raster image).  Used by
SVG / PDF / PNG export and by the multi-panel template PDF.

No fingerprint caching, no incremental updates — every call builds a
fresh scene from the snapshot.  Export is one-shot; the cost of a
rebuild is dwarfed by the actual rasterisation.
"""

from __future__ import annotations

import uuid
from dataclasses import replace as _dc_replace
from typing import cast

from PySide6.QtCore import QRectF, Qt
from PySide6.QtGui import QBrush, QColor, QPainter, QPen
from PySide6.QtWidgets import QGraphicsRectItem, QGraphicsScene

from protoboard.controller.view_options.layers import LayerVisibility
from protoboard.domain.connectivity.incremental import ConnectivityGraph
from protoboard.domain.connectivity.wire_net_map import project_wire_net_map
from protoboard.domain.layout.knot import WirePinKnot
from protoboard.domain.layout.pin_pad_classification import (
    classify_pin_pads,
)
from protoboard.geometry import Insets
from protoboard.services.export.snapshot import BoardSnapshot
from protoboard.services.rendering.board_renderer import (
    build_pad_net_map,
    render_board,
)
from protoboard.services.rendering.component_renderer import (
    render_components,
    render_groups,
)
from protoboard.services.rendering.net_color import build_net_color_map
from protoboard.services.rendering.pin_pad_renderer import (
    render_pin_modifiers,
    render_pin_pads,
)
from protoboard.services.rendering.qt_geometry import to_qrectf
from protoboard.services.rendering.wire_renderer import (
    render_ratsnest,
    render_wire_pins,
    render_wires_with_nets,
)
from protoboard.services.routing.net_status import routed_net_ids


def build_export_scene(snapshot: BoardSnapshot) -> tuple[QGraphicsScene, QRectF]:
    """Construct a one-shot scene + scene-rect from *snapshot*.

    Returns the populated scene plus the world-space rectangle that
    covers the board with a one-raster margin — callers use this rect
    as the ``source`` argument to :meth:`QGraphicsScene.render` so the
    output frames the board itself, ignoring scene padding that
    BoardView uses for free panning.
    """
    scene = QGraphicsScene()

    theme = _dc_replace(
        snapshot.theme,
        raster_pitch_mm=snapshot.board.raster_pitch_mm,
        package_families=snapshot.package_families,
    )

    # Canvas background.  ``board_bg`` is QGraphicsView's brush in the
    # live editor; mirror it as scene background so areas outside the
    # board frame (margins, mirror gutters) match.
    scene.setBackgroundBrush(QBrush(QColor(theme.board_bg)))

    layers = snapshot.layer_state()

    # ── Board layer (pads, strips, frames, isolated-strip Xs) ───────────────
    pad_color_map: dict[uuid.UUID, str] = build_net_color_map(
        snapshot.schematic.nets, theme=theme
    )
    # Pin / wire renderers declare ``net_color_map`` with the wider, invariant
    # type ``dict[UUID, str | None]``; a ``dict[UUID, str]`` is a valid value
    # for their read-only use but the invariance makes Pyright reject it, so we
    # cast once up front.
    pad_color_map_opt = cast("dict[uuid.UUID, str | None]", pad_color_map)
    pad_net_map = build_pad_net_map(
        snapshot.layout.placements,
        snapshot.instances,
        snapshot.components,
        snapshot.schematic,
        theme=theme,
        net_color_map=pad_color_map,
    )
    render_board(
        scene,
        snapshot.board,
        theme=theme,
        layers=layers,
        pad_net_map=pad_net_map,
    )

    # ── Back-side filter ────────────────────────────────────────────────────
    # Half-transparent black wash painted between back-side items (back
    # wires Z=2, back components Z=1) and the board (Z=3).  Mirrors the
    # live BoardView sandwich so PNG / SVG / PDF exports match what the
    # user sees in the editor.  Sits at Z=2.5 so the board pads paint
    # over it; back-side items underneath get visually pushed away.
    alpha_back_filter = layers.alpha_for_back_filter()
    if alpha_back_filter > 0.0:
        b = snapshot.board.bounds
        filter_rect = QGraphicsRectItem(
            to_qrectf(b.inflate(Insets.all(1.0))),
        )
        filter_color = QColor(theme.back_filter_color)
        filter_color.setAlphaF(alpha_back_filter)
        filter_rect.setBrush(QBrush(filter_color))
        filter_rect.setPen(QPen(Qt.PenStyle.NoPen))
        filter_rect.setZValue(2.5)
        scene.addItem(filter_rect)

    # ── Pin-pads layer (schematic-aware overlay above board) ────────────────
    classifications = classify_pin_pads(snapshot.pad_index, snapshot.layout)
    render_pin_pads(
        scene,
        classifications,
        theme=theme,
        layers=layers,
        net_color_map=pad_color_map_opt,
        routing_target_net_id=snapshot.routing_net_id,
    )

    # ── Wires ───────────────────────────────────────────────────────────────
    graph, wire_net_map = _build_graph_and_wire_net_map(snapshot)
    render_wires_with_nets(
        scene,
        list(snapshot.layout.wires),
        wire_net_map,
        net_color_map=pad_color_map_opt,
        theme=theme,
        layers=layers,
        highlighted_net_id=None,
    )

    # ── Wire-pin glyphs ─────────────────────────────────────────────────────
    wire_pins = [
        (k.position, k.net_id)
        for k in snapshot.layout.knots.values()
        if isinstance(k, WirePinKnot)
    ]
    if wire_pins:
        render_wire_pins(
            scene,
            wire_pins,
            net_color_map=pad_color_map_opt,
            theme=theme,
            layers=layers,
            routing_target_net_id=snapshot.routing_net_id,
        )

    # ── Components ──────────────────────────────────────────────────────────
    routed: set[uuid.UUID] | None = None
    if snapshot.schematic.nets and graph is not None:
        routed = routed_net_ids(snapshot.schematic, graph)

    render_components(
        scene,
        snapshot.layout.placements,
        snapshot.instances,
        snapshot.components,
        schematic=snapshot.schematic if snapshot.schematic.nets else None,
        routed_nets=routed,
        theme=theme,
        layers=layers,
        active_group_id=snapshot.active_group_id,
        layout=snapshot.layout,
        highlighted_net_id=None,
    )

    # ── Ratsnest + groups + axis labels (overlays) ──────────────────────────
    if (
        snapshot.schematic.nets
        and snapshot.layer_overrides.ratsnest != LayerVisibility.HIDDEN
        and graph is not None
    ):
        render_ratsnest(
            scene,
            snapshot.schematic,
            snapshot.layout.placements,
            snapshot.instances,
            snapshot.components,
            routed_nets=routed,
            theme=theme,
            graph=graph,
            mode="full",
            ghost=False,
        )

    if snapshot.layout.groups:
        render_groups(
            scene,
            snapshot.layout,
            snapshot.layout.placements,
            snapshot.instances,
            snapshot.components,
            wires=list(snapshot.layout.wires),
            theme=theme,
            active_group_id=snapshot.active_group_id,
            view_scale=1.0,
            wire_net_map=wire_net_map,
            schematic=snapshot.schematic,
        )

    # ── Pin-modifier overlay (topmost) ──────────────────────────────────────
    render_pin_modifiers(
        scene,
        classifications,
        theme=theme,
        layers=layers,
        net_color_map=pad_color_map_opt,
        routing_target_net_id=snapshot.routing_net_id,
    )

    # Scene rect: board bounds + 1-raster margin (matches SVG export
    # convention so file dimensions stay predictable across formats).
    b = snapshot.board.bounds
    source_rect = to_qrectf(b.inflate(Insets.all(1.0)))
    scene.setSceneRect(source_rect)
    return scene, source_rect


def _build_graph_and_wire_net_map(
    snapshot: BoardSnapshot,
) -> tuple[ConnectivityGraph | None, dict[uuid.UUID, uuid.UUID]]:
    """Build a ConnectivityGraph + wire-id → net-id map for *snapshot*.

    Both are needed by several renderers (wires, ratsnest, components);
    building once and threading both through avoids quadratic graph
    rebuilds.  Returns ``(None, {})`` if the snapshot has no schematic
    nets — the renderers handle that branch already.

    The algorithm mirrors
    :func:`protoboard.domain.connectivity.wire_net_map.build_wire_net_map`
    but operates on the snapshot's frozen state instead of a live
    session.
    """
    if not snapshot.schematic.nets or not snapshot.layout.wires:
        return None, {}
    graph = ConnectivityGraph(
        snapshot.board,
        snapshot.layout,
        snapshot.components,
        snapshot.instances,
    )
    return graph, project_wire_net_map(snapshot.schematic, snapshot.layout, graph)


def paint_snapshot(
    painter: QPainter,
    snapshot: BoardSnapshot,
    target_rect: QRectF,
) -> None:
    """Render *snapshot* onto *painter* mapping the board into *target_rect*.

    The painter must already be set up with whatever world transform
    its output device requires (e.g. mm-to-pt for PDF).  ``target_rect``
    is the destination rectangle in painter coordinates; the board's
    scene rect is uniformly scaled into it preserving aspect ratio.

    Anti-aliasing is enabled on the painter for the duration of the
    call and restored to its previous value before returning.
    """
    scene, source_rect = build_export_scene(snapshot)
    prev_aa = painter.testRenderHint(QPainter.RenderHint.Antialiasing)
    painter.setRenderHint(QPainter.RenderHint.Antialiasing, True)
    try:
        scene.render(
            painter,
            target_rect,
            source_rect,
            Qt.AspectRatioMode.KeepAspectRatio,
        )
    finally:
        painter.setRenderHint(QPainter.RenderHint.Antialiasing, prev_aa)


__all__ = ["build_export_scene", "paint_snapshot"]
