# SPDX-License-Identifier: MIT
"""Smoke tests for the pin-pads renderer (gui/renderers/pin_pad_renderer.py)."""

from __future__ import annotations

import uuid

import pytest
from PySide6.QtCore import Qt
from PySide6.QtGui import QColor
from PySide6.QtWidgets import QGraphicsEllipseItem, QGraphicsPathItem, QGraphicsScene
from pytestqt.qtbot import QtBot

from protoboard.controller.view_options.layer_state import LayerState
from protoboard.controller.view_options.layers import LayerOverrides, LayerVisibility
from protoboard.controller.view_options.theme import DEFAULT_THEME
from protoboard.domain.board.board import Board
from protoboard.domain.board.boxes import BoardGrid, PadAtom, SizePolicy
from protoboard.domain.board.pad_state import PadIndex
from protoboard.domain.board.pads import PadLayers
from protoboard.domain.components.definition import (
    ComponentDefinition,
    MetaPin,
    PackageOption,
)
from protoboard.domain.components.instance import ComponentInstance
from protoboard.domain.components.package.generated_families import sil_family
from protoboard.domain.layout.group import ComponentGroup
from protoboard.domain.layout.layout import ROOT_GROUP_ID, Layout
from protoboard.domain.layout.pin_pad_classification import (
    ModifierShape,
    PinPadClass,
    PinPadInfo,
    classify_pin_pads,
)
from protoboard.domain.layout.placement import ComponentPlacement
from protoboard.domain.schematic.net import Net
from protoboard.domain.schematic.schematic import PinRef, Schematic
from protoboard.geometry import Point
from protoboard.gui.renderers.pin_pad_renderer import (
    render_pin_modifiers,
    render_pin_pads,
)


@pytest.fixture
def scene(qtbot: QtBot) -> QGraphicsScene:
    s = QGraphicsScene()
    return s


def _resistor_setup(
    *, exported_pin_numbers: tuple[int, ...] = ()
) -> tuple[PadIndex, Layout, uuid.UUID]:
    board = Board(
        root=BoardGrid(
            width_policy=SizePolicy.HUG,
            height_policy=SizePolicy.HUG,
            cell=PadAtom(
                width_policy=SizePolicy.HUG,
                height_policy=SizePolicy.HUG,
                layers=PadLayers(top=True, bottom=True, plated_through=True),
            ),
            rows=2,
            cols=3,
            row_gap=0,
            col_gap=0,
        )
    )
    family = sil_family(2)
    assert family is not None
    defn = ComponentDefinition(
        name="R",
        meta_pins=(MetaPin(("A",)), MetaPin(("B",))),
        package_options=(
            PackageOption(
                name="SIL-2",
                ref="sil-2/std",
                pin_map={1: (1,), 2: (2,)},
            ),
        ),
    )
    inst = ComponentInstance.new(definition_ref="R", refdes="R1", value="").bind(
        defn, {family.family_id: family}
    )
    pl = ComponentPlacement(
        component_id=inst.id,
        position=Point(0.5, 0.5),
        rotation=90,
        side="top",
    )
    lay = Layout()
    lay.ensure_root_group()
    lay.placements[inst.id] = pl
    if exported_pin_numbers:
        gid = uuid.uuid4()
        lay.groups[gid] = ComponentGroup(
            id=gid,
            name="G",
            component_ids=frozenset({inst.id}),
            exported_pins=frozenset(
                PinRef(component_id=inst.id, pin_number=n) for n in exported_pin_numbers
            ),
        )
        root = lay.groups[ROOT_GROUP_ID]
        lay.groups[ROOT_GROUP_ID] = ComponentGroup(
            id=root.id,
            name=root.name,
            component_ids=root.component_ids,
            child_group_ids=root.child_group_ids | {gid},
            exported_pins=root.exported_pins,
        )
    n1 = uuid.uuid4()
    n2 = uuid.uuid4()
    sch = Schematic(
        nets={
            n1: Net(
                id=n1, name="N1", pins={PinRef(component_id=inst.id, pin_number=1)}
            ),
            n2: Net(
                id=n2, name="N2", pins={PinRef(component_id=inst.id, pin_number=2)}
            ),
        }
    )
    idx = PadIndex(board, lay, sch, {"R": defn}, {inst.id: inst})
    return idx, lay, n1


