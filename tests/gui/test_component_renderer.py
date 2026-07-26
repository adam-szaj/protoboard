# SPDX-License-Identifier: MIT
"""Tests for component_renderer.py — fingerprinting and group update logic."""

from __future__ import annotations

import uuid

import pytest
from pytestqt.qtbot import QtBot

from PySide6.QtWidgets import QGraphicsItemGroup, QGraphicsScene

from protoboard.domain.components.definition import ComponentDefinition, ComponentClass
from protoboard.domain.components.instance import ComponentInstance
from protoboard.domain.components.footprints import SIL
from protoboard.domain.layout.layout import Layout
from protoboard.domain.layout.placement import ComponentPlacement
from protoboard.domain.schematic.net import Net, PinRef
from protoboard.domain.schematic.schematic import Schematic
from protoboard.geometry import Point, Vector
from protoboard.types import Side
from protoboard.gui.layer_state import (
    LayerOverrides,
    LayerState,
    LayerVisibility,
)
from protoboard.gui.renderers.component_renderer import (
    comp_fingerprint,
    update_component_groups,
    _group_alpha_map,
    _make_overlay_label,
    _get_refdes_font,
)
from protoboard.controller.theme import DEFAULT_THEME


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

def _make_placement(
    x: float = 0.0,
    y: float = 0.0,
    rotation: int = 0,
    side: Side = "top",
) -> ComponentPlacement:
    return ComponentPlacement(
        component_id=uuid.uuid4(),
        position=Point(x, y),
        rotation=rotation,
        side=side,
    )


def _make_resistor_defn() -> ComponentDefinition:
    return ComponentDefinition.from_footprint(
        name="R",
        component_class=ComponentClass.RESISTOR,
        footprint=SIL(n=2),
        pin_names={},
    )


def _make_instance(defn: ComponentDefinition, refdes: str = "R1") -> ComponentInstance:
    return ComponentInstance(
        id=uuid.uuid4(), definition_ref=defn.name, refdes=refdes, value="10k"
    ).bind(defn, {})


def _layers_key(layers: LayerState) -> tuple[object, ...]:
    ov = layers.overrides
    return (
        layers.camera_side,
        layers.view_transform,
        layers.ghost_components,
        ov.components_top.value,
        ov.components_bottom.value,
        ov.labels.value,
        ov.pin_names.value,
    )


# ---------------------------------------------------------------------------
# _make_overlay_label — alpha semantics
# ---------------------------------------------------------------------------

class TestMakeOverlayLabelAlpha:
    """Alpha applied by caller to bg_color dims only the background pill;
    text_color is rendered as-is so dimmed labels stay legible."""

    def test_text_color_alpha_passed_through(self, qtbot: QtBot) -> None:
        from PySide6.QtGui import QColor
        from PySide6.QtWidgets import QGraphicsRectItem, QGraphicsTextItem

        text_color = QColor(50, 60, 70, 255)  # opaque
        bg_color = QColor(255, 255, 255, 50)  # ~20% bg
        font = _get_refdes_font(DEFAULT_THEME.overlay_font_size)
        items = _make_overlay_label(
            "REF1", font, text_color, bg_color, 0.0, 0.0, 5.0,
        )
        # Item order: [bg_rect, text_item]
        assert len(items) == 2
        bg_item, text_item = items
        assert isinstance(text_item, QGraphicsTextItem)
        assert isinstance(bg_item, QGraphicsRectItem)
        # Text alpha unchanged regardless of bg_color alpha.
        assert text_item.defaultTextColor().alpha() == 255
        # Background pill carries the caller-supplied alpha.
        assert bg_item.brush().color().alpha() == 50


# ---------------------------------------------------------------------------
# comp_fingerprint
# ---------------------------------------------------------------------------

