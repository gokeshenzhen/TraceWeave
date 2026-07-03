#!/usr/bin/env python3
"""Summarize TraceWeave usage telemetry into a readable report.

Reads the JSONL written by src/usage_telemetry.py (default:
<cache>/telemetry/usage.jsonl) and prints per-tool call counts, per-session
distributions, and a focused block on the auto-debug v2 primitives
(cursor / period / diff_first_divergence) — specifically the fraction of
sessions in which each was used at least once.

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


def render(report: dict) -> str:
    lines: list[str] = []
    lines.append("TraceWeave usage telemetry")
    lines.append("=" * 60)
    lines.append(f"records : {report['total_records']:,}")
    lines.append(f"sessions: {report['total_sessions']:,}  (a session = one get_sim_paths case)")
    lines.append("")

    cps = report["calls_per_session"]
    bps = report["result_bytes_per_session"]
    lines.append("Per-session distribution")
    lines.append("-" * 60)
    lines.append(f"  calls   : min {_fmt_int(cps['min'])}  median {_fmt_int(cps['median'])}  "
                 f"p90 {_fmt_int(cps['p90'])}  max {_fmt_int(cps['max'])}")
    lines.append(f"  bytes   : min {_fmt_int(bps['min'])}  median {_fmt_int(bps['median'])}  "
                 f"p90 {_fmt_int(bps['p90'])}  max {_fmt_int(bps['max'])}  total {_fmt_int(bps['total'])}")
    lines.append("")

    lines.append("Auto-debug v2 primitives — session presence")
    lines.append("-" * 60)
    lines.append(f"  {'feature':<22} {'calls':>7} {'sessions':>9} {'presence':>9}")
    for feature in TRACKED_FEATURES:
        t = report["tracked_features"].get(feature, {})
        lines.append(f"  {feature:<22} {t.get('calls', 0):>7} {t.get('sessions_used', 0):>9} "
                     f"{t.get('session_presence', 0.0)*100:>8.1f}%")
    lines.append("")

    lines.append("Per-tool (by call count)")
    lines.append("-" * 60)
    # ok% counts prerequisite blocks as not-ok; the blk column separates them
    # so a gated tool is not misread as a failing tool.
    lines.append(f"  {'tool':<34} {'calls':>6} {'ok%':>5} {'blk':>4} {'sess':>5} {'pres%':>6}")
    for tool, t in report["per_tool"].items():
        lines.append(f"  {tool:<34} {t['calls']:>6} {t['ok_rate']*100:>4.0f}% "
                     f"{t.get('blocked', 0):>4} "
                     f"{t['sessions']:>5} {t['session_presence']*100:>5.0f}%")

    failures = {tool: t["error_codes"] for tool, t in report["per_tool"].items()
                if t.get("error_codes")}
    if failures:
        lines.append("")
        lines.append("Not-ok calls by error_code (missing_prerequisite = gate, not failure)")
        lines.append("-" * 60)
        for tool, codes in failures.items():
            joined = ", ".join(f"{code}×{n}" for code, n in codes.items())
            lines.append(f"  {tool:<34} {joined}")
    return "\n".join(lines)


def main(argv=None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("path", nargs="?", default=None,
                        help="usage.jsonl path (default: cache dir)")
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
