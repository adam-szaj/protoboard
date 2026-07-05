# SPDX-License-Identifier: MIT
"""Renderer registry — lineage stamping, dispatch, and plugin discovery.

Covers the modular-rendering seam: packages carry a ``lineage`` (shape root →
extends chain → kind), ``renderer_for`` walks it leaf-first against
``BODY_RENDERERS``, and dropping a ``renderers/*.py`` file into a library dir
registers a renderer on import.
"""

from __future__ import annotations

from pathlib import Path
from textwrap import dedent

import pytest

from protoboard.domain.components.package.kind import (
    GeneratorContext,
    build_kind_package,
)
from protoboard.domain.components.package.loader import (
    extends_chain,
    load_packages,
)
from protoboard.geometry import Point
from protoboard.gui.renderers.body_shapes import PackageBody
from protoboard.gui.renderers.packages import (
    BODY_RENDERERS,
    CLASS_RENDERERS,
    CylinderRenderer,
    DiscRenderer,
    PrismRenderer,
    load_renderer_plugins,
    register_renderer,
    renderer_for,
)

# ---------------------------------------------------------------------------
# Fixtures
# ---------------------------------------------------------------------------

_RAW_DEFS = [
    {
        "name": "cylinder_base",
        "abstract": True,
        "shape": "cylinder",
        "leads": {
            "direction": [[0, 0, -1], [0, 0, 1]],
            "body_offset": 0,
            "board_offset": [[4, 0]],
        },
    },
    {
        "name": "axial_test",
        "abstract": True,
        "extends": "cylinder_base",
    },
    {
        "name": "DO-TEST",
        "extends": "axial_test",
        "dims": {"diameter": 2.0, "length": 4.0},
    },
]


@pytest.fixture
def do_test_package():
    return load_packages([dict(d) for d in _RAW_DEFS])["DO-TEST"]


# ---------------------------------------------------------------------------
# Lineage stamping
# ---------------------------------------------------------------------------

class TestLineage:
    def test_extends_chain_is_root_first(self) -> None:
        registry = {str(d["name"]): dict(d) for d in _RAW_DEFS}
        assert extends_chain("DO-TEST", registry) == (
            "cylinder_base", "axial_test", "DO-TEST",
        )

    def test_loaded_package_carries_lineage(self, do_test_package) -> None:
        # The shape root is prepended when the chain root is a differently
        # named base.
        assert do_test_package.lineage == (
            "cylinder", "cylinder_base", "axial_test", "DO-TEST",
        )

    def test_kind_package_lineage_is_shape_plus_kind(self) -> None:
        ctx = build_kind_package("dip_ic", GeneratorContext(pin_count=8))
        assert ctx.package is not None
        assert ctx.package.lineage == ("prism", "dip_ic")


# ---------------------------------------------------------------------------
# Dispatch
# ---------------------------------------------------------------------------

class TestRendererDispatch:
    def test_shape_root_fallback(self, do_test_package) -> None:
        assert renderer_for(do_test_package) is CylinderRenderer

    def test_kind_dispatches_its_renderer(self) -> None:
        from protoboard.gui.renderers.packages import SilHeaderRenderer
        ctx = build_kind_package("sil", GeneratorContext(pin_count=3))
        assert ctx.package is not None
        assert renderer_for(ctx.package) is SilHeaderRenderer

    def test_unregistered_kind_falls_back_to_prism(self) -> None:
        import dataclasses
        ctx = build_kind_package("sil", GeneratorContext(pin_count=3))
        assert ctx.package is not None
        pkg = dataclasses.replace(
            ctx.package, lineage=("prism", "no_such_kind"))
        assert renderer_for(pkg) is PrismRenderer

    def test_specific_renderer_wins_over_root(self, do_test_package) -> None:
        class DoTestRenderer(CylinderRenderer):
            renders = "DO-TEST"

        try:
            assert renderer_for(do_test_package) is DoTestRenderer
        finally:
            del BODY_RENDERERS["DO-TEST"]

    def test_mid_lineage_renderer_wins_when_leaf_unknown(
        self, do_test_package,
    ) -> None:
        class AxialTestRenderer(CylinderRenderer):
            renders = "axial_test"

        try:
            assert renderer_for(do_test_package) is AxialTestRenderer
        finally:
            del BODY_RENDERERS["axial_test"]

    def test_subclass_without_renders_does_not_steal_slot(self) -> None:
        before = dict(BODY_RENDERERS)

        class _Anon(PrismRenderer):  # no ``renders`` declared
            pass

        assert BODY_RENDERERS == before

    def test_package_body_constructs_dispatched_subclass(
        self, do_test_package,
    ) -> None:
        shape = PackageBody(do_test_package)
        assert type(shape) is CylinderRenderer
        assert isinstance(shape, PackageBody)

    def test_builtin_roots_registered(self) -> None:
        assert BODY_RENDERERS["cylinder"] is CylinderRenderer
        assert BODY_RENDERERS["prism"] is PrismRenderer
        assert BODY_RENDERERS["disc"] is DiscRenderer