class TestCompFingerprint:
    def test_same_inputs_produce_same_fingerprint(self) -> None:
        pl = _make_placement(1.0, 2.0)
        lk = _layers_key(LayerState())
        fp1 = comp_fingerprint(pl, "R", {}, None, lk)
        fp2 = comp_fingerprint(pl, "R", {}, None, lk)
        assert fp1 == fp2

    def test_different_position_gives_different_fingerprint(self) -> None:
        pl_a = _make_placement(1.0, 2.0)
        pl_b = _make_placement(3.0, 4.0)
        lk = _layers_key(LayerState())
        fp_a = comp_fingerprint(pl_a, "R", {}, None, lk)
        fp_b = comp_fingerprint(pl_b, "R", {}, None, lk)
        assert fp_a != fp_b

    def test_different_rotation_gives_different_fingerprint(self) -> None:
        pl_a = _make_placement(0, 0, rotation=0)
        pl_b = _make_placement(0, 0, rotation=90)
        lk = _layers_key(LayerState())
        fp_a = comp_fingerprint(pl_a, "R", {}, None, lk)
        fp_b = comp_fingerprint(pl_b, "R", {}, None, lk)
        assert fp_a != fp_b

    def test_different_definition_ref_gives_different_fingerprint(self) -> None:
        pl = _make_placement()
        lk = _layers_key(LayerState())
        fp_r = comp_fingerprint(pl, "R", {}, None, lk)
        fp_c = comp_fingerprint(pl, "C", {}, None, lk)
        assert fp_r != fp_c

    def test_different_net_assignment_gives_different_fingerprint(self) -> None:
        pl = _make_placement()
        lk = _layers_key(LayerState())
        net_id = uuid.uuid4()
        fp_no_net = comp_fingerprint(pl, "R", {}, None, lk)
        fp_with_net = comp_fingerprint(pl, "R", {1: net_id}, None, lk)
        assert fp_no_net != fp_with_net

    def test_different_side_gives_different_fingerprint(self) -> None:
        pl_top = _make_placement(side="top")
        pl_bot = _make_placement(side="bottom")
        lk = _layers_key(LayerState())
        fp_t = comp_fingerprint(pl_top, "R", {}, None, lk)
        fp_b = comp_fingerprint(pl_bot, "R", {}, None, lk)
        assert fp_t != fp_b

    def test_routed_nets_affects_fingerprint(self) -> None:
        pl = _make_placement()
        lk = _layers_key(LayerState())
        net_id = uuid.uuid4()
        fp_none = comp_fingerprint(pl, "R", {}, None, lk)
        fp_routed = comp_fingerprint(pl, "R", {}, frozenset({net_id}), lk)
        assert fp_none != fp_routed

    def test_layers_key_affects_fingerprint(self) -> None:
        pl = _make_placement()
        ls1 = LayerState()
        ls2 = LayerState()
        ls2.camera_side = "bottom"
        fp1 = comp_fingerprint(pl, "R", {}, None, _layers_key(ls1))
        fp2 = comp_fingerprint(pl, "R", {}, None, _layers_key(ls2))
        assert fp1 != fp2

    def test_pin_offsets_affect_fingerprint(self) -> None:
        pl_no_off = _make_placement()
        pl_with_off = _make_placement()
        pl_with_off.pin_offsets = {1: Vector(0.5, 0.0)}
        lk = _layers_key(LayerState())
        fp_a = comp_fingerprint(pl_no_off, "R", {}, None, lk)
        fp_b = comp_fingerprint(pl_with_off, "R", {}, None, lk)
        assert fp_a != fp_b

    def test_pin_net_order_does_not_matter(self) -> None:
        pl = _make_placement()
        lk = _layers_key(LayerState())
        n1, n2 = uuid.uuid4(), uuid.uuid4()
        fp_ab = comp_fingerprint(pl, "R", {1: n1, 2: n2}, None, lk)
        fp_ba = comp_fingerprint(pl, "R", {2: n2, 1: n1}, None, lk)
        assert fp_ab == fp_ba  # sorted(items()) normalises order

    def test_different_refdes_gives_different_fingerprint(self) -> None:
        # The overlay label embeds inst.refdes; editing it must rebuild
        # the group so the new text paints.
        pl = _make_placement()
        lk = _layers_key(LayerState())
        fp_r1 = comp_fingerprint(pl, "R", {}, None, lk, refdes="R1")
        fp_r2 = comp_fingerprint(pl, "R", {}, None, lk, refdes="R2")
        assert fp_r1 != fp_r2

    def test_different_value_gives_different_fingerprint(self) -> None:
        # The overlay label embeds inst.value; editing it must rebuild.
        pl = _make_placement()
        lk = _layers_key(LayerState())
        fp_a = comp_fingerprint(pl, "R", {}, None, lk, value="?")
        fp_b = comp_fingerprint(pl, "R", {}, None, lk, value="4.7k")
        assert fp_a != fp_b


