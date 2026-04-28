# protoboard-designer

Connectivity-aware perfboard/stripboard layout designer for Linux (PySide6).

Unlike purely graphical tools, the internal model is a netlist graph — physical layout is a projection of that graph onto the hole grid. This enables DRC, KiCAD netlist import/export, and routing suggestions.

![CI](https://github.com/adam-szaj/protoboard-designer/actions/workflows/ci.yml/badge.svg)

## Development setup

```bash
uv venv
source .venv/bin/activate
uv pip install -e ".[dev]"
pre-commit install
```

## Common commands

```bash
# Lint
ruff check src/ tests/

# Format check
black --check src/ tests/

# Type check
mypy src/

# Run all tests
pytest

# Run a single test
pytest tests/unit/test_layout_engine.py::test_name -v

# Run with coverage
pytest --cov=protoboard --cov-report=term-missing

# Run the app (Phase 3+)
protoboard
```

## License

MIT — see [LICENSE](LICENSE).
