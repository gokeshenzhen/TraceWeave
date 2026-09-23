#!/usr/bin/env python3
"""Check private fault/clean/missing corpus receipts through the public MCP.

Fault recipes are deliberately external. The independent formulas are the same
ones used for the pinned clean corpus. This measures evidence correctness, not
whether an LLM discovers or uses the expression API.
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
sys.path.insert(0, str(ROOT))
from scripts.check_project_expressions import rules


def compare_samples(rows, derived, actual):
    """Unknown/missing values never establish a numeric mismatch or a pass."""
    known, mismatches = [], []
    for row in rows:
        a, b = (row["signals"][key].get("dec") for key in (derived, actual))
        if a is None or b is None:
            continue
        known.append(row)
        if a != b:
            mismatches.append(row)
    return known, mismatches


def check_outcome(checks, expected):
    if not checks:
        return False
    states = [c["status"] for c in checks]
    if expected == "missing":
        return "missing" in states and all(s in {"missing", "match"} for s in states)
    if "missing" in states or any(c.get("known_samples", 0) < 8 for c in checks):
        return False
    if expected == "mismatch":
        return "mismatch" in states and all(s in {"match", "mismatch"} for s in states)
    return all(s == "match" for s in states)


async def evaluate(receipt_path, expected):
    receipt = json.loads(receipt_path.read_text())
    work, project = receipt_path.parent, receipt["project"]
    report = {"project": project, "expected": expected, "model_adoption": "not_measured", "runs": []}
    evidence = []
    params = StdioServerParameters(command=sys.executable, args=[str(ROOT / "server.py")], cwd=str(ROOT),
        env={**os.environ, "TRACEWEAVE_TELEMETRY": "0", "TRACEWEAVE_AUTO_KDB": "0"})
    with anyio.fail_after(300):
        async with stdio_client(params) as (read, write):
            async with ClientSession(read, write) as client:
                await client.initialize()
                catalog = {t.name: t for t in (await client.list_tools()).tools}

                async def call(name, args):
                    Draft202012Validator(catalog[name].inputSchema).validate(args)
                    response = await client.call_tool(name, args)
                    payload = json.loads(response.content[0].text)
                    evidence.append({"tool": name, "arguments": args, "result": payload})
                    assert not response.isError and not payload.get("error"), (name, payload)
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
                        assert run["returncode"] != "timeout", "Simulation timed out"
                        if expected == "mismatch":
                            assert not run["checker"]["passed"] and run["checker"]["errors"], "Original checker did not catch fault"
                        else:
                            assert run["checker"]["passed"], "Original checker must pass for clean/missing control"
                        await call("get_waveform_summary", {"wave_path": wave})
                        for rule in rules(project, receipt["top"], receipt["parameters"].get("DATA_WIDTH", 32)):
                            spec = copy.deepcopy(rule["expression"])
                            paths = [rule["actual"], rule["clock"]]
                            for binding in spec["bindings"].values():
                                paths += [binding] if isinstance(binding, str) else [e["signal"] for e in binding["elements"]]
                            keywords = list(dict.fromkeys(p.rsplit(".", 1)[-1].split("[")[0] for p in paths))
                            discovery = await call("search_signals", {"wave_path": wave, "keyword": keywords, "max_results": 128})
                            matches = [r["path"] for group in discovery["batch"] for r in group["results"]]

                            def exact(path, allow_missing=False):
                                candidates = set(p for p in matches if re.sub(r"\[-?\d+:-?\d+\]$", "", p) == path or p == path)
                                if not candidates and allow_missing:
                                    return path  # Deliberate negative: no invented observations.
                                assert len(candidates) == 1, ("missing_or_ambiguous_dump", path, sorted(candidates))
                                return candidates.pop()

                            actual, clock = exact(rule["actual"]), exact(rule["clock"])
                            for alias, binding in spec["bindings"].items():
                                if isinstance(binding, str):
                                    spec["bindings"][alias] = exact(binding)
                                else:
                                    for e in binding["elements"]:
                                        e["signal"] = exact(e["signal"], expected == "missing")
                            cycles = await call("get_signals_by_cycle", {"wave_path": wave, "clock_path": clock,
                                "start_time_ps": rule["start"], "num_cycles": rule.get("count", 128),
                                "sample_offset_ps": 1, "signal_paths": [spec, actual]})
                            assert not cycles["transition_data_truncated"], "Truncated observations"
                            rec = cycles["expressions"][0]
                            if rec["coverage_status"] != "complete":
                                assert expected == "missing" and rec["coverage_status"] == "partial", rec
                                point = await call("get_signal_at_time", {"wave_path": wave, "signal_path": spec,
                                    "time_ps": rule["start"]})
                                assert point["expressions"][0]["coverage_status"] == "partial", point
                                assert point.get("value", {}).get("dec") is None, point
                                pred = copy.deepcopy(spec)
                                pred["expr"] = f"({spec['expr']}) === observed"
                                pred["bindings"]["observed"] = actual
                                window = await call("verify_window", {"wave_path": wave, "clock": clock, "mode": "always",
                                    "start_time_ps": cycles["cycles"][0]["time_ps"],
                                    "end_time_ps": cycles["cycles"][-1]["time_ps"], "predicate": [pred]})
                                # Legacy holds means no violation among evaluated known
                                # samples. With zero known samples it may be true: only
                                # coverage + gaps establish this missing-evidence result.
                                assert window["coverage_status"] == "partial", window
                                assert window["unknown_cycles"] == window["cycles_evaluated"] > 0, window
                                assert all(e["coverage_status"] == "partial" and e["gaps"]
                                           for e in window["expressions"]), window
                                item["checks"].append({"status": "missing", "expression": rec, "point": point, "window": window})
                                continue
                            assert not cycles["signal_errors"], cycles["signal_errors"]
                            key = rec["key"]
                            actual_key = cycles["resolved_aliases"].get(actual, actual)
                            rows = cycles["cycles"]
                            known, mismatches = compare_samples(rows, key, actual_key)
                            assert len(known) >= 8, ("insufficient_known_samples", len(known))
                            sample = mismatches[0] if mismatches else known[len(known)//2]
                            # Cycle timestamps are edge times; point reads reproduce the offset.
                            at = sample["time_ps"] + 1
                            points = [await call("get_signal_at_time", {"wave_path": wave,
                                "signal_path": path, "time_ps": at}) for path in (spec, actual)]
                            assert [p["value"]["dec"] for p in points] == [sample["signals"][k]["dec"] for k in (key, actual_key)]
                            pred = copy.deepcopy(spec)
                            pred["expr"] = f"({spec['expr']}) === observed"
                            pred["bindings"]["observed"] = actual
                            window = await call("verify_window", {"wave_path": wave, "clock": clock, "mode": "always",
                                "start_time_ps": rows[0]["time_ps"], "end_time_ps": rows[-1]["time_ps"], "predicate": [pred]})
                            assert window["cycles_evaluated"] == len(rows), window
                            assert all(r["coverage_status"] == "complete" for r in window["expressions"]), window
                            assert window["holds"] is (not bool(mismatches)), window
                            item["checks"].append({"status": "mismatch" if mismatches else "match", "expr": spec["expr"],
                                "known_samples": len(known), "mismatch_count": len(mismatches), "point_time_ps": at,
                                "expected_value": points[0]["value"], "observed_value": points[1]["value"],
                                "window": window})
                        assert check_outcome(item["checks"], expected), item["checks"]
                        item["status"] = "passed"
                    except Exception as exc:
                        item.update(status="failed", error=str(exc))
                    (work / "fault_evidence.json").write_text(json.dumps(evidence, indent=2))
                    (work / "fault_report.json").write_text(json.dumps(report, indent=2))
                    print(json.dumps({"project": project, "seed": run["seed"], "expected": expected,
                        "status": item["status"], "checks": len(item["checks"]), "error": item.get("error", "")[:500]}), flush=True)
    return report


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("receipts", type=Path, nargs="+")
    parser.add_argument("--expected", choices=["mismatch", "match", "missing"], default="mismatch")
    args = parser.parse_args()
    passed = True
    for path in args.receipts:
        report = anyio.run(evaluate, path.resolve(), args.expected)
        passed = bool(report["runs"]) and all(r["status"] == "passed" for r in report["runs"]) and passed
    raise SystemExit(0 if passed else 1)
