# SPDX-License-Identifier: MIT
"""SZKIC interfejsu Package — NIE jest to produkcyjny moduł.

Szkic architektoniczny do `docs/footprints/spec.md` (sekcja „Architektura:
generic_package + interfejs Package"). Sygnatury są kierunkiem, nie zamrożonym
API — doprecyzujemy przy pierwszej implementacji. Plik mieszka w docs/, nie w
src/, dopóki nie zacznie być realnie używany.

Wykorzystuje istniejące typy projektu: geometry.Point / Rect / Size / Insets.

Stan decyzji (2026-06-14):
  [OK]   Leads to warstwa rozpakowana przez loader — Package NIE zna składni
         YAML (3 formy direction, run-length offsetów, {unit,value} normalizuje
         loader do prostych list mm/otwory).
  [OK]   Dwie dziedziny zostają: pin_positions w OTWORACH (siatka),
         board_offset_range w MM (fizyka). Konwersję robi konsument.
  [OPEN] CollisionShape: Rect+rounded (obecnie) czy poligon / maska otworów?
         Do przemyślenia.
"""

from __future__ import annotations

from dataclasses import dataclass
from enum import StrEnum
from typing import Protocol, runtime_checkable

from protoboard.geometry import Insets, Point, Rect


# --------------------------------------------------------------------------- #
# Wartości pomocnicze
# --------------------------------------------------------------------------- #

# Kanoniczny raster (zob. spec: 2.54 obcinane do 2.5). board_offset liczy w
# otworach; mm = otwory * RASTER_MM.
RASTER_MM = 2.5


class MountOrientation(StrEnum):
    """Orientacja montażu — STAN INSTANCJI, nie cecha package.

    Dla axial body=0 solver zwraca inny zakres board_offset per orientacja.
    """

    HORIZONTAL = "horizontal"
    VERTICAL = "vertical"


@dataclass(frozen=True, slots=True)
class OffsetRange:
    """Dopuszczalny rozstaw jako pierścień euklidesowy [r_min, r_max] (mm).

    Pin może wpaść w dowolny otwór (całkowite dX,dY) taki, że
    r_min <= sqrt(dX^2 + dY^2) <= r_max. „Snap” = trafienie w otwór, NIE
    zaokrąglenie kierunku (zob. spec).
    """

    r_min_mm: float
    r_max_mm: float


@dataclass(frozen=True, slots=True)
class CollisionShape:
    """Bryła obudowy zrzutowana na płaszczyznę płytki (model kolizji).

    [OPEN] Na razie prostokąt + zaokrąglenie. Czy wystarczy (cylinder ≈ obrys
    prostokątny), czy potrzebny poligon / maska otworów dla dokładnego routingu
    — DO PRZEMYŚLENIA. To, czego routing/DRC używa do „czy tu jest obudowa”.
    """

    footprint: Rect          # bounding na siatce (otwory)
    rounded_mm: float = 0.0  # promień zaokrąglenia (cylinder edge / corner)


# --------------------------------------------------------------------------- #
# Interfejs
# --------------------------------------------------------------------------- #

@runtime_checkable
class Package(Protocol):
    """Geometria komponentu wyprowadzona z danych package.

    Strukturalny protokół — implementuje go `Cylinder`/`Prism` (z `dims`+`leads`)
    ORAZ docelowo `Group` (zob. notka kierunkowa „Group implements Package”),
    dzięki czemu routing/DRC/drag nie rozróżniają komponent vs grupa.

    Świadomie `Protocol`, nie ABC: „Group IMPLEMENTUJE Package” (fasada nad
    składowymi grupy) to nie to samo co „Group JEST Package”. Grupa nic nie
    dziedziczy — po prostu wystawia te metody (pin_positions = exported pins,
    collision_map = suma dzieci, bbox = group bbox). Protokół wyraża relację
    strukturalną, nie podtypowanie.

    Wybór implementacji: dispatch po polu `shape` (registry), NIE subclassing
    łańcucha `extends` (zob. spec — dwie osie dziedziczenia).
    """

    def pin_count(self) -> int: ...

    def pin_positions(
        self,
        *,
        orientation: MountOrientation = MountOrientation.HORIZONTAL,
    ) -> list[Point]:
        """Pozycje pinów NA PŁYTCE (otwory), pin 1 w [0,0].

        Rozwija board_offset (kumulatywnie). Dla axial body=0 zależy od
        `orientation` (solver liczy inny rozstaw poziomo/pionowo).
        """
        ...

    def board_offset_range(
        self,
        *,
        orientation: MountOrientation = MountOrientation.HORIZONTAL,
    ) -> OffsetRange:
        """Dopuszczalny pierścień rozstawu pinów (mm).

        - axial body=0  → liczony solverem (geometria + lead_length),
        - inne (bend)   → [body_offset, body_offset + bend] (dolny clamp),
        - rigid (DIP)   → r_min == r_max (bend=0, zero luzu).
        """
        ...

    def collision_map(self) -> CollisionShape:
        """Bryła zrzutowana na siatkę — model kolizji dla routing/DRC."""
        ...

    def bbox(self) -> Rect:
        """Bounding box obrysu PCB/obudowy na siatce."""
        ...


