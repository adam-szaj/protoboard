# SPDX-License-Identifier: MIT
"""Parity tests pinning the fragment-aware wire renderer (v2).

The swap guard required by CLAUDE.md §8 (rewrites-and-swaps): these pin the
per-fragment behaviour — per-side Z + alpha and via markers at side
transitions — BEFORE ``scene_composer`` is repointed from the v1 path, so
the swap cannot silently regress it.
"""

from __future__ import annotations

import uuid

import pytest
from pytestqt.qtbot import QtBot

from PySide6.QtWidgets import (
    QGraphicsItemGroup,
    QGraphicsLineItem,
    QGraphicsPathItem,
    QGraphicsScene,
)

from protoboard.domain.layout.wire import Fragment, Wire
from protoboard.geometry import Point
from protoboard.gui.layer_state import Z_BACK_WIRES, Z_FRONT_WIRES, LayerState
from protoboard.gui.renderers.wire_renderer import update_wire_groups_v2


@pytest.fixture
def scene(qtbot: QtBot) -> QGraphicsScene:
    return QGraphicsScene()


def _mixed_wire() -> Wire:
    """top → bottom → top: two side transitions, two via points."""
    return Wire(
        id=uuid.uuid4(),
        fragments=[
            Fragment(path=[Point(0, 0), Point(2, 0)], side="top"),
            Fragment(path=[Point(2, 0), Point(4, 0)], side="bottom"),
            Fragment(path=[Point(4, 0), Point(6, 0)], side="top"),
        ],
    )


def _all_top_wire() -> Wire:
    return Wire(id=uuid.uuid4(), path=[Point(0, 0), Point(6, 0)], side="top")


class _RenderState:
    """The group/fingerprint caches scene_composer keeps between refreshes."""

    def __init__(self) -> None:
        self.groups: dict[uuid.UUID, QGraphicsItemGroup] = {}
        self.fingerprints: dict[uuid.UUID, tuple[object, ...]] = {}

    def render(self, scene: QGraphicsScene, wire: Wire) -> QGraphicsItemGroup:
        update_wire_groups_v2(
            scene, [wire], {}, self.groups, self.fingerprints,
            layers=LayerState(),
        )
        return self.groups[wire.id]


def _line_items(grp: QGraphicsItemGroup) -> list[QGraphicsLineItem]:
    return [it for it in grp.childItems() if isinstance(it, QGraphicsLineItem)]


def _z_levels(grp: QGraphicsItemGroup) -> set[int]:
    return {round(it.zValue()) for it in _line_items(grp)}


def _min_pen_alpha_at(grp: QGraphicsItemGroup, z_level: int) -> float:
    alphas = [
        it.pen().color().alphaF()
        for it in _line_items(grp)
        if round(it.zValue()) == z_level
    ]
    assert alphas, f"no line items at z-level {z_level}"
    return min(alphas)


def _via_markers(grp: QGraphicsItemGroup) -> list[QGraphicsPathItem]:
    return [it for it in grp.childItems() if isinstance(it, QGraphicsPathItem)]


def test_mixed_side_wire_renders_on_both_z_levels(scene: QGraphicsScene) -> None:
    grp = _RenderState().render(scene, _mixed_wire())
    zs = _z_levels(grp)
    assert round(Z_FRONT_WIRES) in zs
    assert round(Z_BACK_WIRES) in zs


def test_mixed_side_wire_dims_the_back_fragment(scene: QGraphicsScene) -> None:
    grp = _RenderState().render(scene, _mixed_wire())
    top_alpha = _min_pen_alpha_at(grp, round(Z_FRONT_WIRES))
    bottom_alpha = _min_pen_alpha_at(grp, round(Z_BACK_WIRES))
    assert bottom_alpha < top_alpha


def test_mixed_side_wire_has_via_marker_per_transition(
    scene: QGraphicsScene,
) -> None:
    grp = _RenderState().render(scene, _mixed_wire())
    assert len(_via_markers(grp)) == 2


def test_all_top_wire_single_level_no_via_markers(scene: QGraphicsScene) -> None:
    grp = _RenderState().render(scene, _all_top_wire())
    assert _z_levels(grp) == {round(Z_FRONT_WIRES)}
    assert _via_markers(grp) == []


