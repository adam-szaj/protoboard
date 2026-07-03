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
    CylinderRenderer,
    DiscRenderer,
    PrismRenderer,
    load_renderer_plugins,
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

    def test_kind_falls_back_to_prism(self) -> None:
        ctx = build_kind_package("sil", GeneratorContext(pin_count=3))
        assert ctx.package is not None
        # No renderer registered for "sil" → the prism base takes it.
        assert renderer_for(ctx.package) is PrismRenderer

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