# ---------------------------------------------------------------------------
# Pin-pads
# ---------------------------------------------------------------------------


class TestRenderPinPads:
    def test_pin_pad_yields_disc_plus_carve_for_pin1(
        self, scene: QGraphicsScene
    ) -> None:
        idx, lay, _ = _resistor_setup()
        records = classify_pin_pads(idx, lay)
        items = render_pin_pads(
            scene,
            records,
            theme=DEFAULT_THEME,
            layers=LayerState(),
        )
        # Each PIN_PAD record produces 2 items: filled disc + carved
        # modifier (CIRCLE for the default, square/diamond/star for
        # explicit modifiers).  Plated-through pads classify on both
        # sides, so 2 pins × 2 sides = 4 records × 2 items each = 8.
        assert len(items) == 8
        disc_count = sum(isinstance(it, QGraphicsEllipseItem) for it in items)
        carve_count = sum(isinstance(it, QGraphicsPathItem) for it in items)
        assert disc_count == 4  # 2 sides × 2 pins
        assert carve_count == 4  # carve per pin-pad (CIRCLE counts too)

    def test_layer_hidden_suppresses_output(self, scene: QGraphicsScene) -> None:
        idx, lay, _ = _resistor_setup()
        records = classify_pin_pads(idx, lay)
        ov = LayerOverrides()
        ov.pin_pads = LayerVisibility.HIDDEN
        items = render_pin_pads(
            scene,
            records,
            theme=DEFAULT_THEME,
            layers=LayerState(overrides=ov),
        )
        assert items == []

    def test_free_pin_emits_neutral_ring_and_glyph(self, scene: QGraphicsScene) -> None:
        # FREE (pin present, no net) → neutral outline ring + modifier glyph
        # in pin_free_color, NOT a net-coloured filled disc.
        synthetic = PinPadInfo(
            position=Point(0.5, 0.5),
            side="top",
            net_id=None,
            klass=PinPadClass.FREE,
            modifier=ModifierShape.SQUARE,  # pin1 marker still drawn
        )
        items = render_pin_pads(
            scene,
            [synthetic],
            theme=DEFAULT_THEME,
            layers=LayerState(),
        )
        # Ring (ellipse) + glyph (path), both outline-only, neutral colour.
        rings = [it for it in items if isinstance(it, QGraphicsEllipseItem)]
        glyphs = [it for it in items if isinstance(it, QGraphicsPathItem)]
        assert len(rings) == 1
        assert len(glyphs) == 1
        ring = rings[0]
        free = QColor(DEFAULT_THEME.pin_free_color)
        pen_c = ring.pen().color()
        assert (pen_c.red(), pen_c.green(), pen_c.blue()) == (
            free.red(),
            free.green(),
            free.blue(),
        )
        # No fill — transparent centre so the board pad stays visible.
        assert ring.brush().style() == Qt.BrushStyle.NoBrush

    def test_routing_target_overrides_color(self, scene: QGraphicsScene) -> None:
        idx, lay, n1 = _resistor_setup()
        records = classify_pin_pads(idx, lay)
        items = render_pin_pads(
            scene,
            records,
            theme=DEFAULT_THEME,
            layers=LayerState(),
            routing_target_net_id=n1,
        )
        # Find disc items belonging to net n1 (pin 1's pad — by position).
        # Pin 1 is at (0.5, 0.5).
        target_color = QColor(DEFAULT_THEME.routing_target_color)
        found = False
        for it in items:
            if isinstance(it, QGraphicsEllipseItem):
                rect = it.rect()
                # pad_radius rect → cx = rect.left + r
                cx = rect.x() + rect.width() / 2
                cy = rect.y() + rect.height() / 2
                if abs(cx - 0.5) < 0.01 and abs(cy - 0.5) < 0.01:
                    brush = it.brush()
                    c = brush.color()
                    # Allow alpha multiplied — compare RGB only.
                    if (c.red(), c.green(), c.blue()) == (
                        target_color.red(),
                        target_color.green(),
                        target_color.blue(),
                    ):
                        found = True
                        break
        assert found, "Pin-pad of routing-target net should be repainted in neon"


