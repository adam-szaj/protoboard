# SPDX-License-Identifier: MIT
"""CSV report compatibility for routing runs."""

from __future__ import annotations

import re
from pathlib import Path

from structlog.testing import capture_logs

from protoboard.services.routing.autorouter import write_report
from protoboard.services.routing.hierarchical import write_report as hierarchical_report
from protoboard.services.routing.reporting import write_report as reporting_report
from protoboard.services.routing.results import AutorouteResult, NetStats, PassStats


def test_existing_report_imports_use_reporting_module() -> None:
    assert write_report is reporting_report
    assert hierarchical_report is reporting_report


def test_write_report_appends_exact_csv(tmp_path: Path) -> None:
    path = tmp_path / "report.csv"
    path.write_bytes(b"existing report\n")
    result = AutorouteResult(
        pass_stats=[
            PassStats(
                0,
                42,
                1,
                1,
                3,
                100,
                2,
                4,
                nets=[
                    NetStats('Łącznik, "A"', True, 80, 0, 1, 2, 6, 4, 2),
                    NetStats("B", False, 20, 2, 3, 0, 0, 0, 1),
                ],
            ),
            PassStats(1, 43, 2, 0, 4, 50, 0, 0),
        ]
    )

    with capture_logs() as logs:
        write_report(result, path)

    report = path.read_bytes().decode("utf-8")
    prefix, timestamp, body = report.split("\n", 2)
    assert prefix == "existing report"
    assert re.fullmatch(r"# run \d{4}-\d{2}-\d{2}T\d{2}:\d{2}:\d{2}", timestamp)
    assert body == (
        "## passes\n"
        "pass_index,seed,routed_count,unrouted_count,total_wires,"
        "total_nodes_expanded,total_over_budget_hits,total_rip_up_attempts\r\n"
        "0,42,1,1,3,100,2,4\r\n"
        "1,43,2,0,4,50,0,0\r\n"
        "## nets pass=0\n"
        "net_name,routed,nodes_expanded,over_budget_hits,rip_up_attempts,"
        "path_hops_min,path_hops_max,path_hops_final,segments_found\r\n"
        '"Łącznik, ""A""",1,80,0,1,2,6,4,2\r\n'
        "B,0,20,2,3,0,0,0,1\r\n"
        "## nets pass=1\n"
        "net_name,routed,nodes_expanded,over_budget_hits,rip_up_attempts,"
        "path_hops_min,path_hops_max,path_hops_final,segments_found\r\n"
        "\n"
    )
    assert logs == [
        {
            "event": "autoroute.report_written",
            "path": str(path),
            "log_level": "info",
        }
    ]


def test_write_report_logs_oserror_without_raising(tmp_path: Path) -> None:
    path = tmp_path / "missing" / "report.csv"

    with capture_logs() as logs:
        write_report(AutorouteResult(), path)

    assert not path.exists()
    assert len(logs) == 1
    assert logs[0]["event"] == "autoroute.report_write_failed"
    assert logs[0]["path"] == str(path)
    assert logs[0]["log_level"] == "warning"
    assert str(path) in logs[0]["error"]
