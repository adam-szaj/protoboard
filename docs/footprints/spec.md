# Generic Parametric Footprints

Komponenty elektroniczne niezależnie od swoich cech elektrycznych mogą być
umieszczane w różnych obudowach. Niektóre typy obudów są ściśle określone w
taki sposób, że sama nazwa dokładnie opisuje wygląd i wymiary pakietu.
Przykładami takich obudów mogą być DIP-8, DIP-14, TO-92, TO-220, TO-247, DO-35,
DO-41. Część z tych pakietów można zdefiniować jako sparametryzowane wersje
bardziej generycznych kształtów, takich jak pryzma czy cylinder.

Sam kształt obudowy to tylko część zagadnienia, niezależnie od kształtu
dochodzi jeszcze temat pinów/wyprowadzeń komponentów. Jedne rodzaje obudów mają
sztywne wymiary (np obudowy typu DIP), inne dają więszą elastyczność gdyż mają
odkształcalne wyprowadzenia drutowe (np rezystory THT).

Mnogość kombinacji wynikającej ze zróżnicowania parametrów i zastosowań
komponentów elektronicznych powoduje, że zdefiniowanie użytecznej biblioteki w
połączeniu z zadowalającym UX stawia przed developerem sporo wyzwań.


## Rozwiązanie

Została podjęta decyzja, że biblioteka będzie definiowała pewną liczbę
"nazwanych" obudów, oraz zestaw obudów parametrycznych. Dzięki parametryzacji
biblioteku, użytkownik programu, będzie mógł dostosować bibliotekę komponentów
do tego, co akurat znajduje się w jego szufladzie. Dlaczego jest to takie
ważne? Otóż tworząc projekt prototypowy musimy wiedzieć przed rozpoczęciem
lutowania, że dana konfiguracja komponentów jest wykonalna, bo zmiana routingu
po tym jak część płytki jest zlutowana może okazać się niepotrzebnie
skomplikowane.

### Generyczny footprint

Obecnie obudowy z rodziny "DIP" oraz rodzina "TO" ma już swoje renderery,
dlatego nie będziemy opisywać ich w tym rozdziale.

Większość typowych obudów można wystarczająco dobrze opisać za pomocą
geometrycznych primitywów takich jak cylinder i prostopadłościan (pryzma).
Dlatego te bryły będą stanowić podstawę większości typowych footprintów.

Jednak największy problem jest z usystematyzowaniem opisu wyprowadzeń. Dlatego
ograniczymy definiownie struktur do komponentów dwu-pinowych, choć ich opis
powinno się dać rozszerzyć do dowolnej liczby wyprowadzeń.


Obudowy komponentów 2-pinowych możemy podzielić pod względem wyprowadzeń na
dwie grupy, osiowe(axial) i promieniowe(radial).

 - axial: wyprowadzenia są w tej samej osi lecz po przeciwnych stronach obudowy
 - radial: wyprowadzenia są po ten samej stronie obudowy. w szczególnym
   przypadku obudowy cylindrycznej wyprowadzenia mogą być prostopadłe do
   promieni podstawy cylindra i równoległe do normalnej do tej podstawy.

### Parametry

1. Najprościej sparametryzować bryłę obudowy. w przypadku pryzmy są to trzy
   wymiawy W x L x H, oraz opcjonalny promień zaokrąglenia krawędzi (0: default
   - bez zaokrąglenia). W przypadku cylindra wymiary to D x L (lub H) oraz
   opcjonalny promień zaokrąglenia przy widoku od strony ściany cylindra.


