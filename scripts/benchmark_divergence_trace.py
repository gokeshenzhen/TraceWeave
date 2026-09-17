#!/usr/bin/env python3
"""Compare manual/automatic divergence evidence on independent local fixtures.

Each arm runs in a fresh process. Cold/warm measurements exclude hierarchy,
scanner and KDB compilation. --workload all exercises the six PR workloads.
NPI mode invokes VCS on these owned sources; it never uses an external design.
"""

from __future__ import annotations
import argparse
import asyncio
import json
import os
from pathlib import Path
import resource
import statistics
import subprocess
import sys
import tempfile
import time

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))
from divergence_benchmark_workloads import workloads


async def measure(args):
    import server
    from src.divergence_context import resolve_context

    case = Path(args.case)
    oracle = workloads()[args.workload]
    ctx = {"compile_log": str(case / "compile.log"), "simulator": "vcs"}
    await asyncio.gather(
        server._dispatch("build_tb_hierarchy", ctx),
        server._dispatch("scan_structural_risks", ctx),
    )
    bound, reason = resolve_context(ctx, server._handle_store)
    assert bound is not None, reason
    wave = str(case / "wave.vcd")
    trace_args = {
        "side_a": {**ctx, "wave_path": wave, "signal_path": "top.qa"},
        "side_b": {**ctx, "wave_path": wave, "signal_path": "top.qb"},
        "signal_pairs": oracle.pairs or [{"a": "top.a", "b": "top.b"}],
        "start_time_ps": 0,
        "end_time_ps": oracle.end,
    }
    # Aggregate instrumentation only; do not change the parser/runtime behavior.
    counters = {}
    get_parser = server._get_parser
    receipt = server._source_graph_receipt_from_prepare

    class CountReads:
        def __init__(self, parser):
            self.parser = parser

        def __getattr__(self, name):
            return getattr(self.parser, name)

        def get_transitions(self, *a, **kw):
            counters["transition_reads"] += 1
            return self.parser.get_transitions(*a, **kw)

    def prepare_receipt(plan, outcome, **kwargs):
        metrics = outcome.metrics
        counters["source_graph_builds"] += metrics.actual_build_count
        counters["frontend_cpu_ms"] += metrics.worker_cpu_ms or 0
        counters["frontend_peak_rss_kib"] = max(
            counters["frontend_peak_rss_kib"], metrics.rss_peak_kib or 0
        )
        return receipt(plan, outcome, **kwargs)

    server._get_parser = lambda path: CountReads(get_parser(path))
    server._source_graph_receipt_from_prepare = prepare_receipt
    timings, cpu_times, counts, statuses, backends, work = [], [], [], [], [], []
    peak_cache = 0
    for _ in range(args.repeats + 1):
        counters = dict(
            transition_reads=0,
            source_graph_builds=0,
            frontend_cpu_ms=0,
            frontend_peak_rss_kib=0,
        )
        started, cpu = time.perf_counter(), time.process_time()
        if args.arm == "automatic":
            result = await server._dispatch("trace_divergence", trace_args)
            assert result.comparison["first_divergence_time_ps"] == oracle.root_time, (
                result.model_dump()
            )
            observed = {
                (
                    n["a"]["signal"].removeprefix("top."),
                    n["b"]["signal"].removeprefix("top."),
                )
                for n in result.nodes
            }
            assert observed == set(oracle.nodes), result.model_dump()
            if oracle.edge_count is not None:
                assert len(result.edges) == oracle.edge_count, result.model_dump()
            if oracle.missing:
                assert (
                    result.status == "partial"
                    and "signal_not_dumped" in result.coverage["gaps"]
                )
                assert not any(
                    f["kind"] == "local_logic_candidate" for f in result.findings
                )
            for node in result.nodes:
                for side in ("a", "b"):
                    fact = node[side]
                    leaf = fact["signal"].removeprefix("top.")
                    if leaf in oracle.samples:
                        _, _, expected = oracle.samples[leaf]
                        assert int(fact["value"], 2) == expected, fact
                leaf = node["a"]["signal"].removeprefix("top.")
                if leaf in oracle.triggers:
                    assert (
                        node["a"]["observation"]["trigger_time_ps"]
                        == oracle.triggers[leaf]
                    )
                    assert (
                        node["b"]["observation"]["trigger_time_ps"]
                        == oracle.triggers[leaf]
                    )
            if args.backend == "npi":
                receipts = [result.contexts[s]["backend_status"] for s in ("a", "b")]
                for r in receipts:
                    assert r["selected_backend"] == "verdi_npi"
                    if r["actual_backend"] != "verdi_npi":
                        # Verdi 2023.12 collapses the deep inactive tree into an
                        # opaque npiNlOpCell. Never claim to evaluate its text.
                        assert (
                            oracle.allow_npi_fallback
                            and r["actual_backend"] == "source_graph"
                        ), r
                        assert r["fallback_reason"] == "dynamic_evidence_unavailable", r
                        assert result.status == "partial"
                if not oracle.missing and all(
                    r["actual_backend"] == "verdi_npi" for r in receipts
                ):
                    assert result.status == "complete", result.model_dump()
            counts.append(1)
            statuses.append(result.status)
            backends.append(
                [
                    result.contexts[s]["backend_status"]["actual_backend"]
                    for s in ("a", "b")
                ]
            )
            peak_cache = max(
                peak_cache, result.operation_metrics["wave_cache_peak_bytes"]
            )
            counters["backend_queries"] = result.operation_metrics[
                "backend_query_count"
            ]
            counters["admitted_nodes"] = result.operation_metrics["admitted_node_count"]
            counters["returned_nodes"] = len(result.nodes)
            counters["returned_edges"] = len(result.edges)
            counters["graph_restarts"] = result.operation_metrics["restart_count"]
        else:
            diff = await server._dispatch(
                "diff_first_divergence",
                dict(
                    wave_path_a=wave,
                    signal_a="top.qa",
                    wave_path_b=wave,
                    signal_b="top.qb",
                    start_time_ps=0,
                    end_time_ps=oracle.end,
                ),
            )
            assert diff.first_divergence_time_ps == oracle.root_time
            actual = []
            # The same node set and both sides, including shared leaf only once.
            signals = list(dict.fromkeys(s for pair in oracle.nodes for s in pair))
            for signal in signals:
                result = await server._dispatch(
                    "explain_signal_driver",
                    dict(
                        signal_path="top." + signal,
                        wave_path=wave,
                        **ctx,
                        compile_context=bound.context,
                    ),
                )
                actual.append(result.backend)
            evidence = {}
            reads = list(
                dict.fromkeys(
                    [*signals[2:], *oracle.samples, *oracle.controls, *oracle.missing]
                )
            )
            for leaf in reads:
                try:
                    result = await server._dispatch(
                        "get_signal_transitions",
                        dict(
                            wave_path=wave,
                            signal_path="top." + leaf,
                            start_time_ps=0,
                            end_time_ps=oracle.root_time,
                        ),
                    )
                except KeyError:
                    assert leaf in oracle.missing
                    continue
                assert leaf not in oracle.missing, (leaf, result)
                assert not result.truncated
                evidence[leaf] = [
                    (e["time_ps"], e["value"]["bin"]) for e in result.transitions
                ]
            for leaf, (sample, phase, expected) in oracle.samples.items():
                values = [
                    bits
                    for tick, bits in evidence[leaf]
                    if tick < sample or (tick == sample and phase == "after")
                ]
                assert values and int(values[-1], 2) == expected, (leaf, values)
            for trigger in oracle.triggers.values():
                events = evidence["clk"]
                assert any(
                    t == trigger and bits == "1" and i > 0 and events[i - 1][1] == "0"
                    for i, (t, bits) in enumerate(events)
                )
            counts.append(1 + len(signals) + len(reads))
            statuses.append(
                "oracle_missing_dump_confirmed"
                if oracle.missing
                else "oracle_evidence_checked"
            )
            backends.append(actual)
            counters["backend_queries"] = len(signals)
        timings.append(round((time.perf_counter() - started) * 1000, 3))
        cpu_times.append(round((time.process_time() - cpu) * 1000, 3))
        work.append(dict(counters))
    return dict(
        arm=args.arm,
        cold_ms=timings[0],
        warm_ms=timings[1:],
        warm_median_ms=statistics.median(timings[1:]),
        warm_max_ms=max(timings[1:]),
        server_cpu_ms=cpu_times,
        public_calls_per_iteration=counts,
        work=work,
        server_peak_rss_kib=resource.getrusage(resource.RUSAGE_SELF).ru_maxrss,
        additional_wave_cache_peak_bytes=peak_cache
        if args.arm == "automatic"
        else None,
        statuses=statuses,
        actual_backends=backends,
        verified_root_time_ps=oracle.root_time,
        verified_nodes=oracle.nodes,
    )


