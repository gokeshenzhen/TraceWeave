#!/usr/bin/env python3
"""Replay frozen requests through call_tool in isolated real server processes.

No backend is replaced. Raw requests/responses, CPU, capture/validation counters
and sampled process-tree RSS are private local evidence. OS caches are not cold.
"""
from __future__ import annotations

import argparse
import asyncio
from contextvars import ContextVar
from collections import defaultdict, deque
import hashlib
import threading
import signal
import json
import math
import os
from pathlib import Path
import platform
import resource
import statistics
import subprocess
import sys
import time


def python_command(path):
    # Resolving a venv's symlink selects the system site-packages instead.
    return str(Path(path).expanduser().absolute())


def server_directory(workload):
    return str(Path(workload.get("server_cwd") or Path(workload["compile_log"]).parent).absolute())


def decode_response(response):
    blocks = [c.model_dump(mode="json") if hasattr(c, "model_dump") else
              {"type": getattr(c, "type", "text"), "text": getattr(c, "text", "")}
              for c in response.content]
    raw = getattr(response, "structuredContent", None)
    if raw is None:
        try:
            raw = json.loads(next(c.text for c in response.content if getattr(c, "type", "text") == "text"))
        except (ValueError, StopIteration):
            raw = {"error_code": "non_json_mcp_response", "raw_mcp_content": blocks}
    if not isinstance(raw, dict):
        raw = {"error_code": "unexpected_mcp_response_shape", "raw_mcp_content": blocks}
    if response.isError:
        raw = {**raw, "transport_is_error": True}
    return raw


def rss_tree(pid):
    pending, total, observed = [pid], 0, False
    while pending:
        current = pending.pop()
        try:
            for line in Path(f"/proc/{current}/status").read_text().splitlines():
                if line.startswith("VmRSS:"):
                    total += int(line.split()[1]) * 1024
                    observed = True
            pending.extend(map(int, Path(f"/proc/{current}/task/{current}/children").read_text().split()))
        except (OSError, ValueError):
            pass
    return total if observed else None


def serve(args):
    sys.path.insert(0, str(Path(args.repo).resolve()))
    import server
    from mcp import types
    from src import design_context_runtime as contexts
    from src.compile_input_capture import CompileInputCapture
    from src.design_context import DesignContext
    from src.source_graph_runtime import IsolatedSourceGraphProcessRunner
    original_runner_init = IsolatedSourceGraphProcessRunner.__init__
    def common_worker_directory(owner, *a, **kw):
        kw.setdefault("working_directory", os.getcwd())
        original_runner_init(owner, *a, **kw)
    IsolatedSourceGraphProcessRunner.__init__ = common_worker_directory
    active, phase = ContextVar("benchmark_call", default=None), ContextVar("benchmark_phase", default="other")
    lock = threading.Lock()
    observed_keys = ("source_prepare_count", "source_current_count", "capture_read_bytes",
        "source_capture_read_bytes", "source_validation_read_bytes", "metadata_read_bytes", "other_read_bytes",
        "actual_build_count", "frontend_launch_count", "disk_hit_count", "artifact_cache_hit_count",
        "npi_load_generation_events")
    totals, records = defaultdict(int, dict.fromkeys(observed_keys, 0)), []

    def bump(key, amount=1):
        with lock:
            totals[key] += amount
            row = active.get()
            if row is not None:
                row[key] = row.get(key, 0) + amount

    original_prepare, original_current, original_read = contexts.prepare_design_context, DesignContext.current, CompileInputCapture._read
    def prepare(*a, **kw):
        bump("source_prepare_count")
        token = phase.set("source_capture")
        try:
            return original_prepare(*a, **kw)
        finally:
            phase.reset(token)
    def current(*a, **kw):
        bump("source_current_count")
        token = phase.set("source_validation")
        try:
            return original_current(*a, **kw)
        finally:
            phase.reset(token)
    def read(owner, *a, **kw):
        before = owner.read_bytes_count
        try:
            return original_read(owner, *a, **kw)
        finally:
            count = owner.read_bytes_count - before
            bump("capture_read_bytes", count)
            role = ("metadata" if type(owner).__module__ == "src.native_query" else phase.get())
            bump(role + "_read_bytes", count)
    contexts.prepare_design_context, DesignContext.current, CompileInputCapture._read = prepare, current, read
    original_record = server._record_source_graph_prepare_metrics
    def record(outcome):
        for key in ("actual_build_count", "frontend_launch_count", "disk_hit_count"):
            bump(key, getattr(outcome.metrics, key, 0))
        bump("artifact_cache_hit_count", int(getattr(outcome.metrics.cache_tier, "value", "") in {"memory", "disk"}))
        return original_record(outcome)
    server._record_source_graph_prepare_metrics = record
    original_handler = server.app.request_handlers[types.CallToolRequest]
    measured = Path(args.output_dir)
    generation = 0
    async def handler(request):
        nonlocal generation
        counters = dict.fromkeys(observed_keys, 0)
        token = active.set(counters)
        wall, cpu = time.perf_counter(), time.process_time()
        try:
            return await original_handler(request)
        finally:
            active.reset(token)
            module = sys.modules.get("src.verdi_npi_backend")
            observed = sum(getattr(module, "_NPI_LOAD_GENERATIONS", {}).values())
            counters["npi_load_generation_events"] = observed - generation
            totals["npi_load_generation_events"] += observed - generation
            generation = observed
            records.append(dict(tool=request.params.name, arguments=request.params.arguments or {},
                counters=counters, wall_ms=(time.perf_counter() - wall) * 1000,
                cpu_ms=(time.process_time() - cpu) * 1000))
            payload = dict(counters=dict(totals), calls=records, python=sys.version,
                python_executable=sys.executable, server_cwd=os.getcwd(),
                platform=platform.platform(), source_graph_config=repr(server.get_source_graph_execution_config()),
                context_store_metadata_bytes=server._design_context_store._bytes,
                self_peak_rss_bytes=resource.getrusage(resource.RUSAGE_SELF).ru_maxrss * 1024)
            temporary = measured.with_suffix(".tmp")
            temporary.write_text(json.dumps(payload, indent=2))
            temporary.replace(measured)
    server.app.request_handlers[types.CallToolRequest] = handler
    asyncio.run(server.main())