2. Wyprowadzenia natomiast mają kierunek, offset względem siebie oraz offset
   względem obudowy.

   - kierunek to umowny kierunek względem obudowy, jeśli przyjmiemy, że
     cylinder jest opisany w układzie współrzędnych (X, Y, Z) gdzie oś cylindra
     pokrywa się z osią Z, to kierunki wyprowadzeń dla kondensatora osiowego są
     odpowiednio z=[0,0,1] i -z=[0,0,-1], dla wyprowadzeń promieniowych oba
     wyprowadzenia będą miały ten sam kierunek np -z=[0,0,-1]

   - offset względem siebie to odległość pomiędzy osiami wyprowadzeń, typowo
     dla komponentów osiowych offset wynosi 0, dla komponentów promieniowych
     jest to raster/pitch (zwykle wielokrotność rastru), ale wyprowadzenia
     można też odginać. Dlatego offset komponentu nie musi pokrywać się z
     ułożeniem na płytce.

   - offset względem obudowy, to odległość geometrycznego środka pomiędzy
     wyprowadzeniami, od geometrycznego środka osi obudowy wzdłuż kierunku
     wyprowadzeń. Najczęściej będzie to default=0.

   Jeśli offset wyrażony scalarem jest niewystarczający, należy określić offset
   za pomocą wektora.


### Parametryzacja

Chcąc umieścić komponent w projekcie musimy określić dla niego obudowę. Obudowy
najczęściej będą pochodnymi obudów generycznych. Część z nich będzie w
bibliotece w pełni określonych np obudowa diody DO-35, ma znany rozmiar i układ
wyprowadzeń, definicja DO-35 będzie dziedziczyć generyczną obudowę cylindryczną
i określać brakujące parametry.
Inne obudowy w bibliotece będą wymagały podania brakujących wymaganych
parametrów przez użytkownika. Przykładowo wybierając kondensator
elektrolityczny ustalamy dla niego rodzaj wyprowadzeń, średnicę i wysokość
cylindra i offset/pitch jeśli potrzeba.

W szczególnych przypadkach, pitch będzie określony zakresem, w ramach którego
rozstaw pinów można swobodnie dostosowywać przed odgięcie wyprowadzeń. W takich
przypadkach względny offset dotyczy odległości w której wyprowadzenia wychodzą
z obudowy, a pitch jest odległością pomiedzy wyprowadzeniami na płytce.


Sparametryzowane komponenty zapiszemy w bibliotece użytkownika, by nie musiał
za każdym razem podawać wszystkich parametrów.


### Dwa offsety: body vs board

Axial i radial to nie dwa niezależne typy, tylko **nazwane szczególne
przypadki** jednej, ogólniejszej geometrii wyprowadzeń. Geometria jest opisana
**per pin**, a nie jedną etykietą. `kierunek` jest listą wektorów per pin:

 - axial:  `kierunek = [[0,0,-1], [0,0,1]]` — piny współosiowe, wychodzą w
   przeciwne strony.
 - radial: `kierunek = [[0,0,-1], [0,0,-1]]` — oba wychodzą w tę samą stronę.

Rozstaw wyprowadzeń występuje w **dwóch wariantach**, które trzeba rozróżniać:

 - **body_offset** — gdzie wyprowadzenia wychodzą *z bryły obudowy*. Cecha
   fizyczna producenta (np. TO-92: 1.25 mm; rezystor axial: 0). Sztywna.
 - **board_offset** — gdzie piny *lądują w otworach płytki*. To wynik montażu
   (ewentualnego odgięcia wyprowadzeń).

Oba mają **identyczną strukturę**: lista N−1 kolejnych wektorów (patrz niżej).
Dla wygody, gdy para jest jedna (komponent 2-pinowy), w YAML wolno podać
**skalar** `s` (rozwijany do `[[s, 0]]`) lub pojedynczy wektor `[x, y]`; loader
zawsze normalizuje do listy.

**Jeden wektor opisuje oba offsety.** Skoro `body_offset` i `board_offset` różni
**tylko dziedzina/jednostka** (obudowa vs siatka), a nie kształt, to ten **sam
wektor** zwykle służy jako oba — wystarczy podać **jeden** (`body_offset`), a
`board_offset: default` czyta ten sam `value` w dziedzinie siatki. TO-92:
`value [[1,0],[1,0]]` to 3-in-line zarówno na obudowie (×1.25 mm) jak i na
płytce (co 1 otwór). Renderer pokazuje rozejście wynikające z różnicy jednostek
(1.25 → 2.5).

**Drugi wektor podajemy tylko, gdy kształt ma się różnić** — nie gdy różni je
sama jednostka. Przykład: 3-in-line na obudowie, ale pin 2 odsunięty w bok na
płytce (montaż w trójkąt):