# ---------------------------------------------------------------------------
# @register_renderer — typed-axis registration + the component-class axis
# ---------------------------------------------------------------------------

class TestRegisterRendererDecorator:

    def test_lineage_axes_share_body_renderers(self) -> None:
        # kind / family / package_name / shape_name all resolve through the
        # package lineage, so they land in the same registry as ``renders``.
        @register_renderer(kind="_t_deco_kind", family="_T-DECO-FAM")
        class _DecoRenderer(PrismRenderer):
            pass

        try:
            assert BODY_RENDERERS["_t_deco_kind"] is _DecoRenderer
            assert BODY_RENDERERS["_T-DECO-FAM"] is _DecoRenderer
        finally:
            del BODY_RENDERERS["_t_deco_kind"]
            del BODY_RENDERERS["_T-DECO-FAM"]

    def test_component_class_axis_wins_over_lineage_leaf(self) -> None:
        from protoboard.gui.renderers.packages import SilHeaderRenderer

        @register_renderer(component_class="_t_led_bar")
        class _LedBarRenderer(PrismRenderer):
            pass

        ctx = build_kind_package("sil", GeneratorContext(pin_count=3))
        assert ctx.package is not None
        try:
            # The class registration beats the package's own registered kind…
            assert renderer_for(
                ctx.package, component_class="_t_led_bar",
            ) is _LedBarRenderer
            # …while the same package without the class keeps its kind renderer.
            assert renderer_for(ctx.package) is SilHeaderRenderer
        finally:
            del CLASS_RENDERERS["_t_led_bar"]

    def test_package_body_threads_component_class(self) -> None:
        @register_renderer(component_class="_t_led_bar")
        class _LedBarRenderer(PrismRenderer):
            pass

        ctx = build_kind_package("sil", GeneratorContext(pin_count=3))
        assert ctx.package is not None
        try:
            body = PackageBody(ctx.package, component_class="_t_led_bar")
            assert type(body) is _LedBarRenderer
        finally:
            del CLASS_RENDERERS["_t_led_bar"]

    def test_unknown_component_class_falls_back_to_lineage(self) -> None:
        from protoboard.gui.renderers.packages import SilHeaderRenderer

        ctx = build_kind_package("sil", GeneratorContext(pin_count=3))
        assert ctx.package is not None
        assert renderer_for(
            ctx.package, component_class="_t_no_such_class",
        ) is SilHeaderRenderer

    def test_no_axis_is_an_error(self) -> None:
        with pytest.raises(ValueError):
            register_renderer()

    def test_later_registration_wins(self) -> None:
        @register_renderer(kind="_t_collide")
        class _First(PrismRenderer):
            pass

        @register_renderer(kind="_t_collide")
        class _Second(PrismRenderer):
            pass

        try:
            assert BODY_RENDERERS["_t_collide"] is _Second
        finally:
            del BODY_RENDERERS["_t_collide"]


# ---------------------------------------------------------------------------
# Plugin discovery
# ---------------------------------------------------------------------------

_PLUGIN_SRC = dedent(
    """
    from protoboard.gui.renderers.packages import CylinderRenderer

    class PluginTestRenderer(CylinderRenderer):
        renders = "plugin_test_lineage"
    """
)


class TestPluginDiscovery:
    def test_plugin_file_registers_renderer(self, tmp_path: Path) -> None:
        rdir = tmp_path / "renderers"
        rdir.mkdir()
        (rdir / "plugin_test.py").write_text(_PLUGIN_SRC)
        try:
            loaded = load_renderer_plugins([tmp_path])
            assert loaded == [rdir / "plugin_test.py"]
            assert "plugin_test_lineage" in BODY_RENDERERS
        finally:
            BODY_RENDERERS.pop("plugin_test_lineage", None)

    def test_broken_plugin_is_skipped(self, tmp_path: Path) -> None:
        rdir = tmp_path / "renderers"
        rdir.mkdir()
        (rdir / "broken.py").write_text("raise RuntimeError('boom')\n")
        (rdir / "z_good.py").write_text(_PLUGIN_SRC.replace(
            "plugin_test_lineage", "plugin_after_broken"))
        try:
            loaded = load_renderer_plugins([tmp_path])
            assert loaded == [rdir / "z_good.py"]
            assert "plugin_after_broken" in BODY_RENDERERS
        finally:
            BODY_RENDERERS.pop("plugin_after_broken", None)

    def test_missing_renderers_dir_is_fine(self, tmp_path: Path) -> None:
        assert load_renderer_plugins([tmp_path]) == []


# ---------------------------------------------------------------------------
# TO-220 builtin plugin — the reference plugin, loaded via the same discovery
# ---------------------------------------------------------------------------

@pytest.fixture
def to220_package():
    from protoboard.services.library.package_family_loader import (
        load_full_package_catalogue,
    )
    family = load_full_package_catalogue()["TO-220"]
    return family.variants[0].package