async def worker(args):
    from mcp import ClientSession, StdioServerParameters
    from mcp.client.stdio import stdio_client
    import anyio
    measurement = str(Path(args.output_dir).with_suffix(".server.json"))
    workload = json.loads(Path(args.workload).read_text())
    environment = {**os.environ, **workload.get("environment", {})}
    environment["PYTHONPATH"] = str(Path(args.repo).resolve()) + os.pathsep + environment.get("PYTHONPATH", "")
    params = StdioServerParameters(command=sys.executable, args=[str(Path(__file__).resolve()),
        "--stdio-server", "--repo", str(Path(args.repo).resolve()), "--output-dir", measurement],
        cwd=server_directory(workload), env=environment)
    async with stdio_client(params) as (reader, writer):
        async with ClientSession(reader, writer, read_timeout_seconds=__import__("datetime").timedelta(seconds=150)) as session:
            await session.initialize()
            result = await perform(args, session)
    observed = json.loads(Path(measurement).read_text())
    calls = defaultdict(deque)
    for row in observed.pop("calls"):
        calls[(row["tool"], json.dumps(row["arguments"], sort_keys=True))].append(row)
    for query in [*result["prelude"], *(q for sample in [*result.get("warmup", []), *result["samples"]] for q in sample["queries"])]:
        bucket = calls[(query["tool"], json.dumps(query["arguments"], sort_keys=True))]
        if bucket:
            row = bucket.popleft()
            query.update(server_wall_ms=row["wall_ms"], cpu_ms=row["cpu_ms"], counters=row["counters"])
        else:
            query.update(server_wall_ms=None, cpu_ms=None, counters=None,
                         measurement_status="server_observer_not_reached")
    result.update(observed, license_checkout_count=None, license_release_count=None,
        cache_condition="OS page cache uncontrolled; fresh means a new stdio server process")
    return result