```yaml
body_offset:  [[1,0],[1,0]]      # 3-in-line na obudowie
board_offset: [[1,1],[1,-1]]     # pin 2 odsunięty — inny KSZTAŁT, nie jednostka
```

`board_offset` jest źródłem prawdy dla **domyślnej pozycji pinów na płytce**.
Pin 1 leży w lokalnym `[0,0]` *na płytce* (nie w punkcie wyjścia z obudowy), a
pozostałe piny wyznacza **lista kolejnych, sąsiednich wektorów** (od pinu *i* do
*i+1*); dla N pinów lista ma N−1 elementów, pozycje liczone kumulatywnie.
Wektory są 2D, więc ta sama struktura opisuje dowolną topologię — np. DIP-8:

```
board_offset = [[0,1],[0,1],[0,1],[3,0],[0,-1],[0,-1],[0,-1]]
```

(pin1→4 w dół lewej kolumny, przeskok na prawą, pin5→8 w górę).

**Domyślnie `board_offset` ≡ `body_offset`** tam, gdzie ma to sens:

 - **Sztywne wyprowadzenia (DIP):** odgięcie niemożliwe, więc board = body
   na stałe (renderer wie, że realne nóżki wychodzą z boków bryły — w modelu
   pozycja na bryle i na płytce może być ta sama).
 - **Odginalne wyprowadzenia (TO-92):** `body_offset` sztywny, ale
   `board_offset` wolno odróżnić (np. 1.25 → 2.5), zwykle w zadanym **zakresie**.
 - **Axial `body_offset == 0`:** board ≠ body z definicji (piny nie mogą trafić
   w ten sam otwór) — `board_offset` jest wtedy **liczony z geometrii** (patrz
   niżej), a nie kopiowany.


### Jednostki: dwie dziedziny (body vs board)

Liczby offsetów zapisujemy **bez jednostki w nazwie** (`offset`, nie
`offset_mm`). Dwa offsety żyją w **dwóch różnych dziedzinach**, każda ze swoją
jednostką:

 - **`board_offset` — dziedzina siatki.** Zawsze liczony w **całkowitych
   otworach** (jednostka = raster boardu = 2.5 mm, stała). **Nie ma `unit`** —
   `[1,0]` to z definicji jeden otwór. board_offset z natury żyje na siatce
   płytki, inna jednostka nie miałaby sensu.
 - **`body_offset` — dziedzina obudowy.** Deklaruje własny skalar **`unit`**
   (np. `1.25`, `2.5`); zapis `[1,0]` jest mnożony przez `unit` i w pamięci
   trzymany w mm. To naturalne kroki danej obudowy (TO-92: `unit: 1.25`),
   niezależne od rastra płytki.

**Wymiary bryły** (średnica, długość, wysokość) oraz `lead_length` zawsze
podajemy w **mm** — to dane z datasheetu, nie z rastra.

**Kanoniczny raster = 2.5 mm.** Wewnętrznie raster 0.1" / 2.54 mm reprezentujemy
jako **2.5 mm** (2.54 obcięte w dół). 2.5 mm i 0.1" traktujemy jako **ten sam
raster** — różnicę 0.04 mm pochłania luz między średnicą wyprowadzenia a
otworem. Dzięki temu nie ma dwóch metryk do uzgadniania: metryczne komponenty
(lead spacing 2.5) i imperialne płytki (otwory 2.54) leżą na jednej siatce.
Wszystkie wyliczenia rastrowe w tym dokumencie liczymy względem 2.5 mm.


### Wartości wymagane (`null`) i domykanie w hierarchii

Wartość `null` znaczy **„brak sensownego defaultu — użytkownik MUSI podać"**
(np. `body_offset` radialny zależy od konkretnego elementu z szuflady). To co
innego niż wartość 0.

