# SPDX-License-Identifier: MIT
"""CSV reports for autorouting runs."""

from __future__ import annotations

import csv
import datetime
import io
from pathlib import Path

import structlog

from protoboard.services.routing.results import AutorouteResult

# Preserve the report logger's existing namespace for log consumers.
_log = structlog.get_logger("protoboard.services.routing.autorouter")


def write_report(result: AutorouteResult, path: Path) -> None:
    """Append a CSV report for this autoroute run to *path*.

    Format (append mode, UTF-8):
      # run <timestamp>
      ## passes
      pass_index, seed, routed_count, unrouted_count, total_wires,
        total_nodes_expanded, total_over_budget_hits, total_rip_up_attempts
      <one row per pass>
      ## nets  pass=<pass_index>
      net_name, routed, nodes_expanded, over_budget_hits, rip_up_attempts,
        path_hops_min, path_hops_max, path_hops_final, segments_found
      <one row per net>
      ... repeated for each pass
    """
    ts = datetime.datetime.now().isoformat(timespec="seconds")
    buf = io.StringIO()
    w = csv.writer(buf)

    buf.write(f"# run {ts}\n")

    # --- passes summary ---
    buf.write("## passes\n")
    w.writerow(
        [
            "pass_index",
            "seed",
            "routed_count",
            "unrouted_count",
            "total_wires",
            "total_nodes_expanded",
            "total_over_budget_hits",
            "total_rip_up_attempts",
        ]
    )
    for ps in result.pass_stats:
        w.writerow(
            [
                ps.pass_index,
                ps.seed,
                ps.routed_count,
                ps.unrouted_count,
                ps.total_wires,
                ps.total_nodes_expanded,
                ps.total_over_budget_hits,
                ps.total_rip_up_attempts,
            ]
        )

    # --- per-net details per pass ---
    for ps in result.pass_stats:
        buf.write(f"## nets pass={ps.pass_index}\n")
        w.writerow(
            [
                "net_name",
                "routed",
                "nodes_expanded",
                "over_budget_hits",
                "rip_up_attempts",
                "path_hops_min",
                "path_hops_max",
                "path_hops_final",
                "segments_found",
            ]
        )
        for ns in ps.nets:
            w.writerow(
                [
                    ns.net_name,
                    int(ns.routed),
                    ns.nodes_expanded,
                    ns.over_budget_hits,
                    ns.rip_up_attempts,
                    ns.path_hops_min,
                    ns.path_hops_max,
                    ns.path_hops_final,
                    ns.segments_found,
                ]
            )

    buf.write("\n")

    try:
        with path.open("a", encoding="utf-8", newline="") as f:
            f.write(buf.getvalue())
        _log.info("autoroute.report_written", path=str(path))
    except OSError as e:
        _log.warning("autoroute.report_write_failed", path=str(path), error=str(e))
