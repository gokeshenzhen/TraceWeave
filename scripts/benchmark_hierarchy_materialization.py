#!/usr/bin/env python3
"""Benchmark eager hierarchy copies against internal template sharing.

Each measured trial runs in a fresh process. The synthetic design contains
many instances of one module whose leaf fanout is deliberately repeated. The
reported semantic digest expands every logical occurrence, while the measured
build path retains the normal compatibility dict schema.
"""

from __future__ import annotations

import argparse
import gc
import hashlib
import json
from pathlib import Path
import platform
import resource
import statistics
import subprocess
import sys
import time
from typing import Any, Mapping, Sequence


ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from src.operation_metrics import read_process_rss_kib  # noqa: E402
from src.tb_hierarchy_builder import (  # noqa: E402
    build_component_tree,
    scan_sv_text,
)


BENCHMARK_NAME = "hierarchy_template_materialization_v1"


def _git_head() -> str | None:
    completed = subprocess.run(
        ["git", "rev-parse", "HEAD"],
        cwd=ROOT,
        check=False,
        capture_output=True,
        text=True,
    )
    return completed.stdout.strip() or None if completed.returncode == 0 else None


def _git_dirty() -> bool | None:
    completed = subprocess.run(
        ["git", "status", "--porcelain"],
        cwd=ROOT,
        check=False,
        capture_output=True,
        text=True,
    )
    return bool(completed.stdout) if completed.returncode == 0 else None


def _make_scan(branches: int, fanout: int) -> dict[str, Any]:
    leaves = "\n".join(f"  leaf u_leaf_{index}();" for index in range(fanout))
    mids = "\n".join(f"  mid u_mid_{index}();" for index in range(branches))
    source = (
        "module leaf; endmodule\n"
        f"module mid;\n{leaves}\nendmodule\n"
        f"module top;\n{mids}\nendmodule\n"
    )
    return scan_sv_text(
        "synthetic_repeated.sv",
        source,
        retain_source_text=False,
    )


def _semantic_digest(component_tree: Mapping[str, Any]) -> str:
    """Hash the logical nested-dict value without serializing it in full."""

    children_cache: dict[int, bytes] = {}
    scalar_cache: dict[bytes, bytes] = {}

    def scalar_digest(node: Mapping[str, Any]) -> bytes:
        encoded = json.dumps(
            {
                str(field): value
                for field, value in node.items()
                if field != "children"
            },
            sort_keys=True,
            separators=(",", ":"),
            ensure_ascii=False,
        ).encode()
        cached = scalar_cache.get(encoded)
        if cached is not None:
            return cached
        digest = hashlib.sha256(encoded).digest()
        scalar_cache[encoded] = digest
        return digest

    def children_digest(children: Mapping[str, Any]) -> bytes:
        cached = children_cache.get(id(children))
        if cached is not None:
            return cached
        digest = hashlib.sha256()
        for inst_name, node in children.items():
            digest.update(str(inst_name).encode())
            digest.update(b"\0")
            if not isinstance(node, Mapping):
                digest.update(repr(node).encode())
                continue
            digest.update(scalar_digest(node))
            sub = node.get("children")
            if isinstance(sub, Mapping):
                digest.update(children_digest(sub))
            digest.update(b"\0")
        result = digest.digest()
        children_cache[id(children)] = result
        return result

    root_digest = hashlib.sha256()
    for top, children in component_tree.items():
        root_digest.update(str(top).encode())
        root_digest.update(b"\0")
        if isinstance(children, Mapping):
            root_digest.update(children_digest(children))
        root_digest.update(b"\0")
    return root_digest.hexdigest()


def run_trial(args: argparse.Namespace) -> dict[str, Any]:
    scan = _make_scan(args.branches, args.fanout)
    gc.collect()
    rss_start_kib = read_process_rss_kib()
    metrics: dict[str, int] = {}
    started = time.perf_counter()
    tree = build_component_tree(
        [scan],
        "top",
        share_templates=args.mode == "shared",
        metrics=metrics,
    )
    tree_wall_ms = (time.perf_counter() - started) * 1000.0
    rss_end_kib = read_process_rss_kib()
    peak_rss_kib = int(resource.getrusage(resource.RUSAGE_SELF).ru_maxrss)
    semantic_sha256 = _semantic_digest(tree)
    return {
        "benchmark": BENCHMARK_NAME,
        "mode": args.mode,
        "workload": {
            "branch_instance_count": args.branches,
            "leaf_instances_per_branch": args.fanout,
            "expected_logical_instance_count": args.branches
            * (args.fanout + 1),
        },
        "measurement": {
            "tree_wall_ms": round(tree_wall_ms, 3),
            "rss_start_kib": rss_start_kib,
            "rss_end_kib": rss_end_kib,
            "rss_delta_kib": (
                rss_end_kib - rss_start_kib
                if isinstance(rss_start_kib, int)
                and isinstance(rss_end_kib, int)
                else None
            ),
            "peak_rss_kib": peak_rss_kib,
        },
        "representation": metrics,
        "behavior_oracle": {
            "semantic_sha256": semantic_sha256,
            "logical_instance_count": metrics["hierarchy_logical_node_count"],
            "logical_tree_depth": metrics["hierarchy_logical_tree_depth"],
        },
    }