Hierarchia `extends` **domyka** wartości monotonicznie: każdy poziom (generyk →
seria → komponent) może wypełnić `null` konkretną wartością, ale **raz domkniętej
wartości nie nadpisujemy**. Gdyby zachodziła taka potrzeba — to sygnał, że albo
hierarchia jest źle zaprojektowana, albo parametr zatrzaśnięto za wcześnie.
Wartość, która została `null` aż do instancjacji, jest wymuszana w GUI.

`lead_length` domyślnie jest `null` (wypełniane w GUI). Dla axial `body_offset
== 0` jest **wejściem solvera** (bez niego nie policzymy `board_offset`); dla
DIP/TO-92 to głównie informacja dla renderera.


### discrete-step — wprowadzanie offsetu w UI

W rzeczywistych komponentach `body_offset`/`board_offset` jest zwykle skwantowany
do rastru (typowo `N × 2.5 mm` — np. radialny elektrolit). W UI parametr offsetu
renderujemy więc jako **spinner z krokiem rastrowym**:

 - **Źródło rastra:** wartość `step` pojawia się w definicjach **bliższych
   konkretowi** (komponent lub seria), nie w generyku — bo zależy od rodziny.
 - **Snap miękki:** strzałki spinnera inkrementują o `step`, ale użytkownik może
   ręcznie wpisać dowolną wartość (nietypowy element).

To wyłącznie warstwa input/UX — model trzyma offset jako zwykłą wartość.
Mechanizm można pominąć w pierwszej iteracji.


### Solver: `board_offset` dla axial (`body_offset == 0`)

Dla komponentów osiowych z `body_offset == 0` (rezystor, dioda axial) rozstaw na
płytce nie ma jednej „prawdziwej" wartości — liczymy **minimalny rozstaw** z
geometrii. Parametry:

 - `L` — długość obudowy (oś wyprowadzeń),
 - `r_body` — promień cylindra *lub* połowa krótszej krawędzi ściany wyjścia
   (pryzma),
 - `lead_r` — promień drutu wyprowadzenia; minimalny promień gięcia = `3·lead_r`,
 - `offset_względem_obudowy` — mimośród wyjścia leada względem osi bryły
   (default 0; patrz pkt 2 wyżej).

Aproksymacja liniowa (`3·lead_r` dodawane jako zapas na zgięcie):

```
poziomo (DEFAULT):  board_offset_min = L + 6·lead_r
pionowo:            board_offset_min = r_body − offset_względem_obudowy + 3·lead_r
```

 - **Default = orientacja pozioma**, bo komponenty osiowe częściej montuje się
   na płask. Wybór nie ma twardych podstaw logicznych — jest rozsądną konwencją.
 - W poziomie `offset_względem_obudowy` jest pomijalny (wpływa minimalnie na
   *max* rozstaw, bez znaczenia praktycznego).
 - W pionie mimośród **zmniejsza** minimalny rozstaw: lead bliżej krawędzi
   bryły wymaga mniej odginania.

**Maksymalny rozstaw** liczymy z długości wyprowadzeń (oba leady rozprostowane):

```
board_offset_max = board_offset_min + 2·lead_length
```

Solver zwraca wartości w **mm (ciągłe)** — to promienie pierścienia
dopuszczalnego rozstawu. `Package` nie zna rastra konkretnej płytki.
**Orientacja** (poziomo/pionowo) jest **stanem instancji/placement**, nie cechą
package: solver liczy oba zakresy, a który jest aktywny zależy od tego, jak
użytkownik ułożył dany egzemplarz.

**Co znaczy „snap do rastra” — pierścień, nie oś.** Rozróżniamy dwie rzeczy:

 - **Pozycje pinów są dyskretne** — każdy pin THT musi wpaść w fizyczny otwór,
   więc przesunięcie `(dX, dY)` pinu względem pinu 1 to zawsze **całkowite
   rastry**. To jedyny sens „snapu”: trafienie w otwór, **nie** zaokrąglenie
   kierunku do osi/przekątnej.
 - **Zakres rozstawu jest ciągłym ograniczeniem** — dozwolone jest **każde**
   całkowite `(dX, dY)` spełniające (metryka euklidesowa):

   ```
   r_min ≤ √(dX² + dY²) ≤ r_max
   ```

   Pin 2 może więc wpaść w **dowolny otwór w pierścieniu** `[r_min, r_max]` wokół
   pinu 1 — pod dowolnym kątem (np. `[5,2]`), nie tylko ortho `[N,0]` czy diag
   `[N,N]`. Fizycznie odpowiada to odgięciu drutu pod realnym kątem. (To
   uogólnia dzisiejszy skalarny `min_reach`/`max_reach`, który zakładał oś.)