#: Minimum front-vs-back alpha gap for a wire — the visibility bar of the
#: per-fragment render work: a bottom fragment must read as clearly
#: recessed, not a 0.01 alpha whisper (spec §3, cue A).
_MIN_SIDE_ALPHA_GAP = 0.3


def test_wire_back_alpha_gap_is_perceptible_by_default() -> None:
    from protoboard.controller.theme import DEFAULT_THEME
    from protoboard.gui.layer_state import layer_state_for_tool

    layers = layer_state_for_tool(object(), "top", theme=DEFAULT_THEME)
    gap = layers.alpha_for_wire("top") - layers.alpha_for_wire("bottom")
    assert gap >= _MIN_SIDE_ALPHA_GAP


def test_wire_back_alpha_gap_survives_ghost_top_full_bottom() -> None:
    """The affected project's config: wires_top ghost vs wires_bottom full.

    Historically ghost≈0.89 collided with back_alpha≈0.90 — a 0.01 delta.
    The wire-specific back alpha must keep the sides distinct even here.
    """
    from protoboard.controller.theme import DEFAULT_THEME
    from protoboard.gui.layer_state import (
        LayerOverrides,
        LayerVisibility,
        layer_state_for_tool,
    )

    layers = layer_state_for_tool(
        object(), "top", theme=DEFAULT_THEME,
        overrides=LayerOverrides(
            wires_top=LayerVisibility.GHOST, wires_bottom=LayerVisibility.FULL,
        ),
    )
    gap = layers.alpha_for_wire("top") - layers.alpha_for_wire("bottom")
    assert gap >= _MIN_SIDE_ALPHA_GAP


def test_via_marker_is_prominent(scene: QGraphicsScene) -> None:
    """Cue C: the side-transition marker must be visible at a glance —
    a big-enough disc with a contrasting ring (non-color-only)."""
    grp = _RenderState().render(scene, _mixed_wire())
    marker = _via_markers(grp)[0]
    assert marker.boundingRect().width() >= 0.5
    assert marker.pen().color().rgb() != marker.brush().color().rgb()


def test_routing_target_net_renders_in_neon(scene: QGraphicsScene) -> None:
    """v1 parity: wires of the actively-routed net take routing_target_color."""
    from PySide6.QtGui import QColor

    from protoboard.controller.theme import DEFAULT_THEME

    wire = _all_top_wire()
    net_id = uuid.uuid4()
    groups: dict[uuid.UUID, QGraphicsItemGroup] = {}
    fingerprints: dict[uuid.UUID, tuple[object, ...]] = {}
    update_wire_groups_v2(
        scene, [wire], {wire.id: net_id}, groups, fingerprints,
        layers=LayerState(), routing_target_net_id=net_id,
    )
    neon = QColor(DEFAULT_THEME.routing_target_color).rgb()
    pen_colors = {it.pen().color().rgb() for it in _line_items(groups[wire.id])}
    assert neon in pen_colors


def test_fragment_flip_rebuilds_the_group(scene: QGraphicsScene) -> None:
    """Flipping one fragment's side must invalidate the render cache.

    This is exactly the bug the v1 fingerprint had: it keyed on the scalar
    ``wire.side``, so a mid-wire fragment flip changed nothing and the stale
    all-top group survived on screen.
    """
    wire = Wire(
        id=uuid.uuid4(),
        fragments=[
            Fragment(path=[Point(0, 0), Point(2, 0)], side="top"),
            Fragment(path=[Point(2, 0), Point(4, 0)], side="top"),
            Fragment(path=[Point(4, 0), Point(6, 0)], side="top"),
        ],
    )
    state = _RenderState()
    grp_before = state.render(scene, wire)
    assert _z_levels(grp_before) == {round(Z_FRONT_WIRES)}
    assert _via_markers(grp_before) == []

    wire.fragments = [
        Fragment(path=list(f.path), side=("bottom" if i == 1 else f.side))
        for i, f in enumerate(wire.fragments)
    ]
    grp_after = state.render(scene, wire)

    assert grp_after is not grp_before
    assert round(Z_BACK_WIRES) in _z_levels(grp_after)
    assert len(_via_markers(grp_after)) == 2
