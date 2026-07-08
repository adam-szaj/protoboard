  Profilowanie autoroute

  Trzy mechanizmy, niezależne — możesz włączyć po jednym lub wszystkie razem:

  1. Call counters (hierarchical, hot-funkcje)

  Pamięć [[call-counter-framework-2026-06-06]] — @counted("label") ContextVar stack.

  PROTOBOARD_CALL_COUNTERS=1 protoboard-designer …

  Wynik: services.call_counters.snapshot() zwraca dict {"autoroute.astar": N, "autoroute.simplify.candidate_safe": M, ...}. Programowo:

  from protoboard.services.call_counters import counters_enabled, snapshot
  with counters_enabled():
      autoroute(...)
  print(snapshot())

  Hot-paths zainstrumentowane: _get_pad_bucket_index, pin_world_position, find_owned_wires, Board.atoms, simplify candidate-checks.

  2. A* trace (universe per-net dla post-analysis)

  PROTOBOARD_ASTAR_TRACE_DIR=/tmp/astar_trace protoboard-designer …

  Każda sesja autoroute zapisuje YAML per-net w /tmp/astar_trace/ z full snapshot expanded nodes, frontiers, cost-breakdown. Analiza:

  python tools/analyze_astar_trace.py /tmp/astar_trace/net_VCC.yaml

  3. SIMPLIFY_TRACE (post-route simplify decisions)

  SIMPLIFY_TRACE=1 protoboard-designer …

  Każdy simplify.reject / simplify.accept / simplify.crosses_other.* ląduje w log'u (taki sam typ informacji jak Twój wczorajszy simplify.log).

  4. Diagnostyka równoległości (jeśli problem brzmi "wolno")

  # Wymuś sequential (ułatwia stack-traces, eliminuje threading overhead w profilach):
  PROTOBOARD_ASTAR_SEQUENTIAL=1 protoboard-designer …

  # Albo opt-in real thread pool:
  PROTOBOARD_ASTAR_PARALLEL=1 protoboard-designer …

  5. Bench harness (deterministyczny, do regresji)

