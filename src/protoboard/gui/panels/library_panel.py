# SPDX-License-Identifier: MIT
"""Library panel — browse and select components for placement."""

from __future__ import annotations

from collections import defaultdict
from pathlib import Path

from PySide6.QtCore import Qt, Signal
from PySide6.QtWidgets import (
    QLineEdit,
    QTreeWidget,
    QTreeWidgetItem,
    QVBoxLayout,
    QWidget,
)

from protoboard.services.library.loader import ComponentLibrary


class LibraryPanel(QWidget):
    component_selected = Signal(str)  # emits definition_ref name

    def __init__(self, library: ComponentLibrary, parent: object = None) -> None:
        super().__init__(parent)  # type: ignore[arg-type]
        self._library = library

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
        name = item.data(0, Qt.ItemDataRole.UserRole)
        if name:
            self.component_selected.emit(name)