### Przykład przepracowany: rezystor axial (DIN 0207)

Dane: `L = 6.3`, `D = 2.5` (→ `r_body = 1.25`), `lead_r = 0.27`,
`body_eccentricity = 0`, raster 2.5 mm (kanoniczny). Typowy rezystor THT ma
długość całkowitą (koniec-do-końca) ≈ 70 mm, więc
`lead_length ≈ (70 − 6.3)/2 ≈ 32 mm`.

```
min poziomo (default) = L + 6·lead_r          = 6.3 + 1.62 = 7.9 mm  → 4 rastry (/2.5)
min pionowo           = r_body + 3·lead_r      = 1.25 + 0.81 = 2.06  → 1 raster
max                   = 7.9 + 2·32             = 71.9 mm             → ~29 rastrów
```

Porównanie z dzisiejszym ręcznym wpisem `R` (`min_reach: 1`, `default y: 4`,
`max_reach: 23`): solver **odtwarza** min (1) i default (4) z czystej geometrii,
a max (~28) jest w okolicy 23 — różnica wynika tylko z przyjętej `lead_length`.
Model odtwarza ręczne wartości z fizyki bryły i długości wyprowadzeń.

> `lead_length` NIE ma twardego defaultu w modelu — w danych jest `null`
> (required). Reguła „1.5 × wymiar obudowy” to wyłącznie **wartość startowa
> pola w GUI** (sensowne, niezerowe minimum, by spinner nie był pusty), którą
> użytkownik nadpisuje realną długością z datasheetu/szuflady. Dla axial THT
> będzie zwykle nadpisana mocno w górę (~32 mm vs startowe 9.45) — to oczekiwane,
> placeholder nie udaje fizyki.

YAML:

```yaml
packages:
  - name: R_Axial_DIN0207
    extends: cylinder_axial
    description: "Axial resistor body, DIN 0207 (~1/4 W)"
    dims: {diameter: 2.5, length: 6.3}
    leads: {lead_r: 0.27, lead_length: 32.0}   # board_offset = solve

components:
  - name: R
    component_class: resistor
    package: {extends: R_Axial_DIN0207}
    pin_names: {}
    aliases: [R_Axial_DIN0207, R_0603_1608Metric, ...]
```


### Odginalność (`bend`) — luz pozycji pinu dla geometrii generycznej

Gdy geometrii nie umiemy rozwiązać analitycznie (cokolwiek poza axial
`body_offset == 0`), dopuszczalny luz pozycji pinu podajemy **jawnie** zamiast
liczyć. Odginalność to wartość w **otworach** (dziedzina board) = **długość
wektora przesunięcia** pinu względem jego domyślnej pozycji `board_offset`.
Innymi słowy pin może wpaść w dowolny otwór w **kole o promieniu = odginalność**
wokół defaultu (luz izotropowy, 2D — nie tylko wzdłuż jednej osi).

Jedna wartość **per package** — odginalność jest cechą typu wyprowadzeń obudowy
(jak sztywne są nóżki), więc dotyczy całego komponentu jednakowo, nie
poszczególnych pinów. (Pin 1 pozostaje kotwicą wyznaczającą origin `[0,0]`.)

