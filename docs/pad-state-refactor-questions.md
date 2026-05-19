# Pytania projektowe: refaktor pad-state-aware algorithms

Kontekst: zamiast każdy algorytm (simplify, autoroute, DRC, drag-conflict)
ma własną wersję "czy ten pad jest zajęty", wprowadzić jedno źródło
prawdy o stanie pada. Algorytmy odpytują state pada i podejmują
decyzje na bazie tej informacji, zamiast każdy rekonstruować odpowiedź
z layout + schematic.

Definicje (z Twojego sformułowania):

- **Pad zajęty fizycznie** (per-side):
  - prowadzony jest przez niego przewód izolowany (endpoint lub mid-pass),
  - body komponentu jest na nim, pod którym nie można przeprowadzić
    przewodu — strona płytki ma znaczenie.
- **Pad zajęty elektrycznie**:
  - pin komponentu jest na nim,
  - nieizolowany (bare) wire przechodzi przez niego (vertex lub mid-pass).

---
## 1. Gdzie umieścić stan pad'a?

Indeksacja padów. 

Na początek należy rozważyć sposób indeksacji padów. pady niezależnie od pitch
są ułożone na siatce ortogonalnej i mogą być reprezentowane przez pary liczb
całkowitych, co pozwala na szybki dostęp do informacji powiązanych z padem. Z
jednej strony mogą być to rzeczywiste pozycje w mm lub w mils. A z drugiej
strony może dostarczać takich informacji jak galwanizacja przelotu, zajętość
fizyczna i elektryczna. Sama struktura padu jest ściśle powiązana z typem
płytki i instancje Padów powinny być fabrykowane przez instancję płytki.
instancja padu może mieć inny typ lub jedynie inną strukturę wewnętrzną
odzwierciedlającą właściwości fizyczne pada.

Czyli położenie przewodu izolowanego wpłynie jedynie na zajętość fizyczną po
jednej stronie. natomiast położenie bare wire wpłynie na zajętość fizyczną po
jednej a elektryczną, a zajętość elektryczna po żadnej (gdy płytki są
jednostronne) a przewód kładziemy na "nieuzbrojonej stronei", po jednej jeśli
położony jest na uzbrojonej stronie płytki ale otwory nie są przewodzące i po
obu jeśli są przewodzące.

Przejście przewodu na drugą stronę oraz umieszczenie pinu powoduje zajętość
elektryczną i fizyczną z obu stron.

taka struktura jest niezależna od grafiki a nawet od grafu, jest tylko
kontenerem do przechowywania informacji o padach. Może być wykorzystana zarówno
przez algorytmy w domain jak i rendering. I nie widzę potrzeby przebudowywania
całej struktury przy każdej zmianie.

---

## 2. Co dokładnie powinien zwracać `pad_state(pos, side)`?

**Opcja A — Bogaty PadState dataclass (Recommended)**

```python
@dataclass()
class PadState(ABC):
    # table below
    electrical_net: tuple[NetId | None, NetId | None] # (top, bottom):
    physically_blocked: tuple[list[Item], list[Iten]] # item can be a wire, pin, component. In case of isolated wire there can be more than one.

    # @abstractmethod
    # def ...

# different implementation depending on board type (?)
class SingleSidedBoardPadState(PadState): ...
class IsolatedDoubleSidedBoardPadState(PadState): ...
class ConnectedDoubleSidedBoardPadState(PadState): ...
```




Electrical Net
```
                                                   |onesided                    |double-sided-isolated|double-sided-connected
---                                                |                            |                     |
EMPTY                                              |                            |                     |
top                                                |None                        |None                 |None
bottom                                             |None                        |None                 |None
---                                                |                            |                     |
ISOLATED WIRE ON TOP                               |                            |                     |
top                                                |None                        |None                 |None
bottom                                             |None                        |None                 |None
---                                                |                            |                     |
ISOLATED WIRE ON BOTTOM                            |                            |                     |
top                                                |None                        |None                 |None
bottom                                             |None                        |None                 |None
---                                                |                            |                     |
BARE WIRE ON TOP                                   |                            |                     |
top                                                |same as wire                |same as wire         |same as wire
bottom                                             |None                        |None                 |same as wire
---                                                |                            |                     |
BARE WIRE ON BOTTOM                                |                            |                     |
top                                                |None                        |None                 |same as wire
bottom                                             |same as wire                |same as wire         |same as wire
---                                                |                            |                     |
WIRE SIDE CHANGE OF BARE OR ISOLATED, COMPONENT PIN|                            |                     |
top                                                |same as wire as wire or pin*|same as wire or pin* |same as wire or pin*
bottom                                             |same as wire as wire or pin |same as wire or pin  |same as wire or pin
---                                                |                            |                     |
```

*) all three cases requires at least partialy exposed conductor so we have to
assume that pad is occupied electricaly even on onesided board.

Plus: pełna informacja. Algorytmy formułują decyzje typu "pad zajęty
moim netem? OK, kontynuuj", "pad zajęty bare drutem obcego netu?
short, odrzuć", "pad zajęty body komponentu nad tą stroną? autoroute
może pójść po drugiej stronie".

Minus: więcej pól = więcej do utrzymania w spójności. Czy
`blocker_id` jest stabilny przez Undo/Redo? 


## 3. Kiedy aktualizować PadOccupancy?

**Opcja A — Inkrementalnie przy każdej mutacji (Recommended)**

---

## 4. Zakres tej zmiany w jednej iteracji?

**Opcja B — Pełne wdrożenie + migracja wszystkich callers**

PadOccupancy + simplify + migracja autoroute/drag-conflict/DRC +
testy. Wspólny commit zostawia codebase spójny.

Plus: jednorazowa zmiana, codebase czysty.

Minus: duży PR. Łatwo coś przeoczyć. Trudniej zrobić rollback gdy
któryś caller okaże się subtelnie inny.

---
## Pomocnicze rozważania
