# SPDX-License-Identifier: MIT
"""Components belonging to the highlighted net must skip ghost dimming.

Under a routing/placement tool, ``LayerState.ghost_components=True`` and
the components layer fades to ~ghost_comp_alpha.  That's the right
default — the user is focused on the tool — but when a specific net is
highlighted (RouteTool drawing or Net Inspector selection), components
participating in that net must remain fully visible so their pads stand
out at full opacity.
"""

from __future__ import annotations

import uuid

from PySide6.QtWidgets import QGraphicsScene

from protoboard.domain.components.definition import ComponentDefinition
from protoboard.domain.components.footprints import SIL
from protoboard.domain.components.instance import ComponentInstance
from protoboard.domain.layout.placement import ComponentPlacement
from protoboard.geometry import Point
from protoboard.gui.layer_state import LayerState
from protoboard.gui.renderers.component_renderer import _render_one
from protoboard.gui.theme import DEFAULT_THEME


def _make_comp() -> tuple[ComponentDefinition, ComponentInstance, ComponentPlacement, uuid.UUID]:
    cid = uuid.uuid4()
    defn = ComponentDefinition(
        name="R", footprint=SIL(n=2), pin_names={1: "A", 2: "B"},
    )
    inst = ComponentInstance(id=cid, definition_ref="R", refdes="R1", value="").bind(defn, {})
    placement = ComponentPlacement(
        component_id=cid, position=Point(0.5, 0.5), rotation=90, side="top",
    )
    return defn, inst, placement, cid


class TestHighlightedComponentBypassesGhost:
    def test_highlighted_renders_when_ghost_zeros_normal(self, qtbot) -> None:
        """With ghost_comp_alpha=0 the un-highlighted path produces alpha=0
        and the renderer drops the component (returns None).  The
        highlighted path bypasses the ghost factor and returns a group."""
        defn, inst, placement, cid = _make_comp()
        layers = LayerState(ghost_components=True)
        layers.ghost_comp_alpha = 0.0  # crushed by ghost when not highlighted
        qtbot.addWidget  # ensure QApplication

        scene_normal = QGraphicsScene()
        grp_normal = _render_one(
            scene_normal, placement, inst, defn, cid, {}, None,
            DEFAULT_THEME, layers, group_alpha=1.0,
            highlighted_net_id=None, is_highlighted_comp=False,
        )
        assert grp_normal is None, (
            "ghost_comp_alpha=0 should crush un-highlighted comp to alpha=0"
        )

        scene_bold = QGraphicsScene()
        grp_bold = _render_one(
            scene_bold, placement, inst, defn, cid, {}, None,
            DEFAULT_THEME, layers, group_alpha=1.0,
            highlighted_net_id=None, is_highlighted_comp=True,
        )
        assert grp_bold is not None, (
            "highlighted comp must skip the ghost factor and render at full alpha"
        )

    def test_group_alpha_still_applies_when_highlighted(self, qtbot) -> None:
        """Bypass affects only the ghost factor — group_alpha=0 still drops."""
        defn, inst, placement, cid = _make_comp()
        layers = LayerState(ghost_components=True)
        qtbot.addWidget
        scene = QGraphicsScene()
        grp = _render_one(
            scene, placement, inst, defn, cid, {}, None,
            DEFAULT_THEME, layers, group_alpha=0.0,
            highlighted_net_id=None, is_highlighted_comp=True,
        )
        assert grp is None
