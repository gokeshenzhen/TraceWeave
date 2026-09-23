#!/usr/bin/env python3
"""Compare public expression queries to simulator-recorded upstream RTL values.

Receipts must first report a passing original checker. All evidence is obtained
through a fresh MCP stdio server. This is a correctness evaluation, not an LLM
adoption measurement. Reports and upstream artifacts stay in the work directory.
"""
import argparse
import asyncio
import copy
import json
import os
from pathlib import Path
import re
import sys

import anyio
from jsonschema import Draft202012Validator
from mcp import ClientSession, StdioServerParameters
from mcp.client.stdio import stdio_client

ROOT = Path(__file__).resolve().parents[1]


def rules(project, top, width=32):
    result = []

    def add(expr, bindings, actual, clock=None, start=110_000, types=None, constants=None):
        spec = {"expr": expr, "typing": "wave_bits", "bindings": bindings}
        if types:
            spec["types"] = types
        if constants:
            spec["constants"] = constants
        result.append({"expression": spec, "actual": actual,
            "clock": clock or top + ".clk", "start": start})

    if project == "P01":
        for ft in (False, True):
            for depth in (1, 8, 9):
                p = top + f".i_tb_{'ft_' if ft else ''}{depth}.dut."
                expr = "m[i]" if not ft else "(n == 0 && push) ? d : m[i]"
                binds = {"m": p + "mem_q", "i": p + "read_pointer_q"}
                if ft:
                    binds.update(n=p + "status_cnt_q", push=p + "push_i", d=p + "data_i")
                add(expr, binds, p + "data_o", types={"m": {"width": 8 * depth,
                    "packed": [[depth - 1, 0], [7, 0]]}})
    elif project == "P02":
        add("d[i]", {"d": top + ".data_inp", "i": top + ".idx"}, top + ".data_oup",
            types={"d": {"width": 315, "packed": [[6, 0], [44, 0]]}})
    elif project == "P03":
        p = top + ".dut."
        add("d[i*W+:W]", {"d": p + "inp_data_i", "i": p + "slice_q"}, p + "oup_data_o", constants={"W": "8"})
    elif project == "P04":
        p = top + ".dut."
        add("{d,q}", {"d": p + "inp_data_i", "q": p + "gen_upsize.data_q"}, p + "oup_data_o")
    elif project == "P05":
        terms = [f"(a >= m[{i}].start_addr && a < m[{i}].end_addr)" for i in range(3)]
        add(" || ".join(terms), {"a": top + ".addr", "m": top + ".addr_map"}, top + ".dec_valid",
            clock="traceweave_dump.probe_clk", start=501_000,
            types={"m": {"width": 168, "packed": [[2, 0], [55, 0]], "members": [
                {"name": "idx", "lsb": 24, "type": {"width": 32}},
                {"name": "start_addr", "lsb": 12, "type": {"width": 12}},
                {"name": "end_addr", "lsb": 0, "type": {"width": 12}}]}})
    elif project == "P06":
        for bits, start in [(1, 10), (5, 520), (16, 1035), (32, 1550), (64, 2065), (981, 2580)]:
            add("$countones(d)", {"d": top + f".data_w{bits}"}, top + f".popcount_w{bits}",
                clock="traceweave_dump.probe_clk", start=start * 1000)
            result[-1]["count"] = 45
    elif project == "P07":
        p = top + ".axis_arb_mux_inst."
        add("d[i*W+:W]", {"d": p + "s_axis_tdata_reg", "i": p + "grant_encoded"},
            p + "current_s_tdata", constants={"W": str(width)})
        add("v[i]", {"v": p + "s_axis_tvalid_reg", "i": p + "grant_encoded"}, p + "current_s_tvalid")
    elif project == "P08":
        p = top + "."
        add("a >> S", {"a": p + "read_addr_reg"}, p + "read_addr_valid",
            constants={"S": str((width // 8).bit_length() - 1)})
    elif project == "P09":
        p = top + ".dut."
        add("h + ({e[5:0],e[31:6]} ^ {e[10:0],e[31:11]} ^ {e[24:0],e[31:25]}) + "
            "((e & f) ^ (~e & g)) + w + k", {k: p + v for k, v in {
                "h": "h_reg", "e": "e_reg", "f": "f_reg", "g": "g_reg", "w": "w_data", "k": "k_data"}.items()},
            p + "t1", clock=top + ".tb_clk", start=50_000)
    elif project == "P10":
        p = top + ".dut.keymem."
        add("m[r]", {"r": p + "round", "m": {"elements": [
            {"indices": [i], "signal": p + f"key_mem[{i}]"} for i in range(15)]}},
            p + "round_key", clock=top + ".tb_clk", start=50_000,
            types={"m": {"width": 128, "packed": [[127, 0]], "unpacked": [[0, 14]]}})
    elif project == "P11":
        add("({8{s0}} & d0) | ({8{s1}} & d1)", {k: top + "." + v for k, v in
            {"s0": "stb0", "s1": "stb1", "d0": "dat0_i", "d1": "dat1_i"}.items()}, top + ".dat_i",
            start=35_000)
    else:
        raise ValueError(project)
    return result


async def evaluate(receipt_path):
    receipt = json.loads(receipt_path.read_text())
    project = receipt["project"]
    work = receipt_path.parent
    report = {"project": project, "model_adoption": "not_measured", "runs": []}
    params = StdioServerParameters(command=sys.executable, args=[str(ROOT / "server.py")], cwd=str(ROOT),
        env={**os.environ, "TRACEWEAVE_TELEMETRY": "0", "TRACEWEAVE_AUTO_KDB": "0"})
    with anyio.fail_after(300):
        async with stdio_client(params) as (read, write):
            async with ClientSession(read, write) as client:
                await client.initialize()
                catalog = {t.name: t for t in (await client.list_tools()).tools}
                evidence = []

                async def call(name, args):
                    Draft202012Validator(catalog[name].inputSchema).validate(args)
                    response = await client.call_tool(name, args)
                    payload = json.loads(response.content[0].text)
                    evidence.append({"tool": name, "arguments": args, "result": payload})
                    if response.isError or payload.get("error"):
                        raise AssertionError({"tool": name, "payload": payload})
                    return payload

                compile_log = receipt.get("compile_log", str(work / "compile.log"))
                for run in receipt["runs"]:
                    item = {"seed": run["seed"], "checks": []}
                    report["runs"].append(item)
                    wave = run["wave_path"]
                    try:
                        await call("get_sim_paths", {"verif_root": str(work), "compile_log": compile_log,
                            "sim_log": run["sim_log"], "wave_file": wave})
                        await asyncio.gather(call("build_tb_hierarchy", {"compile_log": compile_log}),
                            call("scan_structural_risks", {"compile_log": compile_log}))
                        await call("parse_sim_log", {"log_path": run["sim_log"], "simulator": "vcs"})
                        if not run["checker"]["passed"]:
                            await call("sweep_handshakes", {"wave_path": wave})
                            raise AssertionError("Original checker did not pass")
                        await call("get_waveform_summary", {"wave_path": wave})
                        for rule in rules(project, receipt["top"], receipt["parameters"].get("DATA_WIDTH", 32)):
                            spec, actual = rule["expression"], rule["actual"]
                            paths = [actual, rule["clock"]]
                            for binding in spec["bindings"].values():
                                paths += ([binding] if isinstance(binding, str) else
                                          [e["signal"] for e in binding["elements"]])
                            keywords = list(dict.fromkeys(p.rsplit(".", 1)[-1].split("[")[0] for p in paths))
                            discovery = await call("search_signals", {"wave_path": wave, "keyword": keywords,
                                "max_results": 128})
                            matches = [r["path"] for group in discovery["batch"] for r in group["results"]]

                            def exact(path):
                                candidates = set(p for p in matches if re.sub(r"\[-?\d+:-?\d+\]$", "", p) == path or p == path)
                                assert len(candidates) == 1, ("missing_or_ambiguous_dump", path, sorted(candidates))
                                return candidates.pop()

                            actual, clock = exact(actual), exact(rule["clock"])
                            for alias, binding in spec["bindings"].items():
                                if isinstance(binding, str):
                                    spec["bindings"][alias] = exact(binding)
                                else:
                                    for element in binding["elements"]:
                                        element["signal"] = exact(element["signal"])
                            cycles = await call("get_signals_by_cycle", {"wave_path": wave,
                                "clock_path": clock, "start_time_ps": rule["start"],
                                "num_cycles": rule.get("count", 128), "sample_offset_ps": 1,
                                "signal_paths": [spec, actual]})
                            assert not cycles["signal_errors"] and not cycles["transition_data_truncated"], cycles
                            rec = cycles["expressions"][0]
                            assert rec["coverage_status"] == "complete", rec
                            key = rec["key"]
                            actual_key = cycles["resolved_aliases"].get(actual, actual)
                            rows = cycles["cycles"]
                            known = [r for r in rows if r["signals"][actual_key].get("dec") is not None]
                            assert len(known) >= 8, ("insufficient_known_samples", len(known))
                            for row in rows:
                                derived, observed = row["signals"][key], row["signals"][actual_key]
                                if observed.get("dec") is not None:
                                    assert derived.get("dec") == observed["dec"], row
                                else:
                                    a, b = (v["bin"].lower().removeprefix("b") for v in (derived, observed))
                                    n = max(len(a), len(b))
                                    assert a.rjust(n, a[0] if a[0] in "xz" else "0") == b.rjust(
                                        n, b[0] if b[0] in "xz" else "0"), row
                            point_time = known[len(known)//2]["time_ps"] + 1
                            point = await call("get_signal_at_time", {"wave_path": wave, "signal_path": spec,
                                "time_ps": point_time})
                            expected = known[len(known)//2]["signals"][actual_key]["dec"]
                            assert point["value"]["dec"] == expected, point
                            predicate = copy.deepcopy(spec)
                            predicate["expr"] = f"({spec['expr']}) === observed"
                            predicate["bindings"]["observed"] = actual
                            window = await call("verify_window", {"wave_path": wave, "clock": clock,
                                "mode": "always", "start_time_ps": rows[0]["time_ps"],
                                "end_time_ps": rows[-1]["time_ps"], "predicate": [predicate]})
                            assert window["holds"] and window["cycles_evaluated"] == len(rows), window
                            assert all(r["coverage_status"] == "complete" for r in window["expressions"]), window
                            item["checks"].append({"expr": spec["expr"], "known_samples": len(known),
                                "point_time_ps": point_time, "point_value": expected, "window_cycles": len(rows),
                                "status": "passed"})
                        item["status"] = "passed"
                    except Exception as exc:
                        item.update(status="failed", error=str(exc))
                    (work / "expression_evidence.json").write_text(json.dumps(evidence, indent=2))
                    (work / "expression_report.json").write_text(json.dumps(report, indent=2))
                    print(json.dumps({"project": project, "seed": run["seed"], "status": item["status"],
                        "checks": len(item["checks"]), "error": item.get("error", "")[:500]}), flush=True)
    return report


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("receipts", type=Path, nargs="+")
    args = parser.parse_args()
    passed = True
    for path in args.receipts:
        report = anyio.run(evaluate, path.resolve())
        passed = bool(report["runs"]) and all(r["status"] == "passed" for r in report["runs"]) and passed
    raise SystemExit(0 if passed else 1)
