# SPDX-License-Identifier: MIT
"""Library panel — browse and select components for placement."""

from __future__ import annotations

from PySide6.QtCore import Qt, Signal
from PySide6.QtWidgets import (
    QLineEdit,
    QTreeWidget,
    QTreeWidgetItem,
    QVBoxLayout,
    QWidget,
)

from protoboard.domain.components.definition import ComponentClass
from protoboard.services.library.loader import ComponentLibrary

# Human-readable labels for each component class, in display order.
_CLASS_LABELS: dict[ComponentClass, str] = {
    ComponentClass.RESISTOR: "Resistors",
    ComponentClass.CAPACITOR_POLAR: "Capacitors (polar)",
    ComponentClass.CAPACITOR_NONPOLAR: "Capacitors (non-polar)",
    ComponentClass.INDUCTOR: "Inductors",
    ComponentClass.TRANSFORMER: "Transformers",
    ComponentClass.FUSE: "Fuses",
    ComponentClass.SWITCH: "Switches",
    ComponentClass.DIODE: "Diodes",
    ComponentClass.DIODE_ZENER: "Diodes (Zener)",
    ComponentClass.DIODE_LED: "LEDs",
    ComponentClass.TRANSISTOR_NPN: "Transistors — NPN",
    ComponentClass.TRANSISTOR_PNP: "Transistors — PNP",
    ComponentClass.TRANSISTOR_NMOS: "Transistors — N-MOSFET",
    ComponentClass.TRANSISTOR_PMOS: "Transistors — P-MOSFET",
    ComponentClass.TRANSISTOR_NJFET: "Transistors — N-JFET",
    ComponentClass.TRANSISTOR_PJFET: "Transistors — P-JFET",
    ComponentClass.IC: "Integrated Circuits",
    ComponentClass.CONNECTOR: "Connectors",
    ComponentClass.CRYSTAL: "Crystals & Oscillators",
    ComponentClass.MECHANICAL: "Mechanical",
    ComponentClass.OTHER: "Other",
}


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
        self._tree.setIndentation(12)
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

        # Group components by class, preserving _CLASS_LABELS order.
        groups: dict[ComponentClass, list[str]] = {c: [] for c in _CLASS_LABELS}
        for name in sorted(self._library.names()):
            if q and q not in name.lower():
                # Also search description.
                try:
                    desc = self._library.get(name).description.lower()
                except KeyError:
                    desc = ""
                if q not in desc:
                    continue
            try:
                cls = self._library.get(name).component_class
            except KeyError:
                cls = ComponentClass.OTHER
            groups.setdefault(cls, []).append(name)

        for cls, label in _CLASS_LABELS.items():
            names = groups.get(cls, [])
            if not names:
                continue
            parent_item = QTreeWidgetItem(self._tree, [f"{label}  ({len(names)})"])
            parent_item.setFlags(
                parent_item.flags() & ~Qt.ItemFlag.ItemIsSelectable
            )
            for name in names:
                try:
                    defn = self._library.get(name)
                    desc = defn.description
                    tooltip = f"{name}  —  {desc}" if desc else name
                except KeyError:
                    tooltip = name
                child = QTreeWidgetItem(parent_item, [name])
                child.setToolTip(0, tooltip)
                child.setData(0, Qt.ItemDataRole.UserRole, name)

        # Auto-expand when searching.
        if q:
            self._tree.expandAll()
        else:
            self._tree.collapseAll()

    # ------------------------------------------------------------------

    def _on_search(self, text: str) -> None:
        self._populate(text)

    def _on_double_click(self, item: QTreeWidgetItem, _column: int) -> None:
        name = item.data(0, Qt.ItemDataRole.UserRole)
        if name:
            self.component_selected.emit(name)
