# SPDX-License-Identifier: MIT
"""Tests for the footprint-choice pop-ups and their seam provider.

The dialogs are pure UI driven by a ``FootprintChoiceProvider``; these tests use
the shipped ``DefaultFootprintChoiceProvider`` and a small stub provider that
returns kind parameters / variants, so the kind pop-up is exercised ahead of the
(user-built) parameter model.
"""

from __future__ import annotations

from pathlib import Path

import pytest
from PySide6.QtWidgets import QDialog
from pytestqt.qtbot import QtBot

from protoboard.gui.dialogs.footprint_picker_dialog import FootprintPickerDialog
from protoboard.gui.dialogs.kind_variant_dialog import KindVariantDialog
from protoboard.gui.footprint_choice import (
    DefaultFootprintChoiceProvider,
    FootprintOptionView,
    KindVariantView,
    ParamSpec,
)
from protoboard.domain.components.package.shapes import Prism
from protoboard.services.library.loader import load_library
from protoboard.services.library.package_family_loader import (
    load_full_package_catalogue,
)

# --------------------------------------------------------------------------- #
# Stub provider — exercises the kind pop-up without the parameter model
# --------------------------------------------------------------------------- #

class _StubProvider:
    def __init__(self, *, params=(), variants=(), built="built/addr"):
        self._params = list(params)
        self._variants = list(variants)
        self._built = built

    def options(self, defn):  # not used by the dialog tests directly
        return []

    def recent(self, defn_ref, offered):
        return []

    def kind_params(self, address):
        return list(self._params)

    def recent_variants(self, address):
        return list(self._variants)

    def build_variant(self, address, values):
        return self._built


def _opt(label: str, address: str, is_kind: bool = False) -> FootprintOptionView:
    return FootprintOptionView(label=label, address=address, is_kind=is_kind)


# --------------------------------------------------------------------------- #
# Pop-up 1 — FootprintPickerDialog
# --------------------------------------------------------------------------- #

