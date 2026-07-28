# SPDX-License-Identifier: MIT
"""SZKIC interfejsu Package — NIE jest to produkcyjny moduł.

Architecture sketch for `docs/footprints/package-model.md` §7 ("The Package
interface"). The signatures were a direction, not a frozen API. NOTE: the
interface is now implemented for real in
`src/protoboard/domain/components/package/shapes.py` (Package/Cylinder/Prism/
Disc) and `mask.py`; this file is a superseded historical sketch.

Wykorzystuje istniejące typy projektu: geometry.Point / Rect / Size / Insets.

Stan decyzji (2026-06-14):
  [OK]   Leads to warstwa rozpakowana przez loader — Package NIE zna składni
         YAML (3 formy direction, run-length offsetów, {unit,value} normalizuje
         loader do prostych list mm/otwory).
  [OK]   Dwie dziedziny zostają: pin_positions w OTWORACH (siatka),
         board_offset_range w MM (fizyka). Konwersję robi konsument.
  [OK]   CollisionShape = body_polygon (mm, body-body collision) + pad_mask
         (otwory → PadCell bity side-aware piny/body). Kolizja = AND masek ≠ 0.
         Body blokujące „zapala” bity stron pinów → solderable-under = brak
         zapalenia (nie osobny bit). Wire-collision analogicznie (kierunek).
"""

from __future__ import annotations

from abc import ABC
from dataclasses import dataclass
from enum import IntFlag, StrEnum
from typing import Protocol, runtime_checkable

from protoboard.geometry import Insets, Point, Rect, Vector

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


class PadCell(IntFlag):
    """Side-aware bity stanu pojedynczego otworu (dziedzina siatki).

    Kolizja = bitowy AND masek dwóch komponentów per otwór; wynik ≠ 0 →
    konflikt. Bity są dobrane tak, by ten AND wystarczał — kluczowy trik:
    body, które BLOKUJE montaż pod sobą, „zapala” bity STRON PINÓW w swoich
    otworach, więc kolizja „niski komponent pod blokującym body” wychodzi z
    tego samego AND, bez osobnego bitu/logiki zezwalającej.

    Piny THT istnieją po OBU stronach (plated-through), stąd bity stron.
    """

    PIN        = 1 << 0   # jest pad/pin (kolizja pin-pin: potem sprawdź sieć)
    PIN_TOP    = 1 << 1   # pin jest / chce być montowany od strony top
    PIN_BOTTOM = 1 << 2   # pin jest / chce być montowany od strony bottom
    BODY_TOP   = 1 << 3   # korpus montowany od strony top
    BODY_BOTTOM = 1 << 4  # korpus od strony bottom

    # UWAGA: nie ma bitu „solderable under”. Body z wysokim standoffem (da się
    # pod nim lutować) ustawia TYLKO BODY_*. Body blokujące (metal, ściśle
    # przylegające) DODATKOWO zapala PIN_TOP/PIN_BOTTOM w swoich otworach (jedną
    # lub obie strony) — to jest cały mechanizm. „Solderable” = BRAK zapalenia,
    # nie osobny bit. Decyzja dzieje się przy BUDOWIE maski, nie przy AND.

    # Kolizja body↔WIRE = „czy da się przeprowadzić drut pod/przy body”. Po
    # PRZECIWNEJ stronie niż body — prawie zawsze tak; po TEJ SAMEJ — zależy od
    # standoff. NIE budujemy tu nowego modelu: wire-wire już działa na PadIndex
    # (side-aware, can_route_through). Bity BODY_* (+ decyzja standoff) zasilają
    # BLOKADY w PadIndex dla prowadzenia wire — router pyta PadIndex jak dotąd.
    # collision_map/pad_mask jest ŹRÓDŁEM tych blokad, nie równoległym modelem.


def has_collision(a: PadCell, b: PadCell) -> bool:
    """Kolizja dwóch otworów = niezerowy bitowy AND. Pin-pin (bit PIN) jest
    sygnałem 'sprawdź sieć', nie zawsze twardym błędem."""
    return bool(a & b)


@dataclass(frozen=True, slots=True)
class CollisionShape:
    """Model kolizji komponentu — DWIE odrębne rzeczy, dwie dziedziny.

    1. `body_polygon` (mm, ciągła geometria) — obrys korpusu do kolizji
       BODY↔BODY (czy dwie obudowy fizycznie nachodzą). Jeden polygon (kształt);
       z której strony jest body kodują bity BODY_* w masce. Lista Point od razu
       (nawet gdy teraz prostokąt — TO-220 z radiatorem, moduł L-kształtny nie
       wymuszą zmiany typu).

    2. `pad_mask` (otwory, dziedzina siatki) — per-otwór `PadCell` bity:
       side-aware piny + body. Kolizja = AND masek (zob. PadCell). To, czego
       routing/DRC używa do „co jest w tym otworze i z której strony”.

    Lutowalność pod body wynika z CECHY body (standoff): body blokujące zapala
    bity stron pinów, body z wysokim standoffem nie — różnica przy budowie maski,
    nie osobny bit (generic_package → maskę liczy kod).
    """

    body_polygon: list[Point]            # obrys korpusu (mm)
    pad_mask: dict[Point, PadCell]       # otwór → bity (rzadka mapa)
    rounded_mm: float = 0.0              # zaokrąglenie obrysu (cylinder/corner)


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
    body_offset_mm: list[Vector]                   # N-1 wektorów, już ×unit→mm
    board_offset: list[Vector] | str               # listy LUB "default"/"solve"
    lead_length_mm: float | None
    lead_r_mm: float | None
    body_eccentricity_mm: float = 0.0

@dataclass(slots=True)
class Body(ABC):
    position: Point = Point()
    pass

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
        base = abs(self.leads.body_offset_mm[0].dx) if self.leads.body_offset_mm else 0.0
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
