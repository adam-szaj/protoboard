#!/usr/bin/env python3
# SPDX-License-Identifier: MIT
"""Analyze A* universe traces dumped by PROTOBOARD_ASTAR_TRACE_DIR.

Typical workflow:

    rm -rf /tmp/astar_trace
    PROTOBOARD_ASTAR_TRACE_DIR=/tmp/astar_trace \\
        uv run python -m protoboard tests/gold_performance_test
    # ... run autoroute in the GUI, then exit ...

    # Quick overview of every dumped universe:
    uv run tools/analyze_astar_trace.py /tmp/astar_trace summary

    # Find the universe whose final path contains a given pad:
    uv run tools/analyze_astar_trace.py /tmp/astar_trace find-path \\
        --pad 24.5,34.5,bottom

    # Replay the search that produced one specific trace file:
    uv run tools/analyze_astar_trace.py /tmp/astar_trace replay \\
        astar_1747408530001_000012.jsonl

    # Show every step where a target pad was *rejected* and why:
    uv run tools/analyze_astar_trace.py /tmp/astar_trace why-blocked \\
        --pad 24.5,34.5,bottom

Pair the output with the parent autoroute.short_detected log line — the
log carries the wire path, this tool tells you which A* decision built it.
"""

from __future__ import annotations

import argparse
import json
import sys
from collections import Counter, defaultdict
from collections.abc import Iterable, Iterator
from pathlib import Path
from typing import Any

# ---------------------------------------------------------------------------
# Trace I/O
# ---------------------------------------------------------------------------


def _iter_events(path: Path) -> Iterator[dict[str, Any]]:
    """Yield every JSON event from a trace file, skipping malformed lines."""
    with path.open() as fp:
        for line_no, line in enumerate(fp, start=1):
            line = line.strip()
            if not line:
                continue
            try:
                yield json.loads(line)
            except json.JSONDecodeError as exc:
                print(
                    f"  WARN {path.name}:{line_no} malformed JSON: {exc}",
                    file=sys.stderr,
                )


def _list_traces(trace_dir: Path) -> list[Path]:
    return sorted(trace_dir.glob("astar_*.jsonl"))


def _load_summary(path: Path) -> dict[str, Any]:
    """Return one summary dict for a trace file.

    Keys: file, init, end_reason, found, path_len, cost, nodes_expanded,
          steps, source, target.
    """
    init: dict[str, Any] | None = None
    end_reason: str | None = None
    found = False
    path_pts: list[Any] | None = None
    cost: float | None = None
    nodes_expanded = 0
    steps = 0
    source: tuple[float, float, str] | None = None
    target: list[float] | None = None

    for event in _iter_events(path):
        kind = event.get("e")
        if kind == "init":
            init = event
            target = event.get("first_target")
        elif kind == "step":
            steps += 1
            if source is None and steps == 1:
                source = tuple(event["pos"])
            nodes_expanded = event.get("nodes_expanded", nodes_expanded)
        elif kind == "goal":
            found = True
            path_pts = event.get("path")
            cost = event.get("g", event.get("cost"))
            nodes_expanded = event.get("nodes_expanded", nodes_expanded)
        elif kind == "end":
            end_reason = event.get("reason")
            nodes_expanded = event.get("nodes_expanded", nodes_expanded)

    return {
        "file": path.name,
        "init": init or {},
        "end_reason": end_reason,
        "found": found,
        "path_len": len(path_pts) if path_pts is not None else None,
        "cost": cost,
        "nodes_expanded": nodes_expanded,
        "steps": steps,
        "source": source,
        "target": target,
        "path": path_pts,
    }


# ---------------------------------------------------------------------------
# Commands
# ---------------------------------------------------------------------------