class TestFootprintPickerDialog:
    def _dialog(self, qtbot: QtBot, recent, options, provider=None):
        dlg = FootprintPickerDialog(
            provider or _StubProvider(), recent, options
        )
        qtbot.addWidget(dlg)
        return dlg

    def test_first_row_preselected(self, qtbot: QtBot) -> None:
        dlg = self._dialog(
            qtbot, [], [_opt("A", "a/1"), _opt("B", "b/1")]
        )
        assert dlg._list.currentRow() == 0

    def test_recent_rows_listed_first(self, qtbot: QtBot) -> None:
        dlg = self._dialog(
            qtbot, [_opt("R1", "r/1")], [_opt("A", "a/1")]
        )
        assert dlg._list.item(0).text().startswith("★ ")
        assert dlg._list.item(1).text() == "A"

    def test_double_click_package_returns_address(self, qtbot: QtBot) -> None:
        dlg = self._dialog(qtbot, [], [_opt("A", "a/1"), _opt("B", "b/1")])
        dlg._confirm_item(dlg._list.item(1))
        assert dlg.chosen_address == "b/1"
        assert dlg.result() == QDialog.DialogCode.Accepted

    def test_search_filters_and_reselects(self, qtbot: QtBot) -> None:
        dlg = self._dialog(
            qtbot, [], [_opt("DIP-8", "dip-8"), _opt("SIL-4", "sil-4")]
        )
        dlg._search.setText("sil")
        assert dlg._list.item(0).isHidden()        # DIP-8 hidden
        assert not dlg._list.item(1).isHidden()    # SIL-4 visible
        assert dlg._list.currentRow() == 1         # selection follows

    def test_kind_row_opens_variant_dialog(
        self, qtbot: QtBot, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        # Confirming a kind row runs the variant pop-up; we stub it to return an
        # address, which becomes the picker's result.
        opened: list[str] = []

        def _fake_run(self_, address: str) -> str | None:
            opened.append(address)
            return "kind/built"

        monkeypatch.setattr(
            FootprintPickerDialog, "_run_variant_dialog", _fake_run
        )
        dlg = self._dialog(qtbot, [], [_opt("PinHeader", "ph", is_kind=True)])
        dlg._confirm_item(dlg._list.item(0))
        assert opened == ["ph"]
        assert dlg.chosen_address == "kind/built"

    def test_kind_dialog_cancel_keeps_picker_open(
        self, qtbot: QtBot, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        monkeypatch.setattr(
            FootprintPickerDialog,
            "_run_variant_dialog",
            lambda self_, address: None,  # user cancelled stage 2
        )
        dlg = self._dialog(qtbot, [], [_opt("PinHeader", "ph", is_kind=True)])
        dlg._confirm_item(dlg._list.item(0))
        assert dlg.chosen_address is None
        assert dlg.result() != QDialog.DialogCode.Accepted


# --------------------------------------------------------------------------- #
# Pop-up 2 — KindVariantDialog
# --------------------------------------------------------------------------- #

class TestKindVariantDialog:
    def test_recent_variant_one_confirm(self, qtbot: QtBot) -> None:
        provider = _StubProvider(
            variants=[KindVariantView("5x2 header", "ph/5x2")]
        )
        dlg = KindVariantDialog(provider, "ph")
        qtbot.addWidget(dlg)
        assert dlg._recent_list.currentRow() == 0   # top pre-selected
        dlg._confirm_recent(dlg._recent_list.item(0))
        assert dlg.chosen_address == "ph/5x2"

    def test_new_params_build_variant(self, qtbot: QtBot) -> None:
        provider = _StubProvider(
            params=[ParamSpec("rows", "int")], built="ph/rows3"
        )
        dlg = KindVariantDialog(provider, "ph")
        qtbot.addWidget(dlg)
        dlg._inputs["rows"].setText("3")
        dlg._on_ok()
        assert dlg.chosen_address == "ph/rows3"

    def test_invalid_param_blocks_ok(self, qtbot: QtBot) -> None:
        provider = _StubProvider(params=[ParamSpec("rows", "int")])
        dlg = KindVariantDialog(provider, "ph")
        qtbot.addWidget(dlg)
        dlg._inputs["rows"].setText("not-an-int")
        dlg._on_ok()
        assert dlg.chosen_address is None           # rejected, stays open
        assert dlg.result() != QDialog.DialogCode.Accepted

    def test_no_params_no_variants_builds_directly(self, qtbot: QtBot) -> None:
        # A kind that needs nothing: OK resolves it as-is via build_variant.
        provider = _StubProvider(built="ph/asis")
        dlg = KindVariantDialog(provider, "ph")
        qtbot.addWidget(dlg)
        dlg._on_ok()
        assert dlg.chosen_address == "ph/asis"


# --------------------------------------------------------------------------- #
# DefaultFootprintChoiceProvider — the shipped seam (no parameter model yet)
# --------------------------------------------------------------------------- #

@pytest.fixture(scope="module")
def lib_and_cat():
    cat = load_full_package_catalogue()
    lib = load_library([Path("library")], cat)
    return lib, cat


class TestDefaultProvider:
    def test_package_footprints_not_kind(self, lib_and_cat) -> None:
        lib, cat = lib_and_cat
        provider = DefaultFootprintChoiceProvider(cat)
        opts = provider.options(lib.get("R"))
        assert len(opts) > 1
        assert all(not o.is_kind for o in opts)

    def test_single_package_part(self, lib_and_cat) -> None:
        lib, cat = lib_and_cat
        provider = DefaultFootprintChoiceProvider(cat)
        opts = provider.options(lib.get("2N2222"))
        assert len(opts) == 1 and not opts[0].is_kind

    def test_connector_reports_kind(self, lib_and_cat) -> None:
        lib, cat = lib_and_cat
        provider = DefaultFootprintChoiceProvider(cat)
        for name in ("PinHeader_1x04", "JST_PH_3pin"):
            opts = provider.options(lib.get(name))
            assert len(opts) == 1 and opts[0].is_kind, name

    def test_unresolvable_address_offers_nothing(self, lib_and_cat) -> None:
        _lib, cat = lib_and_cat
        provider = DefaultFootprintChoiceProvider(cat)
        assert provider.kind_params("anything") == []
        assert provider.recent_variants("anything") == []
        # No values → pass-through; values on an unresolvable kind → None.
        assert provider.build_variant("addr", {}) == "addr"
        assert provider.build_variant("addr", {"x": "1"}) is None

    def test_fully_parameterised_kind_offers_no_params(
        self, lib_and_cat
    ) -> None:
        # PinHeader_1x04 fixes rows=1 in YAML — nothing left to ask; the
        # picker's one-confirm path applies.
        lib, cat = lib_and_cat
        provider = DefaultFootprintChoiceProvider(cat)
        provider.options(lib.get("PinHeader_1x04"))
        assert provider.kind_params("") == []


class TestProviderKindParams:
    """Registry-backed holes → picker inputs → built variant."""

    def _defn_with_hole(self, tmp_path):
        from protoboard.services.library.loader import load_library
        (tmp_path / "lib.yaml").write_text(
            "components:\n"
            "  - name: HDR_ASK\n"
            "    component_class: connector\n"
            "    pins: {count: 6}\n"
            "    package: {kind: pin_header, rows: null}\n"
        )
        return load_library([tmp_path]).get("HDR_ASK")

    def test_hole_becomes_param_row(self, tmp_path) -> None:
        defn = self._defn_with_hole(tmp_path)
        assert defn is not None
        provider = DefaultFootprintChoiceProvider({})
        opts = provider.options(defn)
        assert len(opts) == 1 and opts[0].is_kind
        params = provider.kind_params(opts[0].address)
        assert [(p.name, p.type) for p in params] == [("rows", "int")]

    def test_build_variant_registers_and_resolves(self, tmp_path) -> None:
        from protoboard.domain.components.package.family import (
            resolve_variant,
        )
        defn = self._defn_with_hole(tmp_path)
        assert defn is not None
        families: dict = {}
        provider = DefaultFootprintChoiceProvider(families)
        opts = provider.options(defn)
        address = provider.build_variant(opts[0].address, {"rows": "2"})
        assert address is not None and "/" in address
        variant = resolve_variant(families, address)
        assert variant is not None
        assert isinstance(variant.package, Prism)
        assert variant.package.kind == "pin_header"
        # 2×6 header: dual-row geometry (shorter along the row than 1×6).
        assert variant.package.bbox().ly < 6

    def test_build_variant_invalid_values_returns_none(self, tmp_path) -> None:
        defn = self._defn_with_hole(tmp_path)
        assert defn is not None
        provider = DefaultFootprintChoiceProvider({})
        opts = provider.options(defn)
        # rows=3 is unrealisable for a pin header → generator raises → None.
        assert provider.build_variant(opts[0].address, {"rows": "3"}) is None


class TestRecentVariants:
    """Per-kind variant history: build_variant remembers the parameter set,
    recent_variants re-runs it (registering the family) so the kind dialog's
    one-confirm row works — across provider instances, i.e. across sessions."""

    def _defn_with_hole(self, tmp_path, name="HDR_ASK", pins=6):
        from protoboard.services.library.loader import load_library
        (tmp_path / f"{name}.yaml").write_text(
            f"components:\n"
            f"  - name: {name}\n"
            f"    component_class: connector\n"
            f"    pins: {{count: {pins}}}\n"
            f"    package: {{kind: pin_header, rows: null}}\n"
        )
        return load_library([tmp_path]).get(name)

    def test_build_variant_is_reoffered_in_a_fresh_session(
        self, tmp_path
    ) -> None:
        from protoboard.domain.components.package.family import (
            resolve_variant,
        )
        defn = self._defn_with_hole(tmp_path)
        provider = DefaultFootprintChoiceProvider({})
        opts = provider.options(defn)
        built = provider.build_variant(opts[0].address, {"rows": "2"})
        assert built is not None

        # A fresh provider + empty catalogue = a new session.
        families: dict = {}
        fresh = DefaultFootprintChoiceProvider(families)
        addr = fresh.options(defn)[0].address
        views = fresh.recent_variants(addr)
        assert [v.address for v in views] == [built]
        # The rebuild registered the family, so confirming the row resolves.
        variant = resolve_variant(families, built)
        assert variant is not None and isinstance(variant.package, Prism)
        assert variant.package.kind == "pin_header"

    def test_remembered_parameters_follow_the_pin_count(
        self, tmp_path
    ) -> None:
        # The history stores the PARAMETER SET, not a geometry: rows=2 built
        # on a 6-pin part re-offers as a 2×5 on a 10-pin part.
        defn6 = self._defn_with_hole(tmp_path, name="HDR6", pins=6)
        provider = DefaultFootprintChoiceProvider({})
        provider.options(defn6)
        assert provider.build_variant(
            provider.options(defn6)[0].address, {"rows": "2"}) is not None

        defn10 = self._defn_with_hole(tmp_path, name="HDR10", pins=10)
        families: dict = {}
        fresh = DefaultFootprintChoiceProvider(families)
        addr = fresh.options(defn10)[0].address
        views = fresh.recent_variants(addr)
        assert len(views) == 1
        assert views[0].address.startswith("pin_header-10")

    def test_unbuildable_history_entry_is_skipped(self, tmp_path) -> None:
        from protoboard.gui import recent_kind_variants
        recent_kind_variants.add("no_such_kind", {"x": 1}, "ghost")
        defn = self._defn_with_hole(tmp_path)
        provider = DefaultFootprintChoiceProvider({})
        addr = provider.options(defn)[0].address
        # Entries for other/vanished kinds never surface for this kind...
        assert provider.recent_variants(addr) == []
        # ...and an unrealisable parameter set for THIS kind is skipped too.
        recent_kind_variants.add("pin_header", {"rows": 3}, "bad rows")
        assert provider.recent_variants(addr) == []

    def test_history_dedupes_by_parameter_set(self, tmp_path) -> None:
        defn = self._defn_with_hole(tmp_path)
        provider = DefaultFootprintChoiceProvider({})
        addr = provider.options(defn)[0].address
        assert provider.build_variant(addr, {"rows": "2"}) is not None
        assert provider.build_variant(addr, {"rows": "1"}) is not None
        assert provider.build_variant(addr, {"rows": "2"}) is not None  # re-pick
        views = provider.recent_variants(addr)
        # Two distinct parameter sets; the re-picked one is most recent.
        assert len(views) == 2
        assert views[0].address.endswith("-rows2/std")

    def test_dialog_one_confirm_uses_the_history(
        self, qtbot: QtBot, tmp_path
    ) -> None:
        # End-to-end: a previously-built variant shows in the pop-up's
        # "Recently used" list and OK confirms it with no input.
        defn = self._defn_with_hole(tmp_path)
        provider = DefaultFootprintChoiceProvider({})
        addr = provider.options(defn)[0].address
        built = provider.build_variant(addr, {"rows": "2"})
        assert built is not None

        fresh = DefaultFootprintChoiceProvider({})
        fresh_addr = fresh.options(defn)[0].address
        dlg = KindVariantDialog(fresh, fresh_addr)
        qtbot.addWidget(dlg)
        assert dlg._recent_list.count() == 1
        assert dlg._recent_list.currentRow() == 0   # top pre-selected
        dlg._confirm_recent(dlg._recent_list.item(0))  # the one-confirm path
        assert dlg.chosen_address == built