# --------------------------------------------------------------------------- #
# Implementacje (szkic ciał — nie pełne)
# --------------------------------------------------------------------------- #

@dataclass(frozen=True, slots=True)
class Leads:
    """Rozpakowana geometria wyprowadzeń (po normalizacji z YAML)."""

    direction: list[tuple[float, float, float]]   # per pin (3 formy → lista)
    body_offset_mm: list[Point]                   # N-1 wektorów, już ×unit→mm
    board_offset: list[Point] | str               # listy LUB "default"/"solve"
    lead_length_mm: float | None
    lead_r_mm: float | None
    body_eccentricity_mm: float = 0.0


@dataclass(frozen=True, slots=True)
class Cylinder:
    """shape: cylinder. Implementuje Package."""

    diameter_mm: float
    length_mm: float
    leads: Leads
    bend_holes: float = 0.0
    edge_radius_mm: float = 0.0

    @property
    def r_body_mm(self) -> float:
        return self.diameter_mm / 2.0

    def board_offset_range(
        self, *, orientation: MountOrientation = MountOrientation.HORIZONTAL
    ) -> OffsetRange:
        if self.leads.board_offset == "solve":          # axial body=0
            lead = self.leads.lead_length_mm or 0.0
            lr = self.leads.lead_r_mm or 0.0
            if orientation is MountOrientation.HORIZONTAL:
                r_min = self.length_mm + 6.0 * lr
            else:  # VERTICAL
                r_min = self.r_body_mm - self.leads.body_eccentricity_mm + 3.0 * lr
            return OffsetRange(r_min, r_min + 2.0 * lead)
        # radial / bend → dolny clamp na body_offset (szkic, 1-para)
        base = abs(self.leads.body_offset_mm[0].x) if self.leads.body_offset_mm else 0.0
        return OffsetRange(base, base + self.bend_holes * RASTER_MM)

    def pin_positions(
        self, *, orientation: MountOrientation = MountOrientation.HORIZONTAL
    ) -> list[Point]:
        # rozwiń board_offset (default→body w dziedzinie siatki, solve→range)
        raise NotImplementedError

    def pin_count(self) -> int:
        return len(self.leads.direction)

    def collision_map(self) -> CollisionShape:
        raise NotImplementedError

    def bbox(self) -> Rect:
        raise NotImplementedError


@dataclass(frozen=True, slots=True)
class Prism:
    """shape: prism. DIP, TO-92, devboard (z opcjonalnym margin)."""

    width_mm: float | None      # None → liczone z bbox(piny) + margin
    length_mm: float | None
    height_mm: float | None
    leads: Leads
    bend_holes: float = 0.0
    corner_radius_mm: float = 0.0
    margin_mm: Insets | None = None   # devboard: dims z pinów + margin

    def bbox(self) -> Rect:
        if self.margin_mm is not None and self.width_mm is None:
            return self._bbox_from_pins(self.margin_mm)
        raise NotImplementedError   # z jawnych dims

    def _bbox_from_pins(self, margin: Insets) -> Rect:
        # bbox(pin_positions) [otwory→mm] rozszerzony o margin (mm)
        raise NotImplementedError

    def pin_count(self) -> int:
        raise NotImplementedError

    def pin_positions(
        self, *, orientation: MountOrientation = MountOrientation.HORIZONTAL
    ) -> list[Point]:
        raise NotImplementedError

    def board_offset_range(
        self, *, orientation: MountOrientation = MountOrientation.HORIZONTAL
    ) -> OffsetRange:
        raise NotImplementedError

    def collision_map(self) -> CollisionShape:
        raise NotImplementedError


# --------------------------------------------------------------------------- #
# Dispatch po shape (registry) — NIE subclassing extends
# --------------------------------------------------------------------------- #

# Loader: scala dane przez `extends` (merge słowników), potem patrzy na `shape`
# i instancjuje właściwą klasę. Dodanie pośredniego named-package nie zmienia,
# która implementacja ożywia geometrię — decyduje wyłącznie `shape`.
SHAPE_REGISTRY: dict[str, type] = {
    "cylinder": Cylinder,
    "prism": Prism,
}