async def perform(args, session):
    workload = json.loads(Path(args.workload).read_text())
    common = {k: workload[k] for k in ("compile_log", "supplementary_compile_logs", "simulator",
        "top_hint", "kdb_path", "backend_preference") if k in workload}
    journal = []
    journal_ms = 0.0
    journal_path = Path(args.output_dir).with_suffix(".calls.json")
    async def call(tool, arguments):
        nonlocal journal_ms
        wall, cpu = time.perf_counter(), time.process_time()
        response = await session.call_tool(tool, arguments)
        elapsed, used = (time.perf_counter() - wall) * 1000, (time.process_time() - cpu) * 1000
        raw = decode_response(response)
        if raw.get("format") in {"traceweave.compact.v1", "traceweave.driver.compact.v1"}:
            result = raw["result"]
        else:
            result = raw
        row = dict(tool=tool, arguments=arguments, response=raw, wall_ms=elapsed, cpu_ms=used,
            counters={},
            ok=not response.isError and not result.get("error") and not result.get("error_code") and result.get("ok", True) is not False)
        journal.append(row)
        journal_started = time.perf_counter()
        temporary = journal_path.with_suffix(".tmp")
        temporary.write_text(json.dumps(journal, indent=2))
        temporary.replace(journal_path)
        journal_ms += (time.perf_counter() - journal_started) * 1000
        return row

    prelude_started, prelude_cpu = time.perf_counter(), time.process_time()
    prelude = []
    if workload.get("discovery"):
        prelude.append(await call("get_sim_paths", workload["discovery"]))
    prep = None
    if ((args.variant == "baseline" and not workload.get("baseline_native_first"))
            or workload.get("explicit_source")):
        # Preserve the old parallel prepare/scan workload, including overlap.
        if workload.get("scan"):
            prep, scan = await asyncio.gather(call("prepare_design_context", common),
                call("scan_structural_risks", workload["scan"]))
            prelude.extend([prep, scan])
        else:
            prep = await call("prepare_design_context", common)
            prelude.append(prep)
        if not prep["ok"]:
            return dict(prelude=prelude, samples=[], error="baseline_preparation_failed")
        handle = prep["response"]["design_handle"]
    else:
        handle = None
        if workload.get("scan"):
            prelude.append(await call("scan_structural_risks", workload["scan"]))
    for query in workload.get("investigation", []):
        prelude.append(await call(query["tool"], query["arguments"]))
    prelude_observed_wall_ms = (time.perf_counter() - prelude_started) * 1000
    prelude_wall_ms = prelude_observed_wall_ms - journal_ms
    samples = []
    warmup_count = int(args.warmup)
    for repetition in range(args.repeats + warmup_count):
        rows = []
        journal_before = journal_ms
        wall, cpu = time.perf_counter(), time.process_time()
        for query in workload["queries"]:
            business = dict(query["arguments"])
            business.setdefault("output_format", "full")
            # output_format is supported only by the listed output families.
            if query["tool"] not in {"explain_signal_driver", "trace_x_source", "resolve_packed_fields", "trace_divergence"}:
                business.pop("output_format", None)
            arguments = ({**business, "design_handle": handle} if handle else {**common, **business})
            if not handle and workload.get("candidate_kdb_only"):
                arguments = {k: v for k, v in arguments.items() if k not in {"compile_log", "supplementary_compile_logs"}}
            observed = await call(query["tool"], arguments)
            facts = observed["response"].get("result", observed["response"]) if observed["response"].get("format") else observed["response"]
            checks = []
            for pointer, expected in query.get("expected", {}).items():
                actual = facts
                try:
                    for key in pointer.strip("/").split("/"):
                        actual = actual[int(key)] if isinstance(actual, list) else actual[key]
                    matched = actual == expected
                except (KeyError, IndexError, TypeError, ValueError):
                    actual, matched = None, False
                checks.append(dict(pointer=pointer, expected=expected, actual=actual, matched=matched))
            observed["oracle"] = checks
            observed["ok"] &= all(c["matched"] for c in checks)
            rows.append(observed)
        observed_wall_ms = (time.perf_counter() - wall) * 1000
        observer_journal_ms = journal_ms - journal_before
        samples.append(dict(repetition=repetition, queries=rows,
            wall_ms=observed_wall_ms - observer_journal_ms, observed_wall_ms=observed_wall_ms,
            observer_journal_ms=observer_journal_ms, cpu_ms=(time.process_time() - cpu) * 1000))
    return dict(prelude=prelude, warmup=samples[:warmup_count], samples=samples[warmup_count:],
        prelude_wall_ms=prelude_wall_ms, prelude_observed_wall_ms=prelude_observed_wall_ms)



# Only these deliberate source-observation/profiling differences are normalized.
# KDB/artifact identities, semantic facts, limits and global coverage stay exact.
NORMALIZATIONS = ["native_route", "backend_status.design_context", "design_handle",
    "root context_identity_sha256 (healthy NPI only)", "profiling timings ending _ms",
    "row location-binding gaps (reported separately, without source association)"]
LOCATION_GAPS = {"native_source_content_unverified", "npi_instance_location_unbound",
                 "npi_definition_location_unbound", "npi_class_location_unbound"}


