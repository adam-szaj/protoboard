# SPDX-License-Identifier: MIT
"""Library panel — browse and select components for placement."""

from __future__ import annotations

from collections import defaultdict
from pathlib import Path

from PySide6.QtCore import Qt, Signal
from PySide6.QtWidgets import (
    QComboBox,
    QHBoxLayout,
    QLabel,
    QLineEdit,
    QTreeWidget,
    QTreeWidgetItem,
    QVBoxLayout,
    QWidget,
)

from protoboard.services.library.loader import ComponentLibrary


class LibraryPanel(QWidget):
    # emits (definition_ref, pin_variant_id_or_None, body_variant_id_or_None)
    # The third element is the address "package_id/variant_id" or None
    # to inherit the definition's default.  Older slots that accept
    # only two args still work — Qt unpacks positional signal args.
    component_selected = Signal(str, object, object)

    def __init__(self, library: ComponentLibrary, parent: object = None) -> None:
        super().__init__(parent)  # type: ignore[arg-type]
        self._library = library
        # Package-family catalogue is wired by main_window once the session
        # exists; until then the picker stays empty.
        self._package_families: dict = {}

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

        # Body variant picker — visible only when the selection is a
        # two-pin component and the catalogue is wired.
        self._body_row = QWidget()
        body_layout = QHBoxLayout(self._body_row)
        body_layout.setContentsMargins(0, 0, 0, 0)
        body_layout.setSpacing(4)
        body_layout.addWidget(QLabel("Body:"))
        self._body_variant_combo = QComboBox()
        self._body_variant_combo.setToolTip(
            "Body package used when this component is placed.  The picker "
            "lists the variants of the library default first, then every "
            "other shipped package.",
        )
        body_layout.addWidget(self._body_variant_combo, 1)
        layout.addWidget(self._body_row)
        self._body_row.setVisible(False)
        # Parallel to combo indices.
        self._body_variant_addresses: list[str | None] = []

        self._search.textChanged.connect(self._on_search)
        self._tree.itemDoubleClicked.connect(self._on_double_click)
        self._tree.currentItemChanged.connect(self._on_selection_changed)

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
                    variants = defn.variants
                except KeyError:
                    tooltip = name
                    variants = ()
                child = QTreeWidgetItem(file_item, [name])
                child.setToolTip(0, tooltip)
                child.setData(0, Qt.ItemDataRole.UserRole, (name, None))
                if variants:
                    child.setFlags(child.flags() & ~Qt.ItemFlag.ItemIsSelectable)
                    font2 = child.font(0)
                    font2.setItalic(True)
                    child.setFont(0, font2)
                    for v in variants:
                        v_item = QTreeWidgetItem(child, [v.label])
                        v_item.setToolTip(0, f"{name}  [{v.id}]")
                        v_item.setData(0, Qt.ItemDataRole.UserRole, (name, v.id))

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
        if data:
            name, pin_variant = data
            self.component_selected.emit(
                name, pin_variant, self._current_body_variant_address(),
            )

    def _current_body_variant_address(self) -> "str | None":
        idx = self._body_variant_combo.currentIndex()
        if idx < 0 or idx >= len(self._body_variant_addresses):
            return None
        return self._body_variant_addresses[idx]

    def set_package_families(self, families: dict) -> None:
        """Wire the package-family catalogue from the active session.

        Called by MainWindow whenever the session changes (open / close
        project, switch between projects).  The picker is rebuilt to
        reflect the current selection.
        """
        self._package_families = families or {}
        self._refresh_body_picker_for_current()

    def _on_selection_changed(
        self,
        current: "QTreeWidgetItem | None",
        _previous: "QTreeWidgetItem | None",
    ) -> None:
        self._refresh_body_picker_for_current()

    def _refresh_body_picker_for_current(self) -> None:
        current = self._tree.currentItem()
        self._rebuild_body_picker(current)

    def _rebuild_body_picker(
        self, current: "QTreeWidgetItem | None",
    ) -> None:
        from protoboard.domain.components.footprints import TwoPin

        self._body_variant_combo.blockSignals(True)
        self._body_variant_combo.clear()
        self._body_variant_addresses = []

        data = current.data(0, Qt.ItemDataRole.UserRole) if current else None
        defn = None
        if data:
            name, _pin_variant = data
            try:
                defn = self._library.get(name)
            except KeyError:
                defn = None
        show = (
            defn is not None
            and isinstance(defn.footprint, TwoPin)
            and bool(self._package_families)
        )
        self._body_row.setVisible(show)
        if not show or defn is None:
            self._body_variant_combo.blockSignals(False)
            return

        # Default entry first (uses defn.package_family_ref at place time).
        default_label = "Default"
        if defn.package_family_ref:
            default_label = f"Default ({defn.package_family_ref})"
        self._body_variant_combo.addItem(default_label)
        self._body_variant_addresses.append(None)

        primary_pkg_id: "str | None" = None
        if defn.package_family_ref:
            parts = defn.package_family_ref.split("/", 1)
            if len(parts) == 2:
                primary_pkg_id = parts[0]

        sorted_pkg_ids = sorted(self._package_families.keys())
        ordered = []
        if primary_pkg_id and primary_pkg_id in self._package_families:
            ordered.append(primary_pkg_id)
        ordered.extend(p for p in sorted_pkg_ids if p != primary_pkg_id)

        for pkg_id in ordered:
            pkg = self._package_families[pkg_id]
            for v in pkg.variants:
                addr = f"{pkg_id}/{v.variant_id}"
                label = f"{pkg_id} / {v.variant_id}"
                if v.label and v.label != v.variant_id:
                    label = f"{label} — {v.label}"
                self._body_variant_combo.addItem(label)
                self._body_variant_addresses.append(addr)

        self._body_variant_combo.setCurrentIndex(0)
        self._body_variant_combo.blockSignals(False)
