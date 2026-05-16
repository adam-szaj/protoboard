# Testy — strategia

## Rodzaje testów

- **Unit:** `domain/`, `services/` — bez Qt, szybkie.
- **Integration:** end-to-end (utwórz projekt programatycznie → zapisz → wczytaj → DRC).
- **GUI:** `pytest-qt`, smoke testy (otwórz okno, kliknij toolbar, nie crashnij).
- **Property-based:** `hypothesis` dla layout engine (random box trees → pozycje atomów monotoniczne, brak nakładek poza intencjonalnymi gapami).
- **Snapshot:** rendery YAML projektów porównywane z baseline.
- **Performance:** `pytest-benchmark` w `tests/benchmarks/` — zob. [benchmarks.md](benchmarks.md).

## Pokrycie

Target 80% dla `domain/` i `services/`. GUI nie liczone.

## Uruchomienie

```bash
uv run pytest                                   # wszystkie testy
uv run pytest tests/unit/                       # tylko unit
uv run pytest tests/benchmarks/ --benchmark-only -v
```