# ---------------------------------------------------------------------------
# Modifiers (top overlay)
# ---------------------------------------------------------------------------


class TestRenderPinModifiers:
    def test_pin1_emits_square_modifier(self, scene: QGraphicsScene) -> None:
        idx, lay, _ = _resistor_setup()
        records = classify_pin_pads(idx, lay)
        items = render_pin_modifiers(
            scene,
            records,
            theme=DEFAULT_THEME,
            layers=LayerState(),
        )
        # Every PIN_PAD emits a top overlay — including the default CIRCLE
        # (pin markers stay visible even when the body covers the pads).
        # Pin 1 (SQUARE) and pin 2 (CIRCLE), each on BOTH sides
        # (plated-through) → 4 glyphs.
        assert len(items) == 4
        assert all(isinstance(it, QGraphicsPathItem) for it in items)

    def test_circle_modifier_emits_glyph(self, scene: QGraphicsScene) -> None:
        # The default CIRCLE modifier now also gets a top glyph so plain
        # pins are marked consistently with the special ones.
        synthetic = PinPadInfo(
            position=Point(0.5, 0.5),
            side="top",
            net_id=uuid.uuid4(),
            klass=PinPadClass.PIN_PAD,
            modifier=ModifierShape.CIRCLE,
        )
        items = render_pin_modifiers(
            scene,
            [synthetic],
            theme=DEFAULT_THEME,
            layers=LayerState(),
        )
        assert len(items) == 1
        assert isinstance(items[0], QGraphicsPathItem)

    def test_diamond_and_star_shapes(self, scene: QGraphicsScene) -> None:
        # Two synthetic records — one diamond, one star — verify items emit.
        records = [
            PinPadInfo(
                position=Point(0.5, 0.5),
                side="top",
                net_id=uuid.uuid4(),
                klass=PinPadClass.PIN_PAD,
                modifier=ModifierShape.DIAMOND,
            ),
            PinPadInfo(
                position=Point(1.5, 0.5),
                side="top",
                net_id=uuid.uuid4(),
                klass=PinPadClass.PIN_PAD,
                modifier=ModifierShape.STAR,
            ),
        ]
        items = render_pin_modifiers(
            scene,
            records,
            theme=DEFAULT_THEME,
            layers=LayerState(),
        )
        assert len(items) == 2

    def test_free_no_top_modifier(self, scene: QGraphicsScene) -> None:
        # FREE pins draw their glyph in the pin-pads layer, not the top
        # modifier overlay (which is net-coloured) — so no top glyph here.
        records = [
            PinPadInfo(
                position=Point(0.5, 0.5),
                side="top",
                net_id=None,
                klass=PinPadClass.FREE,
                modifier=ModifierShape.SQUARE,
            ),
        ]
        items = render_pin_modifiers(
            scene,
            records,
            theme=DEFAULT_THEME,
            layers=LayerState(),
        )
        assert items == []

    def test_contact_only_no_modifier(self, scene: QGraphicsScene) -> None:
        # CONTACT_ONLY classifications never emit a top modifier glyph.
        records = [
            PinPadInfo(
                position=Point(0.5, 0.5),
                side="top",
                net_id=uuid.uuid4(),
                klass=PinPadClass.CONTACT_ONLY,
                modifier=ModifierShape.CIRCLE,
            ),
        ]
        items = render_pin_modifiers(
            scene,
            records,
            theme=DEFAULT_THEME,
            layers=LayerState(),
        )
        assert items == []
