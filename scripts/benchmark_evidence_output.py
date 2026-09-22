#!/usr/bin/env python3
"""Measure full/compact public output with a deterministic packed A/D workload.

Generate once, then run each source/format in a fresh process. FSDB may be
converted from that same VCD with the installed converter. No simulation runs,
OS cache flushes or result caches. Timings exclude MCP transport and imports;
opening/indexing is reported separately from first and repeated requests.
"""
from __future__ import annotations

import argparse
import asyncio
from collections import Counter
import hashlib
import json
import os
from pathlib import Path
import resource
import sys
import time


FIELDS = {
    "a_valid": 1, "a_opcode": 3, "a_param": 3, "a_size": 2, "a_source": 8,
    "a_address": 32, "a_mask": 4, "a_data": 32, "a_user": 4, "d_ready": 1,
    "d_valid": 1, "d_opcode": 3, "d_param": 3, "d_size": 2, "d_source": 8,
    "d_sink": 1, "d_data": 32, "d_user": 4, "d_error": 1, "a_ready": 1,
}


def generate(path, count):
    groups = [list(FIELDS)[:10], list(FIELDS)[10:]]
    widths = [sum(FIELDS[key] for key in group) for group in groups]
    fields = {}
    for names, width, bus in zip(groups, widths, ("request", "response")):
        bit = width
        for name in names:
            bit -= FIELDS[name]
            fields[name] = dict(path=f"tb.{bus}[{width-1}:0]", lsb=bit, width=FIELDS[name])
    with path.open("x") as out:
        out.write("$timescale 1ps $end\n$scope module tb $end\n"
                  "$var wire 1 c clk $end\n$var wire 1 r rst_n $end\n")
        for width, symbol, bus in zip(widths, ("a", "d"), ("request", "response")):
            out.write(f"$var wire {width} {symbol} {bus} [{width-1}:0] $end\n")
        out.write("$upscope $end\n$enddefinitions $end\n")
        for i in range(2 * count + 1):
            n = (i - 1) // 2
            values = dict(a_ready=1, d_ready=1)
            if i:
                side = "a" if i % 2 else "d"
                values.update({side + "_valid": 1, side + "_source": n % 256,
                               side + "_data": n, side + "_opcode": 4 if side == "a" else 1})
            out.write(f"#{i*10}\n0c\n{int(i > 0)}r\n")
            for names, symbol in zip(groups, ("a", "d")):
                bits = "".join(format(values.get(key, 0), f"0{FIELDS[key]}b") for key in names)
                out.write(f"b{bits} {symbol}\n")
            out.write(f"#{i*10+5}\n1c\n")
        out.write(f"#{(2*count+1)*10}\n0c\n")
    path.with_suffix(".fields.json").write_text(json.dumps(fields, indent=2) + "\n")


def sha(path):
    return hashlib.sha256(path.read_bytes()).hexdigest()


