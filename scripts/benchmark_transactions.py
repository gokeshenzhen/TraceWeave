#!/usr/bin/env python3
"""Independent transaction workloads; fresh-process A/B via --source-root.

Generation is exclusive-create. FSDB inputs can be made by the installed
vcd2fsdb converter from the same VCD. Measurements exclude conversion and MCP
transport; OS cache is not flushed. Every request checks arithmetic expectations.
"""
from __future__ import annotations

import argparse
from collections import Counter
from itertools import chain
import hashlib
import json
from pathlib import Path
import resource
import sys
import time


WIDTHS = dict(clk=1, rv=1, rr=1, rid=8, cv=1, cr=1, cid=8, last=1,
              length=8, dv=1, dr=1, dl=1, data=64, addr=32)


def cycles(kind, n):
    if kind == "idle":
        yield dict(rv=1, rr=1)
        for _ in range(n - 2):
            yield {}
        yield dict(cv=1, cr=1, last=1)
    elif kind == "outstanding":
        for i in range(n):
            yield dict(rv=1, rr=1, rid=i % 64, addr=i)
        for i in range(n):
            yield dict(cv=1, cr=1, cid=63 - i % 64, last=1)
    else:
        for i in range(n):
            if kind == "write":
                yield dict(dv=1, dr=1, data=2*i)
                yield dict(dv=1, dr=1, dl=1, data=2*i+1)
            yield dict(rv=1, rr=1, rid=i % 64, length=int(kind in ("burst", "write")), addr=i)
            if kind == "burst":
                yield dict(cv=1, cr=1, cid=i % 64)
            yield dict(cv=1, cr=1, cid=i % 64, last=1)


def generate(path, kind, n):
    with path.open("x") as out:
        symbols = {name: "s" + str(i) for i, name in enumerate(WIDTHS)}
        out.write("$timescale 1ps $end\n$scope module tb $end\n")
        for name, width in WIDTHS.items():
            out.write(f"$var wire {width} {symbols[name]} {name} $end\n")
        out.write("$upscope $end\n$enddefinitions $end\n")
        previous = {}
        for index, row in enumerate(cycles(kind, n)):
            out.write(f"#{index*10}\n0{symbols['clk']}\n")
            for name, width in WIDTHS.items():
                if name == "clk":
                    continue
                value = row.get(name, 0)
                if previous.get(name) != value:
                    out.write(f"b{value:0{width}b} {symbols[name]}\n")
                    previous[name] = value
            out.write(f"#{index*10+5}\n1{symbols['clk']}\n")
        out.write(f"#{(index+1)*10}\n0{symbols['clk']}\n")


def expected(kind, n):
    count = 1 if kind == "idle" else n
    maximum = n if kind == "outstanding" else 1
    latency = n-1 if kind == "idle" else 2 if kind == "burst" else 1
    return dict(request_count=count, completion_count=count, matched_count=count,
                max_outstanding=maximum, outstanding_at_end=0,
                reorder_count=n//64*63 if kind == "outstanding" else 0,
                beat_count_mismatch_count=0, unknown_id_beats=0,
                orphan_data_beats=0, unmatched_request_count=0, unmatched_completion_count=0), (
        dict(min_cycles=n-63, max_cycles=n+63, median_cycles=n, mean_cycles=float(n))
        if kind == "outstanding" else dict(min_cycles=latency, max_cycles=latency,
                                           median_cycles=latency, mean_cycles=float(latency)))


