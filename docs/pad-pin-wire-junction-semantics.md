# Semantyka Wire, Pin, Joint oraz Pad

Wiele akcji automatycznych i manualnych wymaga określenia twardych warunków
oraz preferencji przy układaniu komponentów lub routingu przewodów. Częstym
problemem przy refaktoringu kodu lub dodawaniu nowych funkcjonalności jest
zła interpretacja co jest dozwolone, co nie a co jedynie nie jest preferowane.

Ten dokument jest jedynym źródłem prawdy dla zasad pad/pin/wire/joint.
Definiuje konwencje, twarde zasady (0–9), zasady miękkie, wire-pin, pad
modifiers (kapton tape) oraz cross-check tabelę implementacji per callsite.

## Konwencje

- **Net** = `schematic.Net` (logiczna sieć ze schematu, identyfikowana przez
  `Net.id`). Wire bez resolved net (oba endpoints off-pin, brak pinu na
  żadnym vertex) traktujemy jako **orphan-net** — nie jest częścią żadnej
  `Net`, ale **nie jest też nową siecią**. Nowo układany wire startuje jako
  orphan-net dopóki jego endpoint nie trafi na pin/joint/inną sieć (wtedy
  dziedziczy net z tego mate'a). Łączenie orphan-net z named net w jednym
  solder node = łańcuch potencjalnego short, traktujemy jak konflikt jeśli
  orphan jest już mate'em innej named net.

- **Solder node** = pin komponentu LUB pad-joint (same-side LUB cross-side =
  via) LUB pad z bare-wire vertex (endpoint lub middle-point) na stronie
  metalizowanej. Każdy fizyczny węzeł lutowniczy.

  Bare wire na padzie metalizowanym po stronie prowadzenia jest **zawsze**
  połączony elektrycznie z padem (niezależnie czy endpoint czy middle).
  Różnica między middle-point a endpoint:
  - **middle-point**: brak dodatkowej penetracji; połączenie tylko przez
    fabryczną metalizację otworu (jeśli pad ma `plated_through=True`,
    łączy obie strony intrinsically).
  - **endpoint**: dodaje własną penetrację (lutowanie); galwanizuje obie
    strony nawet gdy otwór nie jest fabrycznie metalizowany.

- **Strona uzbrojona** padu = strona, dla której `PadLayers` ma `True`.
  `plated_through=True` oznacza że obie strony są galwanicznie połączone
  fabrycznie (bez via).

- **Strona pinu**: piny (regular pin komponentu i wire-pin) **nie mają
  strony**. Logicznie pin jest tak samo po jednej jak i po drugiej stronie.
  Komponent jest położony po jednej stronie, ale wszystkie jego
  solder-pady (pin-joint, t-joint, wire-pin) są obecne po obu stronach
  płytki.

## Definicje terminów

### Solder node — podtypy
- **Pin** — pin komponentu (PinKnot). Pozycja wynikająca z placement.
- **Pad-joint same-side** — wszystkie incident wires na jednej stronie
  (JointKnot, position = pad).
- **Via** — szczególny pad-joint, wires po obu stronach. Pin też tworzy
  via efektywnie (zasada 3 — galwaniczne połączenie obu stron przez
  penetrację fizyczną).
- **Bare-wire vertex** — endpoint LUB middle-point bare wire'a na padzie
  metalizowanym po jego stronie (patrz "Konwencje / Solder node" powyżej).

Pin, pad-joint i via mają tę samą semantykę net-conflict: każdy incident
wire/pin musi być net-compatible. Bare-wire vertex tworzy elektryczny
kontakt bez tworzenia "joint" w sensie strukturalnym (joint to mutacja
modelu, vertex to lokalizacja na padzie).

### Net compatibility (rozszerzenie zasady 0)
Dwa elementy (wire/pin/joint) są **net-compatible** jeśli:
- mają ten sam Net.id, LUB
- któryś jest orphan-net a drugi ma named net (orphan dziedziczy net
  drugiego), LUB
- oba są orphan-net (luźny chain bez named net na żadnym końcu).

**Conflict** = oba mają named net i `net_a.id != net_b.id`.

---

## Twarde zasady

### Zasada 0 — Brak short
Dwie różne sieci nie mogą tworzyć połączenia elektrycznego. To metareguła
nad zasadami 1–9 — każda z nich jest jej konkretyzacją.

### Zasada 1 — Foreign wire przez pin
Przez pin komponentu nie może przebiegać wire innej sieci. Patrz zasada 6
dla pełnej formalizacji (każdy vertex wire'a, nie tylko endpoint).

### Zasada 2 — Pad-joint nie akceptuje obcych sieci
Istniejący pad-joint należy traktować jak pin komponentu. Dwie szczegółowe
reguły:

**2a.** Pad-joint nie może być utworzony na padzie na którym jest cokolwiek
należące do innej sieci. Przykład: na padzie X, po stronie bottom przebiega
wire `W_a` sieci `Net_a`, po stronie top wire `W_b` sieci `Net_b`. Taka
sytuacja jest legalna, ale na tym padzie nie można dołączyć trzeciego
wire'a tworzącego pad-joint (jakikolwiek dodatkowy mate forsuje wybór
"której sieci pad należy", a żadna odpowiedź nie jest poprawna).

**Wyjątek 2a.1** — podmiana fragmentu wire'a. Jeśli endpoint wymienianego
fragmentu leży na padzie X i transient joint zostanie zredukowany
atomowo w tej samej mutacji (`make_t_junction` → split + re-anchor
jednoczesny, degree padu po operacji ≤ 1), operacja jest legalna.
Callsite musi zagwarantować redukcję.

**2b.** Nie można utworzyć joint'u na padzie na którym jest wire innej
sieci (degenerat 2a — bez dodatkowych mate'ów).

### Zasada 3 — Via i pin galwanizują obie strony
Via to szczególny przypadek pad-joint ze zmianą stron płytki. Via oraz pin
komponentu tworzy połączenie galwaniczne po obu stronach płytki, nawet gdy
płytka jest jednostronna lub gdy pad nie jest `plated_through`.

### Zasada 4 — Bare wire i plated-through
Bare wire tworzy połączenie galwaniczne od strony, po której jest położony,
o ile pad jest metalizowany po tej stronie (zasada "Solder node"). Jeśli
otwór padu jest galwanizowany (`plated_through=True`), połączenie
elektryczne pojawia się również po drugiej stronie.

Na padzie z bare wire można:
- stworzyć t-joint tej samej sieci,
- położyć pin komponentu tej samej sieci,
- położyć izolowany przewód:
  - **tej samej sieci** → tworzy pad-joint (+via jeśli strony różne),
  - **obcej sieci** → dozwolone tylko gdy nie zachodzą inne ograniczenia
    (nie ma via, nie ma pad-joint, izolacja izoluje wire od pada).

### Zasada 5 — Galwaniczna penetracja przez pin
Pin komponentu **zawsze** tworzy galwaniczne połączenie obu stron padu,
niezależnie od `PadLayers.plated_through`. Implementacja: oceniając "co
jest na padzie po stronie X", musisz uwzględnić piny **bez względu na
placement.side**, gdy pad jest pod pinem.

To rozszerzenie zasady 3, wyróżnione jako osobna zasada bo:
- via jest **opcjonalna** (tworzona przez user/autorouter intentionally),
- pin **zawsze** istnieje gdy komponent jest placed.

Pin penetracja jest niezależna od bare/insulated wire — pin galwanizuje
pad z samej obecności.

### Zasada 6 — Foreign wire/pin: brak prowadzenia drutu przez pin
**Przez pad komponentu (regular pin i wire-pin) nie może przebiegać żaden
wire** — niezależnie od izolacji, niezależnie czy to endpoint czy interior
vertex, niezależnie od sieci.

Rationale (świadomie wykraczamy poza czystą zasadę elektryczną zero):
- **fizyczne**: prowadzenie drutu nad pinem jest trudne lub niemożliwe
  (pin sterczy ponad pad, izolacja go nie omija),
- **estetyczne i utrzymaniowe**: obwód z drutami nad pinami trudniej
  analizować i serwisować,
- **elektryczne** (jako wycinek zasady 0): bare wire obcej sieci nad
  pinem to natychmiastowy short.

Konsekwencja: zasada nie ma wyjątku "insulated interior vertex może
przechodzić nad pinem". Każdy vertex (endpoint, middle-point) każdego
wire'a (bare, insulated) jest zakazany na padzie pinu, z jedynym
wyjątkiem: wire którego endpoint jest **tym pinem** (legal solder).

Ta sama reguła dotyczy wire-pinów. Wire-pin w praktyce to przylutowany do
pada kawałek przewodu — wszystko nad nim ma te same problemy fizyczne i
estetyczne co regular pin.

### Zasada 7 — Pad-joint exclusivity per sieć
Pad-joint (każdy podtyp, w tym via) jest **exclusive dla jednej named
net** raz utworzony. Po utworzeniu, nowy incident wire musi być
net-compatible z istniejącym joint'em.

Zasada 2 mówi to dla **tworzenia** joint'u. Tu dopisujemy że
**istniejący** joint też się broni — nie można dorzucić foreign wire do
działającego joint'u (tym bardziej via).

Wyjątek z zasady 2a (podmiana fragmentu): legalna pod warunkiem że joint
**redukuje się natychmiast** — tj. po zakończeniu operacji pad nie jest
joint'em (degree → 0 lub 1). Callsite musi zagwarantować atomową redukcję
transient joint'u w tej samej mutacji.

### Zasada 8 — Pin komponentu jako odbiorca incoming wire
Pin komponentu (analogicznie do joint'u w zasadzie 7) **nie akceptuje
foreign wire** jako incoming endpoint.

Obecnie `_find_joint_at` ma net gate (po 4 rundach t-junction).
**`_find_pin_at` nie ma odpowiednika** — implementacja zakłada że jeśli
endpoint wire trafia na pin, to wire jest "tej samej sieci" (lub orphan
przyjmie net pina). Ta zasada wymaga jawnego gate:

```
incoming_wire endpoint na pin P:
- net(incoming) == net(P) → SAFE
- net(incoming) == None, net(P) != None → ASSIGN (incoming dziedziczy net pina)
- net(incoming) != None, net(P) == None → ASSIGN (pin dziedziczy net wire'a)
- net(incoming) != net(P) (oba named) → REJECT (foreign wire na pin)
```

To bezpośredni pierwowzor `can_form_t_junction` ale dla pin endpoints,
nie joint interiors.

### Zasada 9 — Half-orphan wire (warning, nie error)
Wire którego **jeden lub oba endpointy nie mają solder mate** (pin, joint,
ani drugi wire endpoint w tym samym solder node) to **half-orphan**.

DRC zgłasza **warning**, nie error. User decyduje:
- usunąć (wire jest do wycięcia),
- poprowadzić dalej (dodać wire kontynuujący),
- zagospodarować jako wire-pin (virtual goldpin — patrz definicja niżej).

Endpoint który jest solo na padzie traktujemy jako **prospective via**:
gdyby ktoś coś tam dołączył w przyszłości, byłby to via. Do tego momentu
electric net = net wire'a (lokalnie), bez galwanicznego rozszerzania na
drugą stronę.

Half-orphan wire często powstaje przy zmianie pozycji komponentów —
tworzą się konflikty z pinami, albo wire podłączony do przenoszonego
komponentu nie spełnia warunków geometrycznych i zostaje usunięty.
**Autorouter musi traktować half-orphan wire jako kandydata na destynację**
dla sieci, którą trzeba uzupełnić (nie tylko prospective via — także
prospective target dla nowo routowanego segmentu tej samej sieci).

---

## Zasady miękkie

Sytuacje legalne, których należy unikać. Część tych zasad jest dozwolona
bez restrykcji dla jednego toola, a dla innego jest nielegalna.

**Przykład**: crossing przewodów izolowanych (same-side lub opposite-side)
na padzie. Dla autoroutingu jest możliwa droga do prowadzenia przewodu,
limitowana wyższym kosztem ale nie zabroniona. Dla toola "simplify wire
path" (opcja do zaznaczenia) takie przecięcie powinno mieć mocniejsze
ograniczenie — przy simplify staramy się uprościć zarówno ścieżkę jak i
proces lutowania. Jeśli użytkownik chce crossing na padzie, może wybrać
opcję (o ile nie zachodzą zasady twarde).

Crossing off-pad jest traktowany neutralnie o ile jest po przeciwnych
stronach płytki.

Pad z kapton tape akceptuje insulated crossings bez restrykcji (drut nie
dotyka pada fizycznie, tape go izoluje) — patrz "Pad modifiers" niżej.

---

## Wire-pin (virtualny goldpin)

**Wire-pin** to virtualny komponent jednopinowy reprezentujący endpoint
wire'a jako stały solder point. Logiczny odpowiednik fizycznego
pinheadera 1x01 (goldpin). Fizycznie to po prostu kawałek przewodu
przylutowany do pada.

### Use case
- Definiowanie eksportowanych pinów grupy bez konieczności dodawania
  fizycznego goldpina.
- Zamiana half-orphan wire endpointu na deliberowany terminal.
- Wire-pin staje się anchorem netu lokalnie — wire ma terminal, joint
  jest formalny.

### Semantyka
Wire-pin ma:
- pozycję (przypisaną do padu),
- net (dziedziczona z wire'a),
- **NIE ma strony** — analogicznie do regular pinu, wire-pin jest
  obecny po obu stronach płytki.

Galwanizuje pad jak pin komponentu (zasada 5). Reguła 6 stosuje się
identycznie jak dla regular pinu — żaden wire (poza tym, którego
endpoint *jest* tym wire-pinem) nie może przechodzić nad padem wire-pina.

### Lifetime
Wire-pin jest **ściśle związany z wire'em**:
- wire znika → wire-pin znika,
- wire przenosi endpoint w inne miejsce → wire-pin znika z poprzedniej
  pozycji.

Wire-pin nie jest niezależnym obiektem projektowym jak ComponentInstance.

### Dyspozycja w modelu
Dwie opcje:
1. Nowy typ obok `PinKnot`/`JointKnot`: `WirePinKnot(id, position, net_id)`
   — własne dane, nie odsyła do żadnego `ComponentInstance`.
2. Zachować `ComponentInstance` jako "virtual instance"
   (`definition_ref="_virtual_wire_pin"`) — reusuje istniejące code paths
   (DRC, persistence, schematic), ale lifetime zarządzany przez wire,
   nie jak normalny komponent.

Wizualizacja: pomalowanie pada na kolor sieci (bez body komponentu).

Decyzja do podjęcia podczas projektowania warstwy Pad/PadLayer — patrz
"Otwarte pytania".

---

## Pad modifiers (kapton tape)

Kapton tape (lub inna izolacja nakładana ręcznie) zmienia per-pad
charakterystykę elektryczną. Tape może być nałożona na top, bottom, lub
obie strony.

**Tape logicznie tworzy kolejną warstwę.** Nałożona na "top" tworzy
warstwę "top+1", analogiczną do górnej warstwy płytki jednostronnej.
Konsekwencja: przewody można prowadzić na top, bottom, top+1. Taśmy można
nakładać warstwami, więc teoretycznie liczba warstw może być dowolna
(top+2, top+3, …).

### Semantyka

**Tape całkowicie izoluje pad od bare wire** po stronie, na której jest
nałożona. To dotyczy także padów z `plated_through=True` — tape blokuje
bare wire mimo plated-through hole.

**Wyjątki przebijające tape** (penetracja fizyczna w hole):
- pin komponentu,
- via,
- wire-pin.

Te elementy galwanizują pad mimo tape (pin wbity w hole przebija
metalicznie tape over hole).

**Pad-joint (t-joint) nad tape** (zasada 7 + modifier):
- **Default**: dozwolony (tworzy strukturalny węzeł na warstwie
  routingu nad tape; nie tworzy elektrycznego połączenia z padem
  *poniżej* tape ani z drugą stroną — tape izoluje).
- **Per-project knob `strict_tape_joints`**: gdy włączony, t-joint nad
  tape = error (konserwatywne).
- Subtelność: t-joint nad tape to jedyny przypadek, gdzie joint nie
  tworzy połączenia elektrycznego z padem ani nie blokuje drugiej
  strony. Default ALLOW odzwierciedla intencję taśmy (izolować jedną
  stronę od drugiej), ale wymaga uwagi w analizie elektrycznej.

Tape jest **per-side modifier** dla bare-wire i (opcjonalnie) joint
creation. Nie wpływa na piny komponentów, via ani wire-piny, ponieważ te
przebijają tape fizycznie.

### Edge cases

**Bare wire na padzie z tape (z lub bez `plated_through`)**:
- bare wire jest **całkowicie izolowany** od pada,
- pad zachowuje się dla tego wire'a jak zwykła off-pad pozycja,
- jeśli na drugiej stronie tego samego padu jest bare wire bez tape,
  tamten wire normalnie tworzy contact (po swojej stronie); tape z
  jednej strony nie wpływa na drugą stronę poza tym, że nie ma
  galwanicznego mostka przez tape.

**Tape na obu stronach + `plated_through=True`**:
- pad jest sealed dla bare wires z obu stron,
- pin/via/wire-pin nadal galwanizuje (penetracja przez tape w hole),
- to legalny workflow: użytkownik chce zachować dostęp pinów, ale
  zablokować swobodny routing bare-wires nad padem.

**Tape blokuje crossing semantykę miękką**: insulated crossing na
padzie z tape jest neutralny (tape eliminuje fizyczny kontakt z padem;
crossing redukuje się do off-pad crossing).

### Wpływ na DRC
- `_check_short` musi rozumieć że bare wire na pad-z-tape nie tworzy
  galvanic contact na tej stronie.
- `_check_bare_wire_interior_cross` — tape nad bare wire crossing
  eliminuje short, redukuje do "preferable to avoid" (zasada miękka).
- Nowy check `KAPTON_REDUNDANT` (info, nie error): tape na padzie który
  i tak nie ma wires/pinów do izolowania — wskazówka dla użytkownika.

### Wpływ na autoroute
- A* cost model uwzględnia kapton:
  - bare wire przechodzący przez pad-z-tape na bieżącej stronie =
    obniżona penalty (nie blokuje już nic; tape izoluje od pada),
  - po przeciwnej stronie tape nadal pozwala kłaść bare wires bez
    short check.
- Nowy knob `kapton_bare_freedom` w cost model (default neutral; user
  może preferować lub unikać tape).

### Wpływ na simplify
Pad z tape akceptuje insulated crossings bez restrykcji (drut nie dotyka
pada fizycznie, tape go izoluje). To rozluźnia regułę miękką dla
crossings izolowanych na padzie.

### Dyspozycja w modelu

```python
@dataclass(frozen=True)
class PadModifiers:
    kapton_top: bool = False
    kapton_bottom: bool = False
    # przyszłe modyfikatory: solder_mask, etc.

# Layout.pad_modifiers: dict[Point, PadModifiers]
```

Storage w `Layout`, nie w `Board` (board reprezentuje fabryczną płytkę;
tape jest dodawana przez usera per projekt).

Wieloletnia akumulacja warstw tape (top+1, top+2, …) wymaga rozszerzenia
modelu (lista warstw zamiast bool top/bottom). MVP: single-layer tape per
side; multi-layer to follow-up.

### Persistence
W `layout.yaml`:
```yaml
pad_modifiers:
  - position: {x: 4.5, y: 3.5}
    kapton_top: true
  - position: {x: 5.5, y: 3.5}
    kapton_top: true
    kapton_bottom: true
```

Empty list = brak tape (nie zapisujemy).

### GUI tooling
Dwa nowe trybki w toolbar:
- **Add kapton tape** — kliknięcie na padzie aplikuje tape na bieżącą
  warstwę widoku (top/bottom).
- **Remove kapton tape** — odwrotnie.

Rubber-band selection padów dla bulk apply — analogicznie do bulk
component select.

Wizualizacja:
- Pad z kapton na top → renderowany z lekko żółtą poświatą po stronie
  top (przezroczystą, żeby pad nadal był widoczny pod spodem).
- Pad z kapton on bottom → analogicznie po stronie bottom.
- Pad z kapton na obie strony → poświata po widzianej stronie zawsze.

---

## Architektura warstwy Pad/PadLayer (szkic do rozwinięcia)

Cel: algorytmy (autoroute, DRC, simplify, drag-conflict) konsumują
precomputed dane przez wąski interface zamiast inspect-and-decide na
hot path. Logika reguł delegowana do `Pad` → `PadLayer` →
wstrzykiwane strategie.

Wymagania:
1. **N warstw routingu** — board (top, bottom) + dynamiczne warstwy
   kapton (top+1, top+2, …, bottom+1, …). `PadLayer` świadomy czy jest
   izolowany od warstw poniżej/powyżej.
2. **Pin bez strony** — `pin` jest własnością `Pad` (nie `PadLayer`);
   galwanizuje wszystkie warstwy które fizycznie przebija (zazwyczaj
   wszystkie, ale tape between layers może to subtelnie modyfikować w
   przyszłych use-case'ach).
3. **Bare middle-point vs endpoint** — `add_wire` musi rozróżniać
   (model penetracji jest różny). Wymaga rozszerzenia `WireRef` lub
   osobnych metod (`add_bare_endpoint`, `add_bare_middle`).
4. **Strategy injection** — `Pad`/`PadLayer` tylko store'ują; decyzje
   reguł (np. "default ALLOW vs strict tape t-joint") podejmują
   wstrzykiwane `RoutingStrategy`/`DRCStrategy` konsumujące query API.
5. **Cache invalidation** — `get_electric_net`/`get_any_net` jako
   precomputed; każda mutacja (`add_wire`, `set_pin`, `apply_tape`)
   musi invalidate. Versioning / dirty flag / event hooks.

Szkic wstępny istnieje w historii dokumentu (Pad/PadLayer interfejsy);
finalna wersja zostanie zaprojektowana po wypełnieniu cross-check
tabeli i identyfikacji wszystkich call-sites z konkretnymi
requirementami. Implementacja w osobnym dokumencie architektury.

---

## Cross-check checklist

Dla każdego callsite (autoroute cost, DRC checks, simplify,
drag-conflict, T-junction gates, joint reuse, pin reuse) sprawdzić
zgodność z regułami 5–9 i kaptonem.

Statusy: **zgodny** = reguła egzekwowana; **niepełny** = częściowo
(jakiś case nie pokryty); **brakuje** = reguła w ogóle nie egzekwowana
przez ten callsite; **n/a** = reguła z założenia nie dotyczy.

Audit z 2026-05-31, branch `main` po commitach `1b971f3` (docs merge)
i `0065500` (T-junction net gates + nowe DRC checks).

| Callsite | Reguła 5 (pin penetracja) | Reguła 6 (foreign wire/pin) | Reguła 7 (joint exclusivity) | Reguła 8 (pin gating) | Reguła 9 (half-orphan) | Pad modifiers (kapton) |
|---|---|---|---|---|---|---|
| `_check_short` (drc/engine.py:298) | zgodny — operuje na `graph.node_for_pin`, plated-through zaimplikowane przez electric-set merge | n/a — bridge przez pin/foreign wire wykryje jako short tej samej drogi | n/a — joint exclusivity gated wcześniej w `_find_joint_at`/`can_form_t_junction`; short tu to konsekwencja, nie creation | n/a — pin gating to `_find_pin_at` | n/a | brakuje — graph builder ignoruje kapton (nie istnieje w modelu) |
| `_check_bare_wire_interior_cross` (drc/engine.py:465) | n/a — sprawdza segmenty między padami, pin penetracja nie dotyczy | niepełny — wykrywa cross między różnymi electric-set, ale nie reguły 6 (drut nad pinem) | n/a | n/a | n/a | brakuje |
| `_check_bare_wire_cross` (drc/engine.py:385) | zgodny — używa `graph.node_for_pin` (plated-through już zmergowany) i rejestruje obie strony per pad | brakuje — wykrywa tylko bare↔bare, nie bare/insulated nad pinem | n/a | n/a | n/a | brakuje |
| `_check_off_pad_joint` (drc/engine.py:613) | n/a | n/a | niepełny — sprawdza tylko geometrię (joint na padzie), nie egzekwuje exclusivity zawartości | n/a | brakuje — half-orphan joint na plated_through niesygnalizowany | brakuje — `board_pads_for_side` nie zna kaptonu |
| `_check_off_board_pins` (drc/engine.py:660) | zgodny — pin musi być na padzie; plated_through respected przez `board_pads_for_side` | n/a | n/a | n/a | n/a | brakuje |
| `can_form_t_junction` (wire_utils.py:736) | zgodny — `_wire_net_id` respectuje plated_through dla pin-resolution | niepełny — CONFLICT_REJECT dla różnych netów, ale nie sprawdza czy wire przechodzi przez pad pinu (reguła 6 strict) | zgodny — SAFE/CONFLICT_REJECT egzekwuje exclusivity przy tworzeniu | brakuje — `ASSIGN_NET_TO_INCOMING_PIN` zakłada że pin akceptuje wire, brak własnej walidacji can_place_pin | n/a | brakuje |
| `_find_joint_at` (component_cmds.py:259) | zgodny — `plated_pads` check dla cross-side joints | niepełny — odrzuca foreign-net incoming i orphan-incoming gdy joint named, ale nie sprawdza reguły 6 dla wire over-pin | zgodny — post-creation gate (nowa runda) | n/a — pin nie joint | n/a | brakuje |
| `_find_pin_at` (component_cmds.py:230) | n/a — sama lokalizacja pinu | brakuje — wie tylko że "tu jest pin", nie weryfikuje czy incoming wire jest legalny | n/a | brakuje — brak `can_place_pin` net gate (reguła 8 explicitly missing per memory) | n/a | brakuje |
| `astar.cost` (astar.py:404-548) | zgodny — `_collect_plated_through_pads` rules vias only on plated; pin penetracja przez `valid_nodes` setup | niepełny — `wire_over_foreign_wire_pad`/`occupied_by_other_net` to soft cost, nie hard reject; reguła 6 dla pinów milcząco przez `foreign_wire_cells` | niepełny — `via_on_foreign_pad` jako soft cost; joint creation gated przez post-routing `split_wires_at_endpoint_collisions` | brakuje — autoroute zakłada same-net target, nie ma `can_place_pin` check | n/a — autoroute nie konsumuje half-orphan jako destynacji (zasada 9 follow-up) | brakuje |
| `autorouter._check_preconditions` (autorouter.py:486+) | zgodny — `OFF_BOARD_PIN` per pin na właściwej stronie | brakuje | brakuje — `_AUTOROUTE_BLOCKING_DRC_CODES = {SHORT}` blocks only on existing shorts | brakuje | brakuje — nie wykrywa half-orphan jako legal target | brakuje |
| `_check_floating_pins` (drc/engine.py:359) | n/a | n/a | n/a | n/a | brakuje — koncept "floating pin" ≠ half-orphan wire; reguła 9 dotyczy wire'ów bez solder mate | n/a |

## Acceptance criteria dla migracji

Tabela pokazuje że **rzeczywista lista zmian semantycznych** to:

- **Reguła 6 strict (bez wyjątku insulated interior)**: brakuje wszędzie poza T-junction creation gate. Wymaga: dodać check "wire interior over pin/wire-pin pad" do `_check_bare_wire_cross` LUB nowy check `WIRE_OVER_PIN`.
- **Reguła 8 (`can_place_pin` net gate)**: brakuje w `_find_pin_at`, `can_form_t_junction` (`ASSIGN_NET_TO_INCOMING_PIN` path), `astar.cost`. Wymaga: nowa metoda strategii `can_place_pin(pad, our_net) -> Decision` + integracja.
- **Reguła 9 (half-orphan)**: brakuje całkowicie jako koncept. `_check_floating_pins` to coś innego (nieplaced pin). Wymaga: nowy DRC check `HALF_ORPHAN_WIRE` (warning) + autorouter używa half-orphan endpoint jako candidate destination.
- **Kapton (`Pad modifiers`)**: brakuje wszędzie — out of scope MVP (follow-up).

Reguły 5 i 7 są generalnie egzekwowane, choć rozsiane po wielu callsites — refactor przeniesie je za jedną strategię (`DRCEnforcingStrategy`).

**Po wszystkich Step (1–7)** ponownie audit każdej komórki — wszystkie poza kapton-kolumny powinny być `zgodny`. Pozostałe `brakuje`/`niepełny` to TODO follow-up.

---

## Otwarte pytania

1. **Wire-pin jako nowy `Knot` subtype czy virtual `ComponentInstance`?**
   Pierwsze jest czystsze koncepcyjnie, drugie reusuje istniejące code
   paths (DRC, persistence, schematic). Wpływ na exported_pins group
   flow.

2. **Net inheritance dla orphan-orphan join.** Gdy dwa orphan-net wires
   spotykają się w pad-joint i żadne nie ma named net — czy chain
   zostaje orphan, czy dostaje synthetic net id?

3. **Tape persistence i undo.** Add/remove tape jako Command'y w
   command stack — czy potrzebują specjalnego handling przy save?

4. **DRC visualization dla half-orphan.** Warning marker na endpoint
   wire'a — jaka ikona, jaki kolor? Czy klik na warning otwiera
   dialog "co zrobić z tym wire'em" (delete / extend / convert to
   wire-pin)?

5. **Tape może być nakładana tylko na pady czy też off-pad?**
   Praktycznie tape jest fizycznie nakładana na cały obszar — może
   wpłynąć na off-pad wires. Modelowanie per-pad jest prostsze. Czy to
   wystarcza?

6. **Multi-layer tape MVP scope.** Czy MVP wspiera tylko single tape
   per side (top+1, bottom+1) czy od razu lista warstw? Jeśli MVP
   single, jak żeby model nie wymagał breaking change przy
   rozszerzeniu?

7. **Strict tape t-joint knob** — global per project (`Layout.settings`)
   czy per pad (override)? Default jest ALLOW; gdzie żyje override?