def normalized(response):
    if response.get("format"):
        response = response["result"]
    healthy = response.get("backend_status", {}).get("actual_backend") == "verdi_npi"
    def visit(value, path=()):
        if isinstance(value, list):
            return [visit(v, path) for v in value if not (path and path[-1] == "gaps" and isinstance(v, str) and v in LOCATION_GAPS)]
        if not isinstance(value, dict):
            return value
        return {k: visit(v, (*path, k)) for k, v in value.items()
                if k not in {"native_route", "design_handle", "payload_bytes"}
                and not k.endswith("_ms")
                and not (path == ("backend_status",) and k == "design_context")
                and not (not path and healthy and k == "context_identity_sha256")}
    return visit(response)


def difference_paths(left, right, path=""):
    if type(left) is not type(right):
        return [path]
    if isinstance(left, dict):
        return [p for key in sorted(left.keys() | right.keys()) for p in (
            [path + "/" + key] if key not in left or key not in right else
            difference_paths(left[key], right[key], path + "/" + key))][:100]
    if isinstance(left, list):
        if len(left) != len(right):
            return [path + "/length"]
        return [p for index, (a, b) in enumerate(zip(left, right))
                for p in difference_paths(a, b, path + "/" + str(index))][:100]
    return [] if left == right else [path]