def cmd_summary(trace_dir: Path, args: argparse.Namespace) -> int:
    traces = _list_traces(trace_dir)
    if not traces:
        print(f"No astar_*.jsonl files in {trace_dir}", file=sys.stderr)
        return 1

    print(f"{len(traces)} trace file(s) in {trace_dir}\n")
    print(
        f"{'file':40s} {'src→tgt':30s} {'res':5s} {'cost':>8s} "
        f"{'plen':>5s} {'steps':>7s} {'nodes':>7s} reason"
    )
    print("-" * 110)

    found_count = 0
    fail_count = 0
    total_steps = 0
    total_nodes = 0
    for p in traces:
        s = _load_summary(p)
        if s["found"]:
            found_count += 1
        else:
            fail_count += 1
        total_steps += s["steps"]
        total_nodes += s["nodes_expanded"]
        src = s["source"]
        tgt = s["target"]
        src_s = f"{src[0]:g},{src[1]:g},{src[2]}" if src else "?"
        tgt_s = f"{tgt[0]:g},{tgt[1]:g}" if tgt else "?"
        cost_s = f"{s['cost']:.2f}" if s["cost"] is not None else "-"
        plen_s = str(s["path_len"]) if s["path_len"] is not None else "-"
        print(
            f"{s['file']:40s} {src_s:>13s}→{tgt_s:<15s} "
            f"{'OK' if s['found'] else 'FAIL':5s} "
            f"{cost_s:>8s} {plen_s:>5s} {s['steps']:>7d} "
            f"{s['nodes_expanded']:>7d} {s['end_reason'] or '-'}"
        )

    print("-" * 110)
    print(
        f"Found: {found_count}  Failed: {fail_count}  "
        f"Total steps: {total_steps:,}  Total nodes: {total_nodes:,}"
    )
    return 0


def _path_contains(path_pts: Iterable[Any], pad: tuple[float, float, str]) -> bool:
    px, py, ps = pad
    for entry in path_pts:
        x, y, s = entry[0], entry[1], entry[2]
        if x == px and y == py and s == ps:
            return True
    return False


def cmd_find_path(trace_dir: Path, args: argparse.Namespace) -> int:
    pad = _parse_pad(args.pad)
    traces = _list_traces(trace_dir)
    hits = []
    for p in traces:
        s = _load_summary(p)
        if s["path"] and _path_contains(s["path"], pad):
            hits.append(s)

    if not hits:
        print(f"No final paths contain pad ({pad[0]}, {pad[1]}, {pad[2]})")
        return 1

    print(
        f"{len(hits)} universe(s) produced a path through "
        f"({pad[0]}, {pad[1]}, {pad[2]}):\n"
    )
    for s in hits:
        src = s["source"]
        tgt = s["target"]
        src_s = f"{src[0]:g},{src[1]:g},{src[2]}" if src else "?"
        tgt_s = f"{tgt[0]:g},{tgt[1]:g}" if tgt else "?"
        print(
            f"  {s['file']:40s}  {src_s} → {tgt_s}  "
            f"cost={s['cost']}  steps={s['steps']}"
        )
        # Show ±2 steps around the pad for context.
        path = s["path"]
        for i, entry in enumerate(path):
            if entry[0] == pad[0] and entry[1] == pad[1] and entry[2] == pad[2]:
                lo = max(0, i - 2)
                hi = min(len(path), i + 3)
                for j in range(lo, hi):
                    marker = "  >>>" if j == i else "     "
                    e = path[j]
                    print(f"{marker} [{j:3d}] ({e[0]},{e[1]},{e[2]})")
                break
        print()
    return 0


def cmd_replay(trace_dir: Path, args: argparse.Namespace) -> int:
    path = trace_dir / args.file
    if not path.exists():
        print(f"File not found: {path}", file=sys.stderr)
        return 1

    step_num = 0
    for event in _iter_events(path):
        kind = event.get("e")
        if kind == "init":
            print("INIT")
            for k, v in event.items():
                if k != "e":
                    print(f"  {k}: {v}")
            print()
        elif kind == "step":
            step_num += 1
            if args.limit and step_num > args.limit:
                print(
                    f"...(truncated at {args.limit} steps; " f"pass --limit 0 for full)"
                )
                break
            pos = event["pos"]
            g = event["g"]
            queued = event["queued"]
            neighbours = event.get("neighbours", [])
            via = event.get("via", False)
            via_s = " +via" if via else ""
            print(
                f"[{step_num:5d}] pop ({pos[0]},{pos[1]},{pos[2]}) "
                f"g={g:.2f} queued={queued}{via_s}"
            )
            if args.verbose and neighbours:
                for n in neighbours:
                    ndx, ndy, status, *extra = n
                    extra_s = " ".join(str(x) for x in extra)
                    print(f"        nb ({ndx:+g},{ndy:+g}) → {status} {extra_s}")
        elif kind == "goal":
            cost_val = event.get("g", event.get("cost"))
            print(
                f"\nGOAL at ({event['pos'][0]},{event['pos'][1]},"
                f"{event['pos'][2]})  cost={cost_val}  "
                f"nodes={event['nodes_expanded']}"
            )
            print(f"Path ({len(event['path'])} points):")
            for i, e in enumerate(event["path"]):
                print(f"  [{i:3d}] ({e[0]},{e[1]},{e[2]})")
        elif kind == "end":
            print(
                f"\nEND  reason={event['reason']}  " f"nodes={event['nodes_expanded']}"
            )
    return 0


