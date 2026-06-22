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

    def test_default_has_no_params_or_variants(self, lib_and_cat) -> None:
        _lib, cat = lib_and_cat
        provider = DefaultFootprintChoiceProvider(cat)
        assert provider.kind_params("anything") == []
        assert provider.recent_variants("anything") == []
        assert provider.build_variant("addr", {"x": "1"}) == "addr"