class TestComposeOverlayLabel:
    """Overlay label composition contract:

    * concrete parts → ``"<refdes> <part-number>"``  (Q1 2N2222)
    * generic parts with a value → ``"<refdes> <value>"``  (R23 4.7k)
    * generic parts awaiting a value → ``"<refdes> ?"``  (R23 ?)
    * value empty → just refdes  (R23)
    """

    def test_value_present_concatenated_with_space(self) -> None:
        from protoboard.gui.renderers.component_renderer import (
            _compose_overlay_label,
        )
        assert _compose_overlay_label("R23", "4.7k") == "R23 4.7k"

    def test_value_question_mark_prompts_user(self) -> None:
        from protoboard.gui.renderers.component_renderer import (
            _compose_overlay_label,
        )
        assert _compose_overlay_label("R23", "?") == "R23 ?"

    def test_value_part_number_for_concrete_parts(self) -> None:
        from protoboard.gui.renderers.component_renderer import (
            _compose_overlay_label,
        )
        assert _compose_overlay_label("Q1", "2N2222") == "Q1 2N2222"
        assert _compose_overlay_label("D5", "1N4148") == "D5 1N4148"
        assert _compose_overlay_label("U2", "NE555") == "U2 NE555"

    def test_empty_value_yields_refdes_only(self) -> None:
        from protoboard.gui.renderers.component_renderer import (
            _compose_overlay_label,
        )
        assert _compose_overlay_label("R23", "") == "R23"


# ---------------------------------------------------------------------------
# update_component_groups — incremental update logic
# ---------------------------------------------------------------------------

