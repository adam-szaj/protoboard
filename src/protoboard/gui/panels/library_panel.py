# SPDX-License-Identifier: MIT
"""Library panel — browse and select components for placement."""

from __future__ import annotations

from collections import defaultdict
from pathlib import Path
from typing import TYPE_CHECKING

from PySide6.QtCore import Qt, Signal
from PySide6.QtWidgets import (
    QDialog,
    QLineEdit,
    QTreeWidget,
    QTreeWidgetItem,
    QVBoxLayout,
    QWidget,
)

from protoboard.gui.footprint_choice import DefaultFootprintChoiceProvider
from protoboard.services.library.loader import ComponentLibrary

if TYPE_CHECKING:
    from protoboard.domain.components.package.family import PackageFamily
    from protoboard.gui.footprint_choice import (
        FootprintChoiceProvider,
        FootprintOptionView,
    )

# Sentinel distinguishing "the user dismissed the picker" (place nothing) from a
# resolved ``None`` address (place with the component's own default geometry).
_CANCELLED: object = object()


class LibraryPanel(QWidget):
    # emits (definition_ref, package_variant_address_or_None)
    component_selected = Signal(str, object)

    def __init__(self, library: ComponentLibrary, parent: object = None) -> None:
        super().__init__(parent)  # type: ignore[arg-type]
        self._library = library
        # The footprint-choice seam: answers package-vs-kind, recent picks and
        # kind parameters for the pop-up. The default provider reports every
        # footprint as a package with no parameters; a session may inject a
        # model-backed one via ``set_choice_provider``.
        self._choice: FootprintChoiceProvider = DefaultFootprintChoiceProvider()

        layout = QVBoxLayout(self)
        layout.setContentsMargins(4, 4, 4, 4)
        layout.setSpacing(4)

        self._search = QLineEdit()
        self._search.setPlaceholderText("Search components…")
        self._search.setClearButtonEnabled(True)
        layout.addWidget(self._search)

        self._tree = QTreeWidget()
        self._tree.setHeaderHidden(True)
        self._tree.setIndentation(14)
        layout.addWidget(self._tree)

        self._search.textChanged.connect(self._on_search)
        self._tree.itemDoubleClicked.connect(self._on_double_click)

        self._populate()

    # ------------------------------------------------------------------
    # Build tree
    # ------------------------------------------------------------------

    def _populate(self, filter_text: str = "") -> None:
        self._tree.clear()
        q = filter_text.strip().lower()

        # Collect names that pass the filter, together with their source path.
        # Structure: {source_yaml_path: [name, ...]}  (order preserved via sorted)
        # Components without a known source go into a synthetic "Other" bucket.
        _NO_SOURCE = Path("<other>")

        by_file: dict[Path, list[str]] = defaultdict(list)
        for name in sorted(self._library.names()):
            if q:
                hit = q in name.lower()
                if not hit:
                    try:
                        hit = q in self._library.get(name).description.lower()
                    except KeyError:
                        pass
                if not hit:
                    continue
            src = self._library.source_path(name) or _NO_SOURCE
            by_file[src].append(name)

        if not by_file:
            return

        # Determine the common root so we can build relative sub-paths.
        real_paths = [p for p in by_file if p != _NO_SOURCE]
        if real_paths:
            # Find longest common ancestor directory shared by all source files.
            common = real_paths[0].parent
            for p in real_paths[1:]:
                while common != p.parent and common not in p.parents:
                    common = common.parent
            # common is now the root we strip from display paths.
            root_dir = common
        else:
            root_dir = Path(".")

        # Build an intermediate tree:
        # { subdir_parts: { file_stem: [name, ...] } }
        # e.g. ("audio",) -> {"opamps": ["LM358", ...]}
        # A flat library (all files at root) → subdir_parts = ()
        tree: dict[tuple[str, ...], dict[str, list[str]]] = defaultdict(lambda: defaultdict(list))

        for src, names in by_file.items():
            if src == _NO_SOURCE:
                subdir: tuple[str, ...] = ()
                stem = "Other"
            else:
                rel = src.relative_to(root_dir)
                parts = rel.parts          # e.g. ("audio", "opamps.yaml")
                subdir = tuple(parts[:-1])  # directory components
                stem = src.stem            # filename without .yaml
            for name in names:
                tree[subdir][stem].append(name)

        # Render tree into QTreeWidget.
        # If there is only one subdir (including the root ""), skip the dir level.
        all_subdirs: list[tuple[str, ...]] = sorted(tree.keys())
        single_subdir = len(all_subdirs) == 1

        def _add_file_node(
            parent: QTreeWidget | QTreeWidgetItem,
            stem: str,
            names: list[str],
        ) -> None:
            count = len(names)
            file_item = QTreeWidgetItem(parent, [f"{stem}  ({count})"])
            file_item.setFlags(file_item.flags() & ~Qt.ItemFlag.ItemIsSelectable)
            font = file_item.font(0)
            font.setBold(True)
            file_item.setFont(0, font)
            for name in names:
                try:
                    defn = self._library.get(name)
                    desc = defn.description
                    tooltip = f"{name}  —  {desc}" if desc else name
                except KeyError:
                    tooltip = name
                child = QTreeWidgetItem(file_item, [name])
                child.setToolTip(0, tooltip)
                child.setData(0, Qt.ItemDataRole.UserRole, name)

        for subdir in all_subdirs:
            file_map = tree[subdir]
            sorted_stems = sorted(file_map.keys())

            if single_subdir:
                # Flat: file nodes directly under root.
                for stem in sorted_stems:
                    _add_file_node(self._tree, stem, file_map[stem])
            else:
                # Show subdir node (or "(root)" for files at the common root).
                dir_label = "/".join(subdir) if subdir else "(root)"
                total = sum(len(v) for v in file_map.values())
                dir_item = QTreeWidgetItem(self._tree, [f"{dir_label}  ({total})"])
                dir_item.setFlags(dir_item.flags() & ~Qt.ItemFlag.ItemIsSelectable)
                for stem in sorted_stems:
                    _add_file_node(dir_item, stem, file_map[stem])

        if q:
            self._tree.expandAll()
        else:
            self._tree.collapseAll()

    # ------------------------------------------------------------------

    def set_library(self, library: ComponentLibrary) -> None:
        """Replace the library and repopulate the tree."""
        self._library = library
        self._populate(self._search.text())

    def _on_search(self, text: str) -> None:
        self._populate(text)

    def _on_double_click(self, item: QTreeWidgetItem, _column: int) -> None:
        data = item.data(0, Qt.ItemDataRole.UserRole)
        if not data:
            return
        name = str(data)
        try:
            defn = self._library.get(name)
        except KeyError:
            return

        options = self._choice.options(defn)
        offered = {o.address for o in options}
        recent = self._choice.recent(name, offered)

        resolved = self._resolve_address(name, options, recent)
        if resolved is _CANCELLED:
            return  # the user dismissed the pop-up — nothing placed
        address: str | None = resolved  # type: ignore[assignment]
        if address is not None:
            self._record_recent(name, address, options, recent)
        self.component_selected.emit(name, address)

    def _resolve_address(
        self,
        name: str,
        options: list[FootprintOptionView],
        recent: list[FootprintOptionView],
    ) -> str | None | object:
        """The footprint address to place with, or ``_CANCELLED``.

        No pop-up when there is nothing to choose: zero options (the component's
        own default geometry, address ``None``) or a single *package* option.
        Anything else (several options, or a *kind* that may need parameters)
        opens the picker; a dismissed picker returns ``_CANCELLED``.
        """
        if not options:
            return None
        if len(options) == 1 and not options[0].is_kind:
            return options[0].address
        # A single kind option with no parameters / variants to offer is also a
        # no-choice: place it directly rather than show an empty pop-up.
        if len(options) == 1 and options[0].is_kind and not recent:
            opt = options[0]
            if not self._choice.kind_params(opt.address) and not (
                self._choice.recent_variants(opt.address)
            ):
                return self._choice.build_variant(opt.address, {})

        from protoboard.gui.dialogs.footprint_picker_dialog import (
            FootprintPickerDialog,
        )

        dialog = FootprintPickerDialog(self._choice, recent, options, self)
        if dialog.exec() != QDialog.DialogCode.Accepted:
            return _CANCELLED
        return dialog.chosen_address

    def _record_recent(
        self,
        name: str,
        address: str,
        options: list[FootprintOptionView],
        recent: list[FootprintOptionView],
    ) -> None:
        from protoboard.gui import recent_footprints

        label = next(
            (o.label for o in [*recent, *options] if o.address == address),
            address or name,
        )
        recent_footprints.add(name, address, label)

    def set_choice_provider(self, provider: FootprintChoiceProvider) -> None:
        """Replace the footprint-choice seam (e.g. a model-backed provider)."""
        self._choice = provider

    def set_package_families(
        self, families: dict[str, PackageFamily] | None
    ) -> None:
        """Wire the package-family catalogue from the active session.

        Called by MainWindow whenever the session changes. The default provider
        uses the catalogue to resolve footprint addresses and detect kinds; a
        custom provider injected via :meth:`set_choice_provider` ignores this.
        """
        if isinstance(self._choice, DefaultFootprintChoiceProvider):
            self._choice.set_package_families(families or {})
