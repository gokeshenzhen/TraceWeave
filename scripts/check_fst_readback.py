#!/usr/bin/env python3
"""Reproduce FST reading acceptance through a fresh stdio MCP process.

Uses synthetic event tables with independent expectations, not simulator logs.
Optional FSDB comparison uses the repository's existing cross-scale fixture;
no simulation, client configuration changes, or dependency installation occurs.
"""
import argparse
import hashlib
import json
import os
from pathlib import Path
import subprocess
import sys
import tempfile
import time

import anyio
from mcp import ClientSession, StdioServerParameters
from mcp.client.stdio import stdio_client

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))
sys.path.insert(0, str(ROOT / "tests"))
from fst_fixture import write_fst
from fst_analysis_readback import validate_analyses


def fingerprint(path):
    return {"path": str(path), "bytes": path.stat().st_size,
            "sha256": hashlib.sha256(path.read_bytes()).hexdigest()}


async def run_check(work, installed, require_fsdb):
    work.mkdir(parents=True, exist_ok=True)
    rows = [(5, "data [3:0]", "10xz"), (10, "data [3:0]", "0101"),
            (11, "data [3:0]", "zz00"), (11, "data [3:0]", "1111"),
            (20, "data [3:0]", "0000"), (30, "data [3:0]", "1010")]
    wave = write_fst(work / "basic.fst", rows, start=5, end=35,
        activity=[(22, 0), (26, 1)], declarations=[
            ("data [3:0]", 4, "wire", "input", None),
            ("alias [0:3]", 4, "wire", "output", "data [3:0]")])
    physical = write_fst(work / "physical.fst", [(0, "a", "0"), (10, "a", "1"),
        (11, "a", "x"), (12, "a", "z"), (20, "a", "0")], scale=-13)
    vcd = work / "basic.vcd"
    vcd.write_text("$timescale 1ps $end\n$scope module top $end\n$var wire 4 ! data [3:0] $end\n"
                   "$upscope $end\n$enddefinitions $end\n" +
                   "".join(f"#{t}\nb{value} !\n" for t, _, value in rows) + "#35\n")
    bootstrap = work / "launch.py"
    loaded = work / "loaded.json"
    # Capture module identities INSIDE the child that serves the actual calls.
    # All receipt strings are Python literals, never interpolated shell text.
    bootstrap.write_text(
        "import asyncio, hashlib, json, sys\nfrom pathlib import Path\n" +
        ("from traceweave_mcp._runtime import server\n" if installed else
         f"sys.path.insert(0, {str(ROOT)!r})\nimport server\n") +
        "base = Path(server.__file__).resolve().parent\n"
        "before = 'pylibfst' in sys.modules\n"
        "def capture():\n"
        " paths = {Path(m.__file__).resolve() for m in list(sys.modules.values()) if getattr(m, '__file__', None) and str(m.__file__).endswith('.py')}\n"
        " paths = {p for p in paths if p.parent == base or base / 'src' in p.parents}\n"
        " paths.add(base / 'src' / 'fst_worker.py')\n"
        " receipt = {'python': sys.executable, 'native_imported_before_query': before, 'native_imported_after_query': 'pylibfst' in sys.modules,\n"
        "  'modules': [{'path': str(p), 'sha256': hashlib.sha256(p.read_bytes()).hexdigest()} for p in sorted(paths)]}\n" +
        f" Path({str(loaded)!r}).write_text(json.dumps(receipt))\n" +
        "original = server._dispatch\n"
        "async def observed(*args, **kwargs):\n"
        " try: return await original(*args, **kwargs)\n"
        " finally: capture()\n"
        "server._dispatch = observed\ncapture()\n" +
        "asyncio.run(server.main())\n")
    env = {**os.environ, "TRACEWEAVE_TELEMETRY": "0", "TRACEWEAVE_AUTO_KDB": "0",
           "TRACEWEAVE_CACHE_DIR": str(work / "cache"),
           "TRACEWEAVE_CONNECTIVITY_ROUTE": "source_graph", "TRACEWEAVE_SOURCE_GRAPH_PYTHON": sys.executable,
           "TRACEWEAVE_HIERARCHY_NPI_SOURCE_OVERLAY": "off"}
    env.pop("PYTHONPATH", None)
    params = StdioServerParameters(command=sys.executable, args=[str(bootstrap)], cwd=work, env=env)
    report = {"status": "running", "installed": installed, "calls": [],
              "fixtures": [fingerprint(p) for p in (wave, physical, vcd)],
              "compile_hierarchy_scan_log_sweep": "not_run: synthetic fixtures without compile/simulation logs",
              "scope": "full FST digital reading and analysis matrix",
              "mcp_round_trip_includes_module_fingerprinting": True}
    report["source_commit"] = subprocess.check_output(["git", "-C", str(ROOT), "rev-parse", "HEAD"], text=True).strip()
    report["source_dirty"] = bool(subprocess.check_output(["git", "-C", str(ROOT), "status", "--porcelain"], text=True).strip())
    async with stdio_client(params) as (reader, writer):
        async with ClientSession(reader, writer) as session:
            with anyio.fail_after(180):
                initialized = await session.initialize()
                catalog = await session.list_tools()
                report["server"] = initialized.serverInfo.model_dump()
                report["tool_count"] = len(catalog.tools)
                report["loaded"] = json.loads(loaded.read_text())
                assert not report["loaded"]["native_imported_before_query"]

                async def call(tool, arguments):
                    started = time.perf_counter()
                    reply = await session.call_tool(tool, arguments)
                    text = next(c.text for c in reply.content if c.type == "text")
                    body = json.loads(text)
                    report["calls"].append({"tool": tool, "arguments": arguments, "result": body,
                        "mcp_round_trip_ms": (time.perf_counter() - started) * 1000,
                        "json_bytes": len(text.encode())})
                    if str(body.get("format", "")).startswith("traceweave."):
                        from src.evidence_output import expand_compact_result
                        return expand_compact_result(text)
                    return body

                paths = await call("get_sim_paths", {"verif_root": str(work), "wave_file": str(wave)})
                assert paths["wave_files"][0]["format"] == "fst" and paths["fst_runtime"]["enabled"]
                summary = await call("get_waveform_summary", {"wave_path": str(wave)})
                assert summary["format"] == "FST" and summary["total_signals"] == 2
                assert summary["fst_backend"]["storage_count"] == 1
                report["native"] = summary["fst_backend"]
                search = await call("search_signals", {"wave_path": str(wave), "keyword": "data"})
                assert search["results"][0]["path"] == "top.data[3:0]"
                for at, bits in ((5, "10xz"), (10, "0101"), (11, "1111"), (20, "0000"), (30, "1010")):
                    for source in (wave, vcd):
                        point = await call("get_signal_at_time", {"wave_path": str(source), "signal_path": "top.data", "time_ps": at})
                        # Legacy VCD retains its vector 'b' prefix in bin.
                        actual = point["value"]["bin"]
                        if source == vcd:
                            actual = actual.removeprefix("b")
                        assert actual == bits, (source, at, point)
                for at, status in ((0, "outside_recorded_range"), (24, "dump_inactive"), (27, "unrecorded_after_dump_resume")):
                    point = await call("get_signal_at_time", {"wave_path": str(wave), "signal_path": "top.data", "time_ps": at})
                    assert point.get("value") is None and point["value_status"] == status
                    assert point["fst_reading"]["coverage_status"] == "partial"
                selected = {"path": "top.alias", "bits": [3, 0]}
                selection = await call("get_signal_at_time", {"wave_path": str(wave), "signal_path": selected, "time_ps": "5ps"})
                assert selection["value"]["bin"] == "z1"
                transition_args = {"wave_path": str(wave), "signal_path": "top.data", "start_time_ps": 10, "end_time_ps": 20}
                transitions = await call("get_signal_transitions", transition_args)
                assert [(r["time_ps"], r["value"]["bin"]) for r in transitions["transitions"]] == [
                    (10, "0101"), (11, "zz00"), (11, "1111"), (20, "0000")]
                assert transitions["initial_state"]["time_ps"] == 5 and "predecessor" not in transitions
                compact = await call("get_signal_transitions", {**transition_args, "output_format": "compact"})
                assert compact == transitions
                capped = await call("get_signal_transitions", {**transition_args, "max_transitions": 1})
                assert capped["truncated"] and capped["transition_count"] == 4
                assert capped["fst_reading"] == transitions["fst_reading"]
                around_args = {"wave_path": str(wave), "signal_paths": [selected], "center_time_ps": 11, "window_ps": 1}
                around = await call("get_signals_around_time", around_args)
                assert next(iter(around["signals"].values()))["value_at_center"]["bin"] == "11"
                assert await call("get_signals_around_time", {**around_args, "output_format": "compact"}) == around
                narrow = await call("get_signal_transitions", {"wave_path": str(physical), "signal_path": "top.a", "start_time_ps": 2, "end_time_ps": 2})
                assert [r["time_fs"] for r in narrow["transitions"]] == [2000]
                assert narrow["predecessor"]["time_fs"] == 1200 and narrow["predecessor"]["time_ps"] == 2
                cycles = await call("get_signals_by_cycle", {"wave_path": str(physical), "clock_path": "top.a",
                    "signal_paths": ["top.a"], "sample_phase": "before", "num_cycles": 1})
                assert cycles['cycles'][0]['signals']['top.a']['dec'] == 0
                assert cycles['clock_edges_complete'] is False  # later X interrupts the clock prefix
                expression = await call("get_signal_at_time", {"wave_path": str(wave), "time_ps": 10,
                    "signal_path": {"expr": "a", "bindings": {"a": "top.data"}, "typing": "wave_bits"}})
                assert expression['value']['bin'] == '0101'

                await validate_analyses(call, work, report, fingerprint)
                observed_tools = {r['tool'] for r in report['calls']}
                assert set(summary['fst_backend']['supported_tools']) <= observed_tools

                # Independent oracle already embodied by scale_100fs_tb.v;
                # compare recorded digital observations, not analyzer output.
                fsdb_source = ROOT / "tests/fixtures/scale_100fs.fsdb"
                report["fsdb_comparison"] = "not_run: runtime or fixture unavailable"
                if not installed and paths["fsdb_runtime"]["enabled"] and fsdb_source.exists():
                    fsdb = work / "scale_100fs.fsdb"
                    fsdb.write_bytes(fsdb_source.read_bytes())
                    matched = write_fst(work / "matched.fst", [(t, "addr [31:0]", f"{v:032b}") for t, v in (
                        (0, 0), (1000000, 0xAAAA0000), (1001000, 0xBBBB0000), (1001005, 0xCCCC0000))],
                        declarations=[("addr [31:0]", 32, "reg", "implicit", None)], scale=-13, end=1100000)
                    await call("get_sim_paths", {"verif_root": str(work), "wave_file": str(fsdb)})
                    await call("get_waveform_summary", {"wave_path": str(fsdb)})
                    for at, expected in ((99999, 0), (100000, 0xAAAA0000), (100100, 0xBBBB0000), (100101, 0xCCCC0000)):
                        for source, signal in ((fsdb, "scale_100fs_tb.addr[31:0]"), (matched, "top.addr")):
                            point = await call("get_signal_at_time", {"wave_path": str(source), "signal_path": signal, "time_ps": at})
                            assert point["value"]["dec"] == expected
                    report["fixtures"].extend(fingerprint(p) for p in (fsdb, matched))
                    for a, sa, b, sb in ((fsdb, 'scale_100fs_tb.addr[31:0]', matched, 'top.addr'),
                                          (matched, 'top.addr', fsdb, 'scale_100fs_tb.addr[31:0]')):
                        compared = await call('diff_first_divergence', dict(wave_path_a=str(a), signal_a=sa,
                            wave_path_b=str(b), signal_b=sb, start_time_ps=99999, end_time_ps=100102))
                        assert compared['comparison_status'] == 'equal' and compared['coverage_status'] == 'complete'
                    report["fsdb_comparison"] = "passed: independent point values and exact event-page comparison in both directions"
                if require_fsdb:
                    assert report["fsdb_comparison"].startswith("passed")
    report['loaded'] = json.loads(loaded.read_text())
    assert not report['loaded']['native_imported_after_query']
    for module in report["loaded"]["modules"]:
        assert fingerprint(Path(module["path"]))["sha256"] == module["sha256"], "source changed during probe"
        if installed:
            assert '/site-packages/traceweave_mcp/' in module['path'], module
    report["status"] = "passed"
    return report


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--work-dir", type=Path)
    parser.add_argument("--output", type=Path)
    parser.add_argument("--installed", action="store_true", help="Use this interpreter's installed wheel in the child")
    parser.add_argument("--require-fsdb", action="store_true")
    args = parser.parse_args()
    with tempfile.TemporaryDirectory(prefix="traceweave-fst-readback-") as temporary:
        work = (args.work_dir or Path(temporary)).resolve()
        report = anyio.run(run_check, work, args.installed, args.require_fsdb)
        encoded = json.dumps(report, indent=2)
        if args.output:
            args.output.write_text(encoded + "\n")
            print(json.dumps({"status": report["status"], "calls": len(report["calls"]), "output": str(args.output)}))
        else:
            print(encoded)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
