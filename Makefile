
all: test


test:
	QT_QPA_PLATFORM=offscreen uv run pytest tests/unit --ignore=tests/benchmarks --benchmark-disable -m "not serial and not slow and not property" --maxfail 1 -x --color yes --full-trace