def main():
    ap = argparse.ArgumentParser(description=__doc__)
    ap.add_argument("--wave", type=Path, required=True)
    ap.add_argument("--generate", action="store_true")
    ap.add_argument("--count", type=int, default=2048)
    ap.add_argument("--cap", type=int, default=32)
    ap.add_argument("--queries", type=int, default=5)
    ap.add_argument("--output-format", choices=("full", "compact"), default="full")
    ap.add_argument("--source-root", type=Path, default=Path(__file__).resolve().parents[1])
    ap.add_argument("--output", type=Path, required=True)
    args = ap.parse_args()
    if not 1 <= args.count <= 32767 or args.queries < 1:
        ap.error("count must be 1..32767 and queries positive")
    if args.generate:
        generate(args.wave, args.count)
        return
    os.environ["TRACEWEAVE_TELEMETRY"] = "0"
    root = args.source_root.resolve()
    sys.path.insert(0, str(root))
    import server

    fields = json.loads(args.wave.with_suffix(".fields.json").read_text())
    request = dict(wave_path=str(args.wave.resolve()), clock="tb.clk", reset="tb.rst_n",
                   fields=fields, start_time_ps=0, end_time_ps=(2*args.count+1)*10,
                   max_transactions=args.cap)
    if args.output_format == "compact":
        request["output_format"] = "compact"
    counts, durations, captured = Counter(), Counter(), []
    started = time.perf_counter()
    parser = server._get_parser(request["wave_path"])
    parser.get_signal_width("tb.clk")
    open_ms = (time.perf_counter() - started) * 1000
    if hasattr(parser, "_lib"):
        for name in ("fsdb_begin_transition_group", "fsdb_end_transition_group",
                     "fsdb_event_open_v1", "fsdb_event_page_v1", "fsdb_event_close_v1",
                     "fsdb_get_transitions_profiled"):
            original = getattr(parser._lib, name)
            def measured(*a, _fn=original, _name=name):
                counts[_name] += 1
                return _fn(*a)
            setattr(parser._lib, name, measured)
    original_dispatch = server._dispatch
    async def dispatch(name, arguments):
        start = time.perf_counter()
        value = await original_dispatch(name, arguments)
        durations["analysis_and_validation_ms"] += (time.perf_counter() - start) * 1000
        captured.append(value)
        return value
    server._dispatch = dispatch
    # Measure the actual projection and JSON encoder independently, without
    # timing a second encoding or a decoded stand-in for the public path.
    if args.output_format == "compact":
        from src import evidence_output
        original_project = evidence_output.selection_projection
        def project(*a):
            start = time.perf_counter()
            value = original_project(*a)
            durations["output_projection_ms"] += (time.perf_counter() - start) * 1000
            return value
        evidence_output.selection_projection = project
    original_dump = server.BaseModel.model_dump_json
    def dump(self, *a, **kw):
        start = time.perf_counter()
        text = original_dump(self, *a, **kw)
        durations["serialization_ms"] += (time.perf_counter() - start) * 1000
        return text
    server.BaseModel.model_dump_json = dump

    results = []
    async def run():
        for index in range(args.queries):
            counts.clear(); durations.clear(); captured.clear()
            start = time.perf_counter()
            contents = await server.call_tool("inspect_tlul", request)
            total = (time.perf_counter() - start) * 1000
            text = contents[0].text
            measurements = dict(durations)
            wire = json.loads(text)
            if args.output_format == "compact":
                from src.evidence_output import expand_compact_result
                result = expand_compact_result(wire)
                assert result == json.loads(original_dump(captured[0], exclude_none=True))
            else:
                result = wire
            assert result["coverage_status"] == "complete", result
            txn = result["transactions"]
            assert result["sample_count"] == 2*args.count+1
            assert result["accepted_a_count"] == result["accepted_d_count"] == txn["matched_count"] == args.count
            assert txn["outstanding_at_end"] == 0 and txn["max_outstanding"] == 1
            assert txn["latency"] == dict(min_cycles=1, median_cycles=1, max_cycles=1, mean_cycles=1.0)
            assert len(txn["transactions"]) == min(args.count, args.cap)
            for i, t in enumerate(txn["transactions"]):
                assert (t["id"], t["request_time_ps"], t["completion_time_ps"]) == (i % 256, 20*i+15, 20*i+25)
            # Volatile timings are excluded only from the cross-run fact digest.
            facts = deepcopy_without_timings(result)
            results.append(dict(index=index, total_ms=total, **measurements,
                output_bytes=len(text.encode()), transaction_rows=len(txn["transactions"]),
                selection_rows=sum(len(v.get("selections", [])) for v in (
                    wire.get("result", wire), *wire.get("result", wire).get("channels", {}).values(),
                    wire.get("result", wire).get("transactions", {}))),
                references=len(wire.get("references", [])), analysis=txn["analysis"],
                native_calls=dict(counts), peak_rss_kib=resource.getrusage(resource.RUSAGE_SELF).ru_maxrss,
                fact_sha256=hashlib.sha256(json.dumps(facts, sort_keys=True).encode()).hexdigest()))
    asyncio.run(run())
    loaded = sorted({line.split()[-1] for line in Path("/proc/self/maps").read_text().splitlines()
                     if "libfsdb_wrapper" in line})
    report = dict(source_root=str(root), python=sys.executable, python_version=sys.version,
                  request=request, input_sha256=sha(args.wave), input_bytes=args.wave.stat().st_size,
                  open_index_ms=open_ms, queries=results, pid=os.getpid(),
                  loaded={p: sha(Path(p)) for p in loaded},
                  sources={p: sha(root/p) for p in ("server.py", "src/schemas.py", "src/tlul.py",
                      "src/txn_reconstruct.py", "src/transaction_sampling.py", "src/evidence_output.py", "fsdb_wrapper.cpp")
                      if (root/p).exists()})
    args.output.write_text(json.dumps(report, indent=2) + "\n")


def deepcopy_without_timings(value):
    if isinstance(value, dict):
        return {k: deepcopy_without_timings(v) for k, v in value.items() if not k.endswith("_ms")}
    if isinstance(value, list):
        return [deepcopy_without_timings(v) for v in value]
    return value


if __name__ == "__main__":
    main()