def run_case(args, oracle):
    with tempfile.TemporaryDirectory(
        prefix="traceweave-divergence-benchmark-"
    ) as directory:
        case = Path(directory)
        (case / "design.sv").write_text(oracle.rtl)
        (case / "wave.vcd").write_text(oracle.waveform())
        if args.backend == "npi":
            run = subprocess.run(
                [
                    "vcs",
                    "-full64",
                    "-sverilog",
                    "-timescale=1ns/1ps",
                    "-debug_access+all",
                    "-kdb",
                    str(case / "design.sv"),
                    "-top",
                    "top",
                    "-o",
                    "simv",
                    "-l",
                    "compile.log",
                ],
                cwd=case,
                stdout=subprocess.PIPE,
                stderr=subprocess.STDOUT,
                text=True,
                timeout=90,
            )
            if run.returncode:
                raise RuntimeError(
                    "benchmark fixture VCS compilation failed: " + run.stdout[-2000:]
                )
        else:
            (case / "compile.log").write_text(
                f"Chronologic VCS simulator\nCommand: vcs -sverilog {case / 'design.sv'} -top top\nParsing design file '{case / 'design.sv'}'\nTop Level Modules:\n       top\n"
            )
        environment = {
            **os.environ,
            "TRACEWEAVE_CONNECTIVITY_ROUTE": "auto"
            if args.backend == "npi"
            else "source_graph",
            "TRACEWEAVE_SOURCE_GRAPH_PYTHON": args.source_python,
            "TRACEWEAVE_NPI_EXECUTION": "local",
            "TRACEWEAVE_HIERARCHY_NPI_SOURCE_OVERLAY": "off",
            "TRACEWEAVE_SOURCE_GRAPH_DISK_CACHE": "0",
            "TRACEWEAVE_SOURCE_GRAPH_SEMANTIC_SESSION": "0",
            "TRACEWEAVE_AUTO_KDB": "0",
        }
        results = []
        for arm in ("manual", "automatic"):
            child = subprocess.run(
                [
                    sys.executable,
                    __file__,
                    "--arm",
                    arm,
                    "--case",
                    str(case),
                    "--backend",
                    args.backend,
                    "--workload",
                    oracle.name,
                    "--repeats",
                    str(args.repeats),
                ],
                env=environment,
                cwd=ROOT,
                capture_output=True,
                text=True,
                timeout=180,
            )
            if child.returncode:
                raise RuntimeError(
                    f"{oracle.name}/{arm}: " + child.stderr + child.stdout[-2000:]
                )
            results.append(
                json.loads(
                    next(
                        line[7:]
                        for line in child.stdout.splitlines()
                        if line.startswith("RESULT ")
                    )
                )
            )
        return dict(
            workload=oracle.name,
            signal_count=len(oracle.widths) - len(oracle.missing),
            waveform_bytes=(case / "wave.vcd").stat().st_size,
            window_ps=[0, oracle.end],
            expected_node_count=len(oracle.nodes),
            results=results,
        )


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument(
        "--backend", choices=("source_graph", "npi"), default="source_graph"
    )
    parser.add_argument("--source-python", default=str(ROOT / ".venv/bin/python"))
    parser.add_argument("--workload", choices=["all", *workloads()], default="register")
    parser.add_argument("--repeats", type=int, default=3)
    parser.add_argument("--output")
    parser.add_argument("--arm", choices=("manual", "automatic"))
    parser.add_argument("--case")
    args = parser.parse_args()
    if not 1 <= args.repeats <= 20:
        parser.error("repeats must be 1..20")
    if args.arm:
        print("RESULT " + json.dumps(asyncio.run(measure(args))))
        return
    cases = workloads()
    selected = cases.values() if args.workload == "all" else [cases[args.workload]]
    report = dict(
        backend=args.backend,
        repeats=args.repeats,
        conditions="fresh process per arm; VCD 1ps; hierarchy/scan/KDB compilation excluded; disk and semantic-session caches off; warm reuses normal parser/artifact/native caches",
        timing_scope="in-process public dispatch wall time (CPU/IO included); MCP transport and model latency excluded; server CPU excludes frontend CPU reported separately",
        rss_scope="server process high-water RSS and separate frontend high-water RSS, not their sum; unavailable frontend telemetry is zero",
        limits=dict(max_depth=8, max_nodes=128, max_branches=16, timeout_sec=30),
        workloads=[run_case(args, case) for case in selected],
    )
    output = json.dumps(report, indent=2)
    if args.output:
        Path(args.output).write_text(output + "\n")
    print(output)


if __name__ == "__main__":
    main()
