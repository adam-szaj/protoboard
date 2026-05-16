# DRC — Design Rule Check

```python
class DRCEngine:
    def check(self, project: Project) -> list[DRCIssue]: ...

class DRCIssue:
    severity: Literal["error", "warning", "info"]
    code: str
    message: str
    locations: list[RasterPosition]
    affected_nets: list[UUID]
```

## Sprawdzenia

| Kod | Severity | Opis |
|---|---|---|
| `MISSING_NET` | error | Sieć ze schematu nie jest w pełni zrealizowana fizycznie |
| `SHORT` | error | Pady różnych sieci elektrycznie połączone |
| `OVERLAP` | error | Dwa komponenty zajmują ten sam pad |
| `FLOATING_PIN` | warning | Pin komponentu nie należy do żadnej sieci |
| `BARE_WIRE_CROSS` | error | Dwa nieizolowane przewody różnych sieci się krzyżują |

## Optymalizacja

`DRCEngine.check_with_graph()` przyjmuje gotowy `ConnectivityGraph` zamiast budować własny. `session.connectivity_graph()` zwraca cached graf — DRC po raz drugi (bez zmian w layoucie) kosztuje ~1.5 ms zamiast ~7.8 ms. Szczegóły: [performance.md](performance.md).

Puste węzły stripów (bez przypisanych pinów) są ignorowane przez DRC — nie generują `FLOATING_PIN`.