def main():
    ap = argparse.ArgumentParser(description=__doc__)
    ap.add_argument("--wave", type=Path, required=True)
    ap.add_argument("--workload", choices=("dense", "idle", "outstanding", "burst", "write"), required=True)
    ap.add_argument("--size", type=int, default=16384)
    ap.add_argument("--generate", action="store_true")
    ap.add_argument("--source-root", type=Path, default=Path(__file__).resolve().parents[1])
    ap.add_argument("--queries", type=int, default=3)
    ap.add_argument("--cap", type=int, default=4)
    ap.add_argument("--output", type=Path)
    args = ap.parse_args()
    if args.size < 64 or (args.workload == "outstanding" and args.size % 64):
        ap.error("size must be >=64 and outstanding size must be a multiple of 64")
    if args.generate:
        generate(args.wave, args.workload, args.size)
        return
    sys.path.insert(0, str(args.source_root.resolve()))
    from src import txn_reconstruct as txn
    from src.vcd_parser import VCDParser
    from src.fsdb_parser import FSDBParser
    from src.schemas import TxnReconstructResult

    counts, timings = Counter(), Counter()
    def timed(owner, name):
        if not hasattr(owner, name):
            return
        fn = getattr(owner, name)
        def run(*a, **kw):
            before = time.perf_counter()
            try:
                return fn(*a, **kw)
            finally:
                timings[name + "_ms"] += (time.perf_counter()-before)*1000
        setattr(owner, name, run)
    for name in ("sample_signals_on_edges", "_walk", "_finalize", "_project"):
        timed(txn, name)
    before = time.perf_counter()
    parser = (VCDParser if args.wave.suffix == ".vcd" else FSDBParser)(str(args.wave.resolve()))
    parser.get_signal_width("tb.clk")
    open_ms = (time.perf_counter()-before)*1000
    report = dict(python=sys.executable, source_root=str(args.source_root.resolve()),
                  waveform_sha256=hashlib.sha256(args.wave.read_bytes()).hexdigest(),
                  input_bytes=args.wave.stat().st_size, workload=args.workload, size=args.size,
                  open_index_ms=open_ms,
                  rss_after_open_kib=resource.getrusage(resource.RUSAGE_SELF).ru_maxrss,
                  measurement="fresh process; parser open separate; OS cache not flushed; no MCP transport",
                  source_sha256=hashlib.sha256(Path(txn.__file__).read_bytes()).hexdigest())
    original = parser.get_transitions
    def read(*a, **kw):
        r = original(*a, **kw)
        counts["transition_calls"] += 1
        counts["returned_events"] += len(r.get("transitions", [])) + bool(r.get("predecessor"))
        counts["decoded_value_bytes"] += sum(len((t.get("value") or {}).get("bin") or "") for t in
            chain((r["predecessor"],) if r.get("predecessor") else (), r.get("transitions", [])))
        return r
    parser.get_transitions = read
    if isinstance(parser, FSDBParser):
        lib = parser._lib
        report["native_sha256"] = hashlib.sha256(Path(lib._name).read_bytes()).hexdigest()
        report["native_loaded"] = [line for line in Path("/proc/self/maps").read_text().splitlines() if "libfsdb_wrapper" in line]
        for name in ("fsdb_get_transitions", "fsdb_get_transitions_profiled", "fsdb_get_loaded_transitions", "fsdb_get_value_at_time", "fsdb_event_open_v1", "fsdb_event_page_v1", "fsdb_event_close_v1", "fsdb_begin_transition_group", "fsdb_end_transition_group"):
            if not hasattr(lib, name):
                continue
            fn = getattr(lib, name)
            def call(*a, _fn=fn, _name=name):
                counts[_name] += 1
                result = _fn(*a)
                if _name in ("fsdb_get_transitions_profiled", "fsdb_get_loaded_transitions"):
                    profile = a[-1]._obj
                    counts["native_output_bytes"] += profile.output_bytes
                    for phase in ("lookup", "load", "seek", "traverse_format", "unload"):
                        timings["native_" + phase + "_ms"] += getattr(profile, phase + "_ns") / 1_000_000
                elif _name == "fsdb_event_page_v1":
                    counts["native_output_bytes"] += a[-2]._obj.output_bytes
                return result
            setattr(lib, name, call)
    common = dict(get_parser=lambda _: parser, wave_path=str(args.wave.resolve()), clock="tb.clk",
                  req_valid="tb.rv", req_ready="tb.rr", req_id="tb.rid", cmp_valid="tb.cv",
                  cmp_ready="tb.cr", cmp_id="tb.cid", req_len="tb.length", req_fields=["tb.addr"],
                  max_transactions=args.cap, timeout_cycles=8)
    if args.workload == "write":
        common.update(data_valid="tb.dv", data_ready="tb.dr", data_last="tb.dl", data_fields=["tb.data"], capture_beats=True)
    else:
        common["cmp_last"] = "tb.last"
    exp, latency = expected(args.workload, args.size)
    report["requests"] = []
    for index in range(args.queries):
        counts.clear(); timings.clear()
        before = time.perf_counter()
        result = txn.reconstruct_transactions(**common)
        elapsed = (time.perf_counter()-before)*1000
        assert {k:result[k] for k in exp} == exp
        assert result["latency"] == latency, result["latency"]
        assert len(result["transactions"]) == min(args.cap, exp["matched_count"])
        assert all(t["beat_count"] == (2 if args.workload in ("burst", "write") else 1) for t in result["transactions"])
        public = TxnReconstructResult.model_validate(result).model_dump()
        before_serialize = time.perf_counter()
        encoded = json.dumps(result, sort_keys=True).encode()
        serialize_ms = (time.perf_counter() - before_serialize)*1000
        # Compare the public contract: its mean is a float even when an older
        # internal accumulator emits an exactly integral Python value.
        actual_facts = {**{k: public[k] for k in exp}, "latency": public["latency"],
                        "transactions": public["transactions"]}
        report["requests"].append(dict(index=index, total_ms=elapsed, phases_ms=dict(timings),
            reads=dict(counts), analysis=result.get("analysis"), output_bytes=len(encoded),
            serialize_ms=serialize_ms, facts=actual_facts,
            facts_sha256=hashlib.sha256(json.dumps(actual_facts, sort_keys=True).encode()).hexdigest(),
            peak_rss_kib=resource.getrusage(resource.RUSAGE_SELF).ru_maxrss))
    if hasattr(parser, "close"):
        parser.close()
    text = json.dumps(report, indent=2)
    if args.output:
        args.output.write_text(text+"\n")
    print(text)


if __name__ == "__main__":
    main()