To pojęcie jest **analogiczne**, ale **nie tożsame** z zakresem liczonym przez
solver axial: solver daje **asymetryczny zakres wzdłuż osi** (default = `min`,
`max` znacznie dalej), a odginalność daje **symetryczny promień** wokół defaultu.
Unifikacja obu (jeden mechanizm „dopuszczalnego luzu") jest do rozważenia — na
razie pozostają osobne.

**Dolny clamp.** `bend` jest symetryczny (promień ±r), ale fizyka dokłada dolne
ograniczenie: `board_offset` nie może zejść **poniżej `body_offset`** — leady
nie skrzyżują się ciaśniej niż wychodzą z obudowy. Efektywny zakres to więc

```
[ body_offset , body_offset + bend ]
```

co naturalnie modeluje „radial można rozgiąć szerzej, ale nie ścisnąć węziej"
bez wprowadzania osobnego pola na asymetrię.

### Przykład przepracowany: elektrolit radialny (6.3×11)

Dane: `D = 6.3`, `H (length) = 11.0`, radialny lead spacing 2.5 mm (datasheet),
leady proste w dół.

```yaml
packages:
  - name: CP_Elec_6.3x11
    extends: cylinder_radial          # board_offset:default
    description: "Radial electrolytic capacitor, 6.3×11 mm"
    dims: {diameter: 6.3, length: 11.0}
    leads:
      body_offset: {unit: 2.5, value: [[1,0]]}   # 2.5 mm = 1 otwór
    bend: 4                            # rozgięcie do +4 otworów

components:
  - name: C_POL
    component_class: capacitor_polar
    package: {extends: CP_Elec_6.3x11, step: 1}
    pin_names: {1: "+", 2: "-"}
    aliases: [CP_Elec_5x11, CP_Elec_8x11]
```

Porównanie z dzisiejszym `C_POL` (`default y: 1`, zakres 1..5):

 - default = `body_offset` = 1 otwór ✓ (`board_offset: default`).
 - zakres: dolny clamp = `body_offset` (1), górny = `body_offset + bend` =
   1 + 4 = 5 ✓. `bend` to promień euklidesowy, więc rozgięcie też daje pierścień
   (pin 2 w dowolnym otworze `[1, 5]` pod dowolnym kątem, nie tylko prosto).

Elektrolit, w przeciwieństwie do rezystora, **nie** uruchamia solvera
(`body_offset ≠ 0`, `board_offset: default`), za to korzysta z `unit`/`step`
(discrete-step) i `bend`. Druga połowa modelu — przechodzi.

### Przykład przepracowany: TO-92 (3 piny, jeden wektor, dwie dziedziny)

TO-92 to test graniczny: **jedyny dotąd komponent, gdzie ten sam wektor offsetu
daje różny rozstaw na obudowie i na płytce — bo różni go jednostka, nie
kształt**. Wyprowadzenia są 3-in-line w rastrze obudowy **1.25 mm**, a na płytce
ten sam 3-in-line ląduje w otworach co **2.5 mm**.

```yaml
packages:
  - name: TO-92
    extends: prism
    description: "Plastic transistor, 3-lead inline"
    dims: {width: 4.5, length: 4.5, height: 5.2}
    leads:
      direction:   [[0,0,-1],[0,0,-1],[0,0,-1]]   # radial, 3 piny
      body_offset: {unit: 1.25, value: [[1,0],[1,0]]}   # 3-in-line
      board_offset: default                          # ten sam wektor, dziedzina siatki
    bend: 3.54        # √2 · 2.5 — minimalny sensowny: pin sięga otworu po skosie
```

Analiza:

 - **body_offset** = `[[1,0],[1,0]]` × 1.25 = piny na obudowie co 1.25 mm
   (inline).
 - **board_offset: default** = **ten sam wektor** `[[1,0],[1,0]]` czytany w
   dziedzinie siatki = inline co **1 otwór = 2.5 mm**. Nie podajemy drugiego
   wektora, bo kształt jest ten sam (3-in-line) — różni go tylko jednostka.
   Renderer pokazuje rozejście 1.25 → 2.5.
 - **bend = √2 · 2.5 ≈ 3.54** — minimalny *sensowny* promień: to **przekątna
   komórki rastra**, więc każdy pin sięga dowolnego z 8 sąsiednich otworów (w
   tym **diagonalnych**). Mniej dopuszczałoby tylko 4 ortho. Pozwala to złożyć
   TO-92 w **trójkąt** (środkowy pin po skosie do przodu/tyłu — typowy montaż na
   perfboardzie) i swobodnie odginać skrajne, **bez** sztywnej enumeracji
   3 stanów per pin (dzisiejszy model TO-92 — odrzucony, bo nie pozwalał na
   swobodne odginanie).

Gdyby trójkąt miał być **domyślnym kształtem** (nie tylko opcją przez `bend`),
podalibyśmy drugi wektor jawnie: `board_offset: [[1,1],[1,-1]]` (pin 2 odsunięty
w bok). Tu nie trzeba — kształt board = kształt body.

TO-92 testuje: `body_offset` jako prawdziwą **listę** (>2 piny), jeden wektor w
dwóch dziedzinach (jednostka body ≠ jednostka board), oraz `bend` jako
euklidesowy pierścień
swobody. Trzeci komponent — model się nie wywraca.

To TO-92 wymusił rozdział jednostek (sekcja „Jednostki”): `body_offset` ma
własny `unit` (1.25, dziedzina obudowy), a `board_offset` zawsze liczy w
otworach (raster 2.5). Dwie dziedziny, dwie jednostki — bez konfliktu.


## Składnia YAML

### Lokalizacja i ładowanie

Definicje package żyją w plikach `*.yaml` ładowanych tym samym mechanizmem co
komponenty: katalog builtin (`assets/packages/`) + katalogi użytkownika z
`component_library_dirs` (wcześniejsze wygrywają). Package jest **następcą**
dzisiejszego `assets/body_types/` (`BodyTypeSpec`) — docelowo migrujemy istniejące
body_types do tego formatu, a stary loader usuwamy. Sparametryzowane przez
użytkownika warianty zapisują się w jego bibliotece, by nie podawać parametrów
za każdym razem.

### Pola package

| pole | znaczenie |
|---|---|
| `name` | identyfikator package |
| `abstract: true` | baza, nie do bezpośredniego użycia (np. `cylinder`) |
| `extends:` | nazwa package, po którym dziedziczymy i domykamy parametry |
| `shape:` | `cylinder` \| `prism` |
| `dims:` | wymiary bryły w **mm** (`diameter`/`length`/`edge_radius` lub `width`/`length`/`height`/`corner_radius`) |
| `leads:` | geometria wyprowadzeń (niżej) |
| `bend:` | odginalność — jeden promień luzu **w otworach** (raster boardu), per package |

Blok `leads`:

| pole | dziedzina / jednostka | znaczenie |
|---|---|---|
| `direction` | — | lista wektorów 3D per pin (axial `[[0,0,-1],[0,0,1]]`, radial `[[0,0,-1],[0,0,-1]]`) |
| `body_offset` | obudowa, własny `unit` | lista N−1 wektorów (sugar: skalar/wektor dla 1 pary). Forma: `{unit: U, value: [...]}` lub goła lista (wtedy `unit` z domyślnego pola) |
| `board_offset` | siatka, **otwory** (stały raster 2.5) | lista wektorów (inny **kształt** niż body) **lub** `default` (ten sam wektor co body, czytany w dziedzinie siatki) **lub** `solve` (solver axial) |
| `lead_length` | mm | `null` = wypełnia GUI; wejście solvera dla axial |
| `lead_r` | mm | promień drutu (solver) |
| `body_eccentricity` | mm | mimośród wyjścia leada (= „offset względem obudowy”), default 0 |

Dwie dziedziny: **body** (obudowa, `unit` deklarowany przy `body_offset`) vs
**board** (siatka, jednostka stała = raster/otwór, bez `unit`). `bend` żyje w
dziedzinie board → liczony w otworach.

`null` = wartość wymagana (domyka potomek lub GUI); domykanie monotoniczne (bez
nadpisywania domkniętych wartości).

### Generyki (abstract, builtin)

```yaml
packages:
  - name: cylinder
    abstract: true
    shape: cylinder
    dims: {diameter: null, length: null, edge_radius: 0.0}

  - name: cylinder_axial            # rezystor, dioda osiowa
    extends: cylinder
    leads:
      direction:   [[0,0,-1], [0,0,1]]
      body_offset: 0                # → [[0,0]]
      board_offset: solve           # body=0 → solver
      lead_length: null             # mm, GUI/solver
      lead_r:      null             # mm
      body_eccentricity: 0.0

  - name: cylinder_radial           # elektrolit
    extends: cylinder
    leads:
      direction:    [[0,0,-1], [0,0,-1]]
      body_offset:  null            # required (z własnym unit)
      board_offset: default         # := body_offset zrzutowany na siatkę
      lead_length:  null
    bend: 0.0                        # w otworach
```

### Named (domykają generyk)

```yaml
  - name: DO-35
    extends: cylinder_axial
    description: "Glass axial diode body"
    dims: {diameter: 2.0, length: 4.0}
    leads: {lead_r: 0.27}           # board_offset dalej `solve`

  - name: TO-92
    extends: prism
    description: "Plastic transistor, 3-lead inline"
    dims: {width: 4.5, length: 4.5, height: 5.2}
    leads:
      direction:   [[0,0,-1],[0,0,-1],[0,0,-1]]
      body_offset:  {unit: 1.25, value: [[1,0],[1,0]]}  # 1.25 mm między sąsiadami
      board_offset: default                              # → projekcja na siatkę 2.5
    bend: 3.54                        # √2 otwora — pin sięga sąsiada po skosie
```

(pełna analiza TO-92 — patrz „Przykład przepracowany: TO-92" wyżej)

### Kompozycja w komponencie (HAS-A)

Komponent **wskazuje** package i domyka brakujące parametry. Package wyznacza
**liczbę i geometrię** pinów (przez długość list w `leads`); `pin_names` należą
do **komponentu** (nazwy zależą od konkretnego elementu, nie od obudowy). `step`
(discrete-step spinnera) deklarujemy tu, bliżej konkretu.

```yaml
components:
  - name: C_POL
    component_class: capacitor_polar
    package:
      extends: cylinder_radial
      dims: {diameter: 6.3, length: 11.0}
      leads:
        body_offset: {unit: 2.5, value: [[1,0]]}   # 1 × 2.5 = 2.5 mm
      bend: 4
      step: 1
    pin_names: {1: "+", 2: "-"}
```


## Architektura: `generic_package` + interfejs `Package`

> Kierunek architektoniczny — szkic, nie zamrożone API. Sygnatury doprecyzujemy
> przy implementacji pierwszych komponentów.

### Wspólny korzeń

Wszystkie package wywodzą z jednego korzenia **`generic_package`**, który
**wymusza obecność wymaganych pól** w definicjach pochodnych (`name`,
`description`, `dims`, `leads`, …). Definicja, która ich nie domknie (zostawia
`null` aż do instancjacji), jest odrzucana przy ładowaniu — to egzekucja
zasady „`null` = required" w jednym punkcie walidacji.

### Geometrię liczy kod, nie YAML

`generic_package` definiuje (w Pythonie) **interfejs** zwracający geometrię
wyprowadzoną z danych:

```python
class Package(Protocol):
    def pin_positions(self) -> list[Point]: ...     # board_offset rozwinięty
    def collision_map(self) -> CollisionShape: ...  # bryła zrzutowana na siatkę
    def bbox(self) -> Rect: ...
    def pin_count(self) -> int: ...
```

`Cylinder` i `Prism` **implementują** ten interfejs, licząc wynik z `dims` +
`leads` (m.in. solver axial `board_offset` to ciało `Cylinder.pin_positions()`
w wariancie osiowym). YAML pozostaje czystym **opisem danych**; cała geometria
żyje w implementacji.

### Dwie niezależne osie „dziedziczenia"

Trzeba je rozdzielać, by hierarchia nie była krucha:

 - **dane** — YAML `extends:` scala parametry (DO-35 domyka `cylinder_axial`).
   To merge słowników.
 - **zachowanie** — pole `shape:` wybiera, *która klasa* (`Cylinder`/`Prism`)
   ożywi te dane. To **dispatch po `shape`** (registry
   `{"cylinder": Cylinder, …}`), **nie** subclassing łańcucha `extends`.

Dodanie pośredniego named-package (kolejne ogniwo `extends`) nie może zmieniać
tego, która implementacja obsłuży geometrię — decyduje wyłącznie `shape`.