class TestUpdateComponentGroups:
    def _make_setup(self) -> tuple[
        QGraphicsScene,
        ComponentPlacement,
        ComponentInstance,
        ComponentDefinition,
        Schematic,
    ]:
        defn = _make_resistor_defn()
        inst = _make_instance(defn)
        pl = ComponentPlacement(
            component_id=inst.id,
            position=Point(1, 1),
            rotation=0,
            side="top",
        )
        schem = Schematic(nets={})
        return QGraphicsScene(), pl, inst, defn, schem

    def test_creates_group_for_new_component(self, qtbot: QtBot) -> None:
        scene, pl, inst, defn, schem = self._make_setup()
        existing_groups: dict[uuid.UUID, QGraphicsItemGroup] = {}
        existing_fps: dict[uuid.UUID, tuple[object, ...]] = {}
        layout = Layout()
        layout.ensure_root_group()
        layout.placements[inst.id] = pl

        update_component_groups(
            scene=scene,
            placements={inst.id: pl},
            instances={inst.id: inst},
            components={defn.name: defn},
            existing_groups=existing_groups,
            existing_fingerprints=existing_fps,
            schematic=schem,
            routed_nets=None,
            theme=DEFAULT_THEME,
            layers=LayerState(),
            active_group_id=None,
            layout=layout,
            highlighted_net_id=None,
        )

        assert inst.id in existing_groups

    def test_removes_group_for_deleted_component(self, qtbot: QtBot) -> None:
        scene, pl, inst, defn, schem = self._make_setup()
        existing_groups: dict[uuid.UUID, QGraphicsItemGroup] = {}
        existing_fps: dict[uuid.UUID, tuple[object, ...]] = {}
        layout = Layout()
        layout.ensure_root_group()
        layout.placements[inst.id] = pl

        # Create initial group.
        update_component_groups(
            scene=scene,
            placements={inst.id: pl},
            instances={inst.id: inst},
            components={defn.name: defn},
            existing_groups=existing_groups,
            existing_fingerprints=existing_fps,
            schematic=schem,
            routed_nets=None,
            theme=DEFAULT_THEME,
            layers=LayerState(),
            active_group_id=None,
            layout=layout,
            highlighted_net_id=None,
        )
        assert inst.id in existing_groups

        # Remove component from placements.
        update_component_groups(
            scene=scene,
            placements={},
            instances={inst.id: inst},
            components={defn.name: defn},
            existing_groups=existing_groups,
            existing_fingerprints=existing_fps,
            schematic=schem,
            routed_nets=None,
            theme=DEFAULT_THEME,
            layers=LayerState(),
            active_group_id=None,
            layout=layout,
            highlighted_net_id=None,
        )
        assert inst.id not in existing_groups

    def test_unchanged_fingerprint_does_not_rebuild_group(self, qtbot: QtBot) -> None:
        scene, pl, inst, defn, schem = self._make_setup()
        existing_groups: dict[uuid.UUID, QGraphicsItemGroup] = {}
        existing_fps: dict[uuid.UUID, tuple[object, ...]] = {}
        layout = Layout()
        layout.ensure_root_group()
        layout.placements[inst.id] = pl

        def _call() -> None:
            update_component_groups(
                scene=scene,
                placements={inst.id: pl},
                instances={inst.id: inst},
                components={defn.name: defn},
                existing_groups=existing_groups,
                existing_fingerprints=existing_fps,
                schematic=schem,
                routed_nets=None,
                theme=DEFAULT_THEME,
                layers=LayerState(),
                active_group_id=None,
                layout=layout,
                highlighted_net_id=None,
            )

        _call()
        group_first = existing_groups.get(inst.id)
        _call()
        group_second = existing_groups.get(inst.id)
        # Same object — no rebuild occurred.
        assert group_first is group_second

    def test_changed_position_rebuilds_group(self, qtbot: QtBot) -> None:
        scene, pl, inst, defn, schem = self._make_setup()
        existing_groups: dict[uuid.UUID, QGraphicsItemGroup] = {}
        existing_fps: dict[uuid.UUID, tuple[object, ...]] = {}
        layout = Layout()
        layout.ensure_root_group()
        layout.placements[inst.id] = pl

        def _call(x: float) -> None:
            moved_pl = pl.copy()
            moved_pl.position = Point(x, 0)
            layout.placements[inst.id] = moved_pl
            update_component_groups(
                scene=scene,
                placements={inst.id: moved_pl},
                instances={inst.id: inst},
                components={defn.name: defn},
                existing_groups=existing_groups,
                existing_fingerprints=existing_fps,
                schematic=schem,
                routed_nets=None,
                theme=DEFAULT_THEME,
                layers=LayerState(),
                active_group_id=None,
                layout=layout,
                highlighted_net_id=None,
            )

        _call(0.0)
        group_at_0 = existing_groups.get(inst.id)
        _call(5.0)
        group_at_5 = existing_groups.get(inst.id)
        assert group_at_0 is not group_at_5


# ---------------------------------------------------------------------------
# _group_alpha_map
# ---------------------------------------------------------------------------