def cmd_why_blocked(trace_dir: Path, args: argparse.Namespace) -> int:
    pad = _parse_pad(args.pad)
    px, py, ps = pad
    traces = _list_traces(trace_dir)

    # Per-reason histogram across every step that *could have* moved into pad.
    reason_counts: Counter[str] = Counter()
    file_reasons: dict[str, Counter[str]] = defaultdict(Counter)
    expansion_files: list[str] = []

    for path in traces:
        # We need to know which (from_pos, dx, dy) lands on pad.
        # Iterate steps and inspect neighbour decisions for ndx/ndy that
        # take cx+ndx == px and cy+ndy == py.
        for event in _iter_events(path):
            if event.get("e") != "step":
                continue
            cx, cy, side = event["pos"]
            if side != ps:
                continue
            for n in event.get("neighbours") or []:
                ndx, ndy = n[0], n[1]
                if cx + ndx == px and cy + ndy == py:
                    status = n[2]
                    reason_counts[status] += 1
                    file_reasons[path.name][status] += 1
                    if status == "queued":
                        expansion_files.append(path.name)

    if not reason_counts:
        print(f"No A* step ever even considered ({px},{py},{ps}).")
        return 1

    print(
        f"Across {len(traces)} universe(s), pad ({px},{py},{ps}) was "
        f"a candidate {sum(reason_counts.values())} time(s):"
    )
    for status, n in reason_counts.most_common():
        print(f"  {n:6d}  {status}")
    print()

    if expansion_files:
        print(f"{len(expansion_files)} universe(s) actually walked INTO the pad:")
        for fname in expansion_files[:20]:
            print(f"  {fname}")
        if len(expansion_files) > 20:
            print(f"  ...and {len(expansion_files) - 20} more")
        print()
        print(
            "→ Suspect: A* didn't see this pad as occupied.  Compare the "
            "init.n_occupied count against the parent autoroute call."
        )
    else:
        print(
            "→ Every candidate step was rejected. The fusion did NOT come "
            "from this pad being walked; check find-path on the wire's "
            "other endpoints."
        )
    return 0


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def _parse_pad(spec: str) -> tuple[float, float, str]:
    """Parse "x,y,side" into a (x, y, side) tuple."""
    parts = spec.split(",")
    if len(parts) != 3:
        raise SystemExit(f"--pad must be x,y,side; got {spec!r}")
    try:
        return (float(parts[0]), float(parts[1]), parts[2].strip())
    except ValueError as exc:
        raise SystemExit(f"--pad parse error: {exc}") from exc


def main() -> int:
    parser = argparse.ArgumentParser(
        description="Analyze A* universe trace dumps",
        formatter_class=argparse.RawDescriptionHelpFormatter,
        epilog=__doc__,
    )
    parser.add_argument(
        "trace_dir", type=Path, help="directory containing astar_*.jsonl files"
    )
    sub = parser.add_subparsers(dest="cmd", required=True)

    sub.add_parser(
        "summary", help="one line per trace: src→tgt, found/failed, cost, steps"
    )

    p_find = sub.add_parser(
        "find-path", help="list universes whose final path contains a given pad"
    )
    p_find.add_argument(
        "--pad", required=True, help="pad spec: x,y,side  (e.g. 24.5,34.5,bottom)"
    )

    p_replay = sub.add_parser("replay", help="print every step of one trace file")
    p_replay.add_argument("file", help="trace file name (relative to trace_dir)")
    p_replay.add_argument(
        "--limit", type=int, default=200, help="max steps to print (0 = unlimited)"
    )
    p_replay.add_argument(
        "--verbose",
        "-v",
        action="store_true",
        help="also dump per-step neighbour decisions",
    )

    p_why = sub.add_parser(
        "why-blocked",
        help="across every universe, classify decisions that touched a pad",
    )
    p_why.add_argument("--pad", required=True, help="pad spec: x,y,side")

    args = parser.parse_args()
    if not args.trace_dir.exists():
        print(f"Trace dir not found: {args.trace_dir}", file=sys.stderr)
        return 2

    handlers = {
        "summary": cmd_summary,
        "find-path": cmd_find_path,
        "replay": cmd_replay,
        "why-blocked": cmd_why_blocked,
    }
    return handlers[args.cmd](args.trace_dir, args)


if __name__ == "__main__":
    raise SystemExit(main())