def distribution(values):
    values = sorted(values)
    if not values:
        return None
    return dict(n=len(values), median=statistics.median(values), min=values[0], max=values[-1],
                empirical_p95=values[max(0, math.ceil(.95 * len(values)) - 1)])


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    for field in ("baseline-repo", "candidate-repo", "python", "workload", "output-dir"):
        parser.add_argument("--" + field)
    parser.add_argument("--fresh-repeats", type=int, default=5)
    parser.add_argument("--warm-repeats", type=int, default=10)
    parser.add_argument("--sample-ms", type=int, default=100)
    parser.add_argument("--worker", action="store_true")
    parser.add_argument("--stdio-server", action="store_true")
    parser.add_argument("--repo")
    parser.add_argument("--variant", choices=("baseline", "candidate"))
    parser.add_argument("--repeats", type=int, default=1)
    parser.add_argument("--warmup", action="store_true")
    args = parser.parse_args()
    if args.stdio_server:
        serve(args)
        return
    if args.worker:
        result = asyncio.run(worker(args))
        Path(args.output_dir).write_text(json.dumps(result, indent=2))
        return
    for required in ("baseline_repo", "candidate_repo", "python", "workload", "output_dir"):
        if not getattr(args, required):
            parser.error("--" + required.replace("_", "-") + " is required")
    if min(args.fresh_repeats, args.warm_repeats, args.sample_ms) < 1:
        parser.error("repeat and sampling limits must be positive")
    output = Path(args.output_dir).resolve()
    output.mkdir(parents=True, exist_ok=False, mode=0o700)
    summary = {"workload": json.loads(Path(args.workload).read_text()), "runs": [],
        "measurement_conditions": {"cpu_count": os.cpu_count(), "platform": platform.platform(),
            "start_loadavg": Path("/proc/loadavg").read_text().strip(), "sample_ms": args.sample_ms,
            "os_page_cache": "uncontrolled", "warmup_sequences": 1}}
    for variant, repo in (("baseline", args.baseline_repo), ("candidate", args.candidate_repo)):
        repo = str(Path(repo).resolve())
        frozen = Path(repo) / ".frozen-baseline.json"
        if frozen.exists():
            state = json.loads(frozen.read_text())
            summary[variant + "_head"], summary[variant + "_dirty"] = state["head"], state["status"]
        else:
            summary[variant + "_head"] = subprocess.check_output(["git", "rev-parse", "HEAD"], cwd=repo, text=True).strip()
            summary[variant + "_dirty"] = subprocess.check_output(["git", "status", "--porcelain"], cwd=repo, text=True)
        digest = hashlib.sha256()
        for code in sorted([*Path(repo).glob("src/*.py"), Path(repo) / "server.py", Path(repo) / "config.py"]):
            digest.update(str(code.relative_to(repo)).encode() + b"\0" + code.read_bytes())
        summary[variant + "_code_sha256"] = digest.hexdigest()
        for index in range(args.fresh_repeats + 1):
            shape = "warm" if index == args.fresh_repeats else "fresh"
            label = f"{variant}-{shape}-{index}"
            result_file = output / (label + ".json")
            command = [python_command(args.python), str(Path(__file__).resolve()), "--worker",
                "--repo", repo, "--variant", variant, "--workload", str(Path(args.workload).resolve()),
                "--output-dir", str(result_file), "--repeats", str(args.warm_repeats if shape == "warm" else 1)]
            if shape == "warm":
                command.append("--warmup")
            samples = []
            start = time.perf_counter()
            with (output / (label + ".log")).open("w") as log:
                process = subprocess.Popen(command, cwd=repo, stdout=log, stderr=log, start_new_session=True)
                watchdog = False
                while process.poll() is None:
                    if time.perf_counter() - start > 180:
                        watchdog = True
                        os.killpg(process.pid, signal.SIGTERM)
                        try:
                            process.wait(timeout=2)
                        except subprocess.TimeoutExpired:
                            os.killpg(process.pid, signal.SIGKILL)
                            process.wait(timeout=2)
                        break
                    samples.append({"elapsed_ms": (time.perf_counter() - start) * 1000,
                                    "tree_rss_bytes": rss_tree(process.pid)})
                    time.sleep(args.sample_ms / 1000)
            result = json.loads(result_file.read_text()) if result_file.exists() else {}
            journal = result_file.with_suffix(".calls.json")
            if not result and journal.exists():
                result["partial_calls"] = json.loads(journal.read_text())
            result.update(command=command, exit_status=process.returncode, watchdog=watchdog, rss_samples=samples,
                          process_wall_ms=(time.perf_counter() - start) * 1000)
            result_file.write_text(json.dumps(result, indent=2))
            summary["runs"].append(dict(variant=variant, shape=shape, file=result_file.name,
                exit_status=process.returncode, error=result.get("error"),
                all_queries_ok=bool(result.get("samples")) and all(q["ok"] for row in result.get("samples", []) for q in row["queries"])))
    summary["latency"] = {}
    for variant in ("baseline", "candidate"):
        for shape in ("fresh", "warm"):
            values = []
            for row in summary["runs"]:
                if row["variant"] != variant or row["shape"] != shape:
                    continue
                run = json.loads((output / row["file"]).read_text())
                for sample in run.get("samples", []):
                    values.append(sample["wall_ms"] + (run["prelude_wall_ms"]
                        if shape == "fresh" else 0))
            summary["latency"][variant + "_" + shape + "_ms"] = distribution(values)
    summary["normalizations"] = NORMALIZATIONS
    summary["comparisons"] = []
    baseline_rows = [row for row in summary["runs"] if row["variant"] == "baseline"]
    candidate_rows = [row for row in summary["runs"] if row["variant"] == "candidate"]
    for before, after in zip(baseline_rows, candidate_rows):
        baseline = json.loads((output / before["file"]).read_text())
        candidate = json.loads((output / after["file"]).read_text())
        for index, (a, b) in enumerate(zip(baseline.get("samples", []), candidate.get("samples", []))):
            for query, (x, y) in enumerate(zip(a["queries"], b["queries"])):
                differences = difference_paths(normalized(x["response"]), normalized(y["response"]))
                summary["comparisons"].append(dict(shape=before["shape"], repetition=index,
                    query=query, tool=x["tool"], semantic_and_coverage_equal=not differences,
                    difference_paths=differences))
    summary["memory"] = {}
    for variant in ("baseline", "candidate"):
        peaks = []
        for row in summary["runs"]:
            if row["variant"] == variant:
                run = json.loads((output / row["file"]).read_text())
                observed = [sample["tree_rss_bytes"] for sample in run["rss_samples"] if sample["tree_rss_bytes"] is not None]
                if observed:
                    peaks.append(max(observed))
        summary["memory"][variant + "_sampled_tree_peak_bytes"] = distribution(peaks)
    summary["execution_passed"] = all(r["exit_status"] == 0 and r["all_queries_ok"] for r in summary["runs"])
    summary["strict_comparison_passed"] = bool(summary["comparisons"]) and all(c["semantic_and_coverage_equal"] for c in summary["comparisons"])
    summary["measurement_conditions"]["end_loadavg"] = Path("/proc/loadavg").read_text().strip()
    (output / "summary.json").write_text(json.dumps(summary, indent=2))
    print(json.dumps({"output": str(output), "latency": summary["latency"], "runs": summary["runs"], "semantic_mismatches": sum(not c["semantic_and_coverage_equal"] for c in summary["comparisons"])}, indent=2))
    if not summary["execution_passed"]:
        raise SystemExit(1)


if __name__ == "__main__":
    main()