class TestGroupAlphaMap:
    def test_returns_empty_when_active_group_not_found(self) -> None:
        layout = Layout()
        result = _group_alpha_map(layout, uuid.uuid4())
        assert result == {}

    def test_direct_members_get_full_alpha(self) -> None:
        from protoboard.domain.layout.layout import ROOT_GROUP_ID
        from protoboard.domain.commands.group_cmds import CreateGroupCommand
        layout = Layout()
        layout.ensure_root_group()
        comp_id = uuid.uuid4()
        layout.placements[comp_id] = _make_placement()

        # Create a group and add comp_id to it.
        grp_id = uuid.uuid4()
        from protoboard.domain.layout.group import ComponentGroup
        group = ComponentGroup(
            id=grp_id,
            name="G1",
            component_ids=frozenset({comp_id}),
            child_group_ids=frozenset(),
            exported_pins=frozenset(),
        )
        layout.groups[grp_id] = group

        alpha_map = _group_alpha_map(layout, grp_id)
        assert alpha_map.get(comp_id) == pytest.approx(1.0)

    def test_outside_members_get_inactive_alpha(self) -> None:
        from protoboard.domain.layout.layout import ROOT_GROUP_ID
        from protoboard.domain.layout.group import ComponentGroup
        layout = Layout()
        layout.ensure_root_group()
        comp_in = uuid.uuid4()
        comp_out = uuid.uuid4()
        layout.placements[comp_in] = _make_placement()
        layout.placements[comp_out] = _make_placement()

        grp_id = uuid.uuid4()
        group = ComponentGroup(
            id=grp_id,
            name="G1",
            component_ids=frozenset({comp_in}),
            child_group_ids=frozenset(),
            exported_pins=frozenset(),
        )
        layout.groups[grp_id] = group

        alpha_map = _group_alpha_map(layout, grp_id)
        # comp_in → active (1.0), comp_out → inactive (0.2).
        assert alpha_map.get(comp_in) == pytest.approx(1.0)
        assert alpha_map.get(comp_out) == pytest.approx(0.2)


class TestGhostHonoursPackageVariant:
    """The place-tool ghost must preview the picker's chosen body, not the
    definition default — a C placed as SMD-0805 draws the flat chip, not the
    default candy ceramic disc (regression: ghost ignored package_variant)."""

    def _theme_and_defn(self):
        import dataclasses
        from pathlib import Path
        from protoboard.controller.theme import DEFAULT_THEME
        from protoboard.services.library.loader import load_library
        from protoboard.services.library.package_family_loader import (
            load_full_package_catalogue,
        )
        fam = load_full_package_catalogue()
        lib = load_library([Path("library")], fam)
        theme = dataclasses.replace(DEFAULT_THEME, package_families=fam)
        return theme, lib.get("C")

    def test_variant_changes_ghost_pin_spacing(self, qtbot: QtBot) -> None:
        from protoboard.gui.renderers.component_renderer import make_ghost_item

        theme, defn = self._theme_and_defn()
        g_default = make_ghost_item(defn, theme=theme)
        g_smd = make_ghost_item(defn, theme=theme,
                                package_variant="SMD-0805/typ")
        qtbot.addWidget  # noqa: B018 - QGraphicsItemGroup isn't a widget
        # The two ghosts are built without error and differ — the SMD body's
        # pin spacing/shape is not the default candy disc's.
        assert g_default.childItems()
        assert g_smd.childItems()
        assert g_default.boundingRect() != g_smd.boundingRect()

    def test_unknown_variant_falls_back_to_default(self, qtbot: QtBot) -> None:
        from protoboard.gui.renderers.component_renderer import make_ghost_item

        theme, defn = self._theme_and_defn()
        # A stale/unknown address resolves to nothing → default body, no crash.
        g = make_ghost_item(defn, theme=theme, package_variant="No-Such/typ")
        assert g.childItems()

    def test_rotation_turns_body_with_the_pins(self, qtbot: QtBot) -> None:
        # The body must rotate WITH the pins: make_ghost_item passes the
        # placement (rotation) to body_shape_for, so an SMD chip body at 0° vs
        # 90° has a transposed bbox. Regression: without the placement the body
        # drew in an unrotated frame while the pins turned. (Tested on the body
        # shape itself — the whole ghost's bbox is dominated by the fixed-
        # orientation name label, which never rotates.)
        import uuid as _uuid

        from PySide6.QtGui import QColor
        from PySide6.QtWidgets import QGraphicsItemGroup

        from protoboard.domain.components.instance import ComponentInstance
        from protoboard.domain.components.package import (
            resolve_component_package,
        )
        from protoboard.domain.components.package.adapter import PackageFootprint
        from protoboard.domain.layout.placement import (
            ComponentPlacement,
            pin_world_position,
        )
        from protoboard.geometry import Point
        from protoboard.gui.renderers.body_shapes import body_shape_for

        theme, defn = self._theme_and_defn()
        inst = ComponentInstance.new(
            definition_ref="C", refdes="?", value="",
            package_variant="SMD-0805/typ")
        pkg = resolve_component_package(defn, inst, theme.package_families)
        assert pkg is not None
        fp = PackageFootprint(pkg)

        def _body_bbox(rotation: int):
            pl = ComponentPlacement(
                component_id=_uuid.UUID(int=0), position=Point(0.0, 0.0),
                rotation=rotation, side="top", pin_offsets={})
            positions = [pin_world_position(fp, pl, n + 1)
                         for n in range(fp.pin_count())]
            shape = body_shape_for(defn, pl, inst,
                                   package_families=theme.package_families)
            g = QGraphicsItemGroup()
            body_items, label_items = shape.draw(
                positions, QColor("red"), QColor("black"), 1.0, 0.0, theme,
            )
            for it in body_items + label_items:
                g.addToGroup(it)
            return g.boundingRect()

        r0, r90 = _body_bbox(0), _body_bbox(90)
        assert r0.width() == pytest.approx(r90.height(), abs=0.05)
        assert r0.height() == pytest.approx(r90.width(), abs=0.05)