def _median(runs: Sequence[Mapping[str, Any]], field: str) -> float:
    return round(
        statistics.median(float(run["measurement"][field]) for run in runs),
        3,
    )


def aggregate_runs(runs: Sequence[Mapping[str, Any]]) -> dict[str, Any]:
    by_mode: dict[str, Any] = {}
    for mode in ("eager", "shared"):
        selected = [run for run in runs if run["mode"] == mode]
        if not selected:
            continue
        by_mode[mode] = {
            "run_count": len(selected),
            "tree_wall_median_ms": _median(selected, "tree_wall_ms"),
            "rss_delta_median_kib": _median(selected, "rss_delta_kib"),
            "peak_rss_median_kib": _median(selected, "peak_rss_kib"),
            "physical_node_counts": sorted(
                {
                    run["representation"]["hierarchy_physical_node_count"]
                    for run in selected
                }
            ),
            "semantic_oracles": sorted(
                {run["behavior_oracle"]["semantic_sha256"] for run in selected}
            ),
        }

    comparison = None
    if set(by_mode) == {"eager", "shared"}:
        eager = by_mode["eager"]
        shared = by_mode["shared"]

        def reduction(field: str) -> float | None:
            baseline = float(eager[field])
            if baseline <= 0:
                return None
            return round((baseline - float(shared[field])) * 100.0 / baseline, 3)

        comparison = {
            "tree_wall_reduction_percent": reduction("tree_wall_median_ms"),
            "rss_delta_reduction_percent": reduction("rss_delta_median_kib"),
            "peak_rss_change_kib": round(
                float(shared["peak_rss_median_kib"])
                - float(eager["peak_rss_median_kib"]),
                3,
            ),
            "behavior_equal": (
                eager["semantic_oracles"] == shared["semantic_oracles"]
            ),
        }
    return {"by_mode": by_mode, "comparison": comparison}


def _child_command(args: argparse.Namespace, mode: str) -> list[str]:
    return [
        sys.executable,
        str(Path(__file__).resolve()),
        "--branches",
        str(args.branches),
        "--fanout",
        str(args.fanout),
        "--mode",
        mode,
        "--child",
    ]


def _run_child(args: argparse.Namespace, mode: str) -> dict[str, Any]:
    completed = subprocess.run(
        _child_command(args, mode),
        cwd=ROOT,
        check=False,
        capture_output=True,
        text=True,
    )
    if completed.returncode != 0:
        raise RuntimeError(
            f"benchmark child failed for {mode}: {completed.stderr.strip()}"
        )
    return json.loads(completed.stdout)


def build_argument_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--branches", type=int, default=200)
    parser.add_argument("--fanout", type=int, default=1000)
    parser.add_argument(
        "--mode",
        choices=("compare", "eager", "shared"),
        default="compare",
    )
    parser.add_argument("--repeats", type=int, default=3)
    parser.add_argument("--output", type=Path)
    parser.add_argument("--child", action="store_true", help=argparse.SUPPRESS)
    return parser


def main(argv: Sequence[str] | None = None) -> int:
    args = build_argument_parser().parse_args(argv)
    if args.branches < 1 or args.fanout < 1 or args.repeats < 1:
        raise SystemExit("branches/fanout/repeats must be positive")
    if args.child:
        if args.mode == "compare":
            raise SystemExit("child mode must select eager or shared")
        print(json.dumps(run_trial(args), sort_keys=True, separators=(",", ":")))
        return 0

    requested_modes = (
        ("eager", "shared") if args.mode == "compare" else (args.mode,)
    )
    runs = []
    for repeat in range(args.repeats):
        modes = requested_modes if repeat % 2 == 0 else tuple(reversed(requested_modes))
        for mode in modes:
            runs.append(_run_child(args, mode))
    report = {
        "benchmark": BENCHMARK_NAME,
        "schema_version": 1,
        "git": {"head": _git_head(), "dirty": _git_dirty()},
        "environment": {
            "python": platform.python_version(),
            "platform": platform.platform(),
            "processor": platform.processor() or None,
        },
        "conditions": {
            "repeats_per_mode": args.repeats,
            "fresh_process_per_trial": True,
            "npi_source_overlay": False,
        },
        "runs": runs,
        "aggregate": aggregate_runs(runs),
    }
    rendered = json.dumps(report, indent=2, sort_keys=True)
    if args.output is not None:
        args.output.write_text(rendered + "\n", encoding="utf-8")
    print(rendered)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
