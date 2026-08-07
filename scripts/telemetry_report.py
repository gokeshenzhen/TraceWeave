#!/usr/bin/env python3
"""Summarize TraceWeave usage telemetry into a readable report.

Reads the JSONL written by src/usage_telemetry.py (default:
<cache>/telemetry/usage.jsonl) and prints per-tool call counts, per-session
distributions, and a focused block on the auto-debug v2 primitives
(cursor / period / diff_first_divergence) — specifically the fraction of
sessions in which each was used at least once. When Source Graph diagnostics
are present, it also reports cache tiers, exact disk hit rate, validation
outcomes, build skips, capacity/evictions, and p50/p95 latency by tier.

This is an offline analysis tool, deliberately NOT an MCP tool: it answers the
"do the primitives earn their tool-surface slot" question with real numbers,
per the auto-debug v2 retrospective.

Usage:
    python scripts/telemetry_report.py [path/to/usage.jsonl] [--json]
"""

import argparse
import json
import os
import sys

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from src.usage_telemetry import aggregate, load_records, TRACKED_FEATURES  # noqa: E402


def _fmt_int(x) -> str:
    return f"{int(round(x)):,}"


def _fmt_ms(x) -> str:
    return f"{float(x):,.1f}"


def render(report: dict) -> str:
    lines: list[str] = []
    lines.append("TraceWeave usage telemetry")
    lines.append("=" * 60)
    lines.append(f"records : {report['total_records']:,}")
    lines.append(
        f"sessions: {report['total_sessions']:,}  (a session = one get_sim_paths case)"
    )
    lines.append("")

    cps = report["calls_per_session"]
    bps = report["result_bytes_per_session"]
    lines.append("Per-session distribution")
    lines.append("-" * 60)
    lines.append(
        f"  calls   : min {_fmt_int(cps['min'])}  median {_fmt_int(cps['median'])}  "
        f"p90 {_fmt_int(cps['p90'])}  max {_fmt_int(cps['max'])}"
    )
    lines.append(
        f"  bytes   : min {_fmt_int(bps['min'])}  median {_fmt_int(bps['median'])}  "
        f"p90 {_fmt_int(bps['p90'])}  max {_fmt_int(bps['max'])}  total {_fmt_int(bps['total'])}"
    )
    lines.append("")

    lines.append("Auto-debug v2 primitives — session presence")
    lines.append("-" * 60)
    lines.append(f"  {'feature':<22} {'calls':>7} {'sessions':>9} {'presence':>9}")
    for feature in TRACKED_FEATURES:
        t = report["tracked_features"].get(feature, {})
        lines.append(
            f"  {feature:<22} {t.get('calls', 0):>7} {t.get('sessions_used', 0):>9} "
            f"{t.get('session_presence', 0.0) * 100:>8.1f}%"
        )
    lines.append("")

    lines.append("Per-tool (by call count)")
    lines.append("-" * 60)
    # ok% counts prerequisite blocks as not-ok; the blk column separates them
    # so a gated tool is not misread as a failing tool.
    lines.append(
        f"  {'tool':<34} {'calls':>6} {'ok%':>5} {'blk':>4} {'sess':>5} {'pres%':>6}"
    )
    for tool, t in report["per_tool"].items():
        lines.append(
            f"  {tool:<34} {t['calls']:>6} {t['ok_rate'] * 100:>4.0f}% "
            f"{t.get('blocked', 0):>4} "
            f"{t['sessions']:>5} {t['session_presence'] * 100:>5.0f}%"
        )

    source_graph = report.get("source_graph", {})
    lines.append("")
    lines.append("Source Graph disk cache — operational telemetry")
    lines.append("-" * 60)
    if not source_graph.get("calls_with_metrics"):
        lines.append("  no Source Graph cache metrics recorded")
    else:
        disk = source_graph["disk"]
        execution = source_graph["execution"]
        tiers = source_graph["cache_tiers"]
        lines.append(
            "  calls/sessions       "
            f"{source_graph['calls_with_metrics']}/{source_graph['sessions_with_metrics']}"
        )
        lines.append(
            "  cache tier calls     "
            f"memory={tiers['memory']['calls']}  disk={tiers['disk']['calls']}  "
            f"build={tiers['build']['calls']}"
        )
        lines.append(
            "  exact disk lookup    "
            f"hit={_fmt_int(disk['hit_count'])}  "
            f"miss={_fmt_int(disk['miss_count'])}  "
            f"corrupt={_fmt_int(disk['corrupt_count'])}  "
            f"hit-rate={disk['exact_hit_rate'] * 100:.1f}%"
        )
        lines.append(
            "  builds/skips/launch  "
            f"{_fmt_int(execution['actual_build_count'])}/"
            f"{_fmt_int(disk['build_skip_count'])}/"
            f"{_fmt_int(execution['frontend_launch_count'])}"
        )
        lines.append(
            "  disk bytes/entries   "
            f"read={_fmt_int(disk['bytes_read'])}  "
            f"written={_fmt_int(disk['bytes_written'])}  "
            f"max={_fmt_int(disk['bytes_max'])}/"
            f"{_fmt_int(disk['entry_count_max'])}  "
            f"evicted={_fmt_int(disk['eviction_count'])}"
        )
        outcomes = source_graph.get("validation_outcomes", {})
        if outcomes:
            lines.append(
                "  validation outcomes "
                + ", ".join(
                    f"{name}={_fmt_int(count)}" for name, count in outcomes.items()
                )
            )
        lines.append("")
        lines.append("  Tier call latency (ms)")
        lines.append(f"  {'tier':<10} {'n':>5} {'p50':>10} {'p95':>10} {'max':>10}")
        for tier in ("memory", "disk", "build"):
            dist = tiers[tier]["call_latency_ms"]
            lines.append(
                f"  {tier:<10} {dist['n']:>5} {_fmt_ms(dist['median']):>10} "
                f"{_fmt_ms(dist['p95']):>10} {_fmt_ms(dist['max']):>10}"
            )

        by_tool = source_graph.get("by_tool", {})
        if by_tool:
            lines.append("")
            lines.append("  By tool")
            lines.append(
                f"  {'tool':<30} {'calls':>5} {'mem':>5} {'disk':>5} "
                f"{'build':>5} {'hit%':>6}"
            )
            for tool, values in by_tool.items():
                cache_tiers = values["cache_tiers"]
                lines.append(
                    f"  {tool:<30} {values['calls']:>5} "
                    f"{cache_tiers['memory']:>5} {cache_tiers['disk']:>5} "
                    f"{cache_tiers['build']:>5} "
                    f"{values['disk_exact_hit_rate'] * 100:>5.1f}%"
                )

    failures = {
        tool: t["error_codes"]
        for tool, t in report["per_tool"].items()
        if t.get("error_codes")
    }
    if failures:
        lines.append("")
        lines.append(
            "Not-ok calls by error_code (missing_prerequisite = gate, not failure)"
        )
        lines.append("-" * 60)
        for tool, codes in failures.items():
            joined = ", ".join(f"{code}×{n}" for code, n in codes.items())
            lines.append(f"  {tool:<34} {joined}")
    return "\n".join(lines)


def main(argv=None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument(
        "path", nargs="?", default=None, help="usage.jsonl path (default: cache dir)"
    )
    parser.add_argument("--json", action="store_true", help="emit raw report JSON")
    args = parser.parse_args(argv)

    records = load_records(args.path)
    report = aggregate(records)

    if args.json:
        print(json.dumps(report, ensure_ascii=False, indent=2))
    else:
        print(render(report))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