# ---------------------------------------------------------------------------
# Pin-names layer independence from the labels layer (regression)
# ---------------------------------------------------------------------------

class TestPinNamesLayerIndependence:
    """Pin-name labels are governed solely by the ``pin_names`` layer.

    Regression: pin-name rendering used to live inside the ``alpha_label > 0``
    block, so hiding the "Labels" layer also hid pin names even though the two
    layers are meant to toggle independently (see
    ``LayerState.alpha_for_pin_names``).
    """

    def _scene_texts(self, layers: LayerState) -> set[str]:
        from PySide6.QtWidgets import QGraphicsTextItem

        defn = ComponentDefinition.from_footprint(
            name="U",
            component_class=ComponentClass.IC,
            footprint=SIL(n=2),
            pin_names={1: "VCC", 2: "GND"},
        )
        inst = ComponentInstance(
            id=uuid.uuid4(), definition_ref=defn.name, refdes="U1", value="",
        ).bind(defn, {})
        pl = ComponentPlacement(
            component_id=inst.id, position=Point(1, 1), rotation=0, side="top",
        )
        layout = Layout()
        layout.ensure_root_group()
        layout.placements[inst.id] = pl
        scene = QGraphicsScene()
        update_component_groups(
            scene=scene,
            placements={inst.id: pl},
            instances={inst.id: inst},
            components={defn.name: defn},
            existing_groups={},
            existing_fingerprints={},
            schematic=Schematic(nets={}),
            routed_nets=None,
            theme=DEFAULT_THEME,
            layers=layers,
            active_group_id=None,
            layout=layout,
            highlighted_net_id=None,
        )
        return {
            it.toPlainText()
            for it in scene.items()
            if isinstance(it, QGraphicsTextItem)
        }

    def _layers(self, **overrides: LayerVisibility) -> LayerState:
        return LayerState(overrides=LayerOverrides(**overrides))

    def test_pin_names_visible_when_labels_hidden(self, qtbot: QtBot) -> None:
        # Labels off, pin names on → pin names still render; refdes does not.
        texts = self._scene_texts(
            self._layers(labels=LayerVisibility.HIDDEN)
        )
        assert "VCC" in texts
        assert "GND" in texts
        assert "U1" not in texts

    def test_pin_names_hidden_when_labels_visible(self, qtbot: QtBot) -> None:
        # The converse: pin-names layer off still hides them even with labels on.
        texts = self._scene_texts(
            self._layers(pin_names=LayerVisibility.HIDDEN)
        )
        assert "VCC" not in texts
        assert "GND" not in texts
        assert "U1" in texts

    def test_both_layers_visible(self, qtbot: QtBot) -> None:
        texts = self._scene_texts(self._layers())
        assert {"VCC", "GND", "U1"} <= texts