class TestTO220Plugin:
    def test_family_lineage_carries_family_id(self, to220_package) -> None:
        assert "prism_radial" in to220_package.lineage
        assert to220_package.lineage[-2:] == ("TO-220", "TO-220/std")

    def test_builtin_plugin_registers_and_draws_tab(
        self, qtbot, to220_package,
    ) -> None:
        from PySide6.QtGui import QColor

        from protoboard.gui.theme import DEFAULT_THEME
        from protoboard.paths import BUILTIN_LIBRARY_DIR

        loaded = load_renderer_plugins([BUILTIN_LIBRARY_DIR])
        assert any(p.name == "to220.py" for p in loaded)
        assert "TO-220" in BODY_RENDERERS

        pads = [Point(0.0, 0.0), Point(1.0, 0.0), Point(2.0, 0.0)]
        fill = QColor("#888888")
        border = QColor("#222222")

        base_items = PrismRenderer(to220_package).draw(
            pads, fill, border, 1.0, 5.0, DEFAULT_THEME)
        plugin_items = PackageBody(to220_package).draw(
            pads, fill, border, 1.0, 5.0, DEFAULT_THEME)
        # Lineage dispatch picks the plugin, which adds exactly one item —
        # the light metal tab strip on top of the plastic body.
        assert type(PackageBody(to220_package)).__name__ == "TO220Renderer"
        assert len(plugin_items) == len(base_items) + 1
        tab = plugin_items[-1]
        assert tab.zValue() > max(i.zValue() for i in base_items)

    def test_tab_hugs_the_body_long_edge(self, qtbot, to220_package) -> None:
        # Regression: the tab used bbox width/height transposed (the bbox is
        # oriented by the dominant pin axis, x for TO-220's pin row), so it
        # spanned half the body and floated outside it.  The tab must lie
        # INSIDE the body rectangle and run its full length along the pin row.
        from PySide6.QtGui import QColor

        from protoboard.gui.theme import DEFAULT_THEME
        from protoboard.paths import BUILTIN_LIBRARY_DIR

        load_renderer_plugins([BUILTIN_LIBRARY_DIR])
        pads = [Point(0.0, 0.0), Point(1.0, 0.0), Point(2.0, 0.0)]
        fill = QColor("#888888")
        border = QColor("#222222")

        base_items = PrismRenderer(to220_package).draw(
            pads, fill, border, 1.0, 5.0, DEFAULT_THEME)
        body = max(
            base_items,
            key=lambda it: (it.sceneBoundingRect().width()
                            * it.sceneBoundingRect().height()),
        ).sceneBoundingRect()
        plugin_items = PackageBody(to220_package).draw(
            pads, fill, border, 1.0, 5.0, DEFAULT_THEME)
        tab = plugin_items[-1].sceneBoundingRect()

        assert body.contains(tab)
        # Full body length along the pin row (pen-width tolerance)…
        assert tab.width() == pytest.approx(body.width(), abs=0.1)
        # …as a thin strip on the −y long edge, not a half-width block.
        assert tab.height() < body.height() / 2.0
        assert abs(tab.top() - body.top()) < 0.1


# ---------------------------------------------------------------------------
# Per-draw paint state (self.paint / paint_override)
# ---------------------------------------------------------------------------

class TestPaintState:
    def test_paint_raises_outside_draw(self, do_test_package) -> None:
        renderer = CylinderRenderer(do_test_package)
        with pytest.raises(RuntimeError, match="draw"):
            _ = renderer.paint

    def test_draw_exposes_resolved_paint(self, qtbot, do_test_package) -> None:
        from PySide6.QtGui import QColor

        from protoboard.gui.theme import DEFAULT_THEME

        seen = {}

        class Probe(CylinderRenderer):
            def render_body(self, positions):
                seen["paint"] = self.paint
                return super().render_body(positions)

        Probe(do_test_package).draw(
            [Point(0.0, 0.0), Point(4.0, 0.0)],
            QColor("#888888"), QColor("#222222"), 0.5, 7.0, DEFAULT_THEME,
        )
        paint = seen["paint"]
        assert paint.fill.name() == "#888888"
        assert paint.alpha == 0.5
        assert paint.z == 7.0
        assert paint.theme is DEFAULT_THEME

    def test_paint_override_recolours_base_and_restores(
        self, qtbot, do_test_package,
    ) -> None:
        from PySide6.QtGui import QColor

        from protoboard.gui.theme import DEFAULT_THEME

        class Overrider(CylinderRenderer):
            def render_body(self, positions):
                with self.paint_override(fill="#000000"):
                    items = super().render_body(positions)
                assert self.paint.fill.name() == "#888888"  # restored
                extra = self.axis_canvas(positions[:2], positions[0],
                                         positions[1])
                extra.addRect((-0.5, -0.5), (0.5, 0.5))
                return items + extra.items

        items = Overrider(do_test_package).draw(
            [Point(0.0, 0.0), Point(4.0, 0.0)],
            QColor("#888888"), QColor("#222222"), 1.0, 5.0, DEFAULT_THEME,
        )
        fills = [it.brush().color().name() for it in items
                 if hasattr(it, "brush")]
        assert "#000000" in fills   # the base body took the override
        assert "#888888" in fills   # the detail kept the resolved fill
