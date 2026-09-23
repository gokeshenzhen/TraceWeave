#!/usr/bin/env python3
"""Run original AXIS/AXI cocotb checkers in a separate evaluation environment.

Requires cocotb==1.9.2, cocotb-test==0.2.6, cocotbext-axi==0.1.24,
cocotb-bus==0.2.1, pytest==7.2.1 and jinja2==3.1.2; no implicit install.
"""
import argparse
import hashlib
import importlib.util
import json
import logging
import os
from pathlib import Path
import shutil
import sys
import time
import traceback
from types import SimpleNamespace
import xml.etree.ElementTree as ET

from expression_project_eval import PINS, SEEDS, save, validate_checkout


def run_case(root, dest, project, seed, width):
    import cocotb_test.simulator
    import find_libpython
    if not find_libpython.find_libpython():
        raise RuntimeError("cocotb requires a shared libpython; use a shared-library Python evaluation venv")
    name, dut = ("verilog-axis", "axis_arb_mux") if project == "P07" else ("verilog-axi", "axi_ram")
    repo = validate_checkout(root, name)
    work = dest / project / f"w{width}_{seed}"
    work.mkdir(parents=True, exist_ok=True)
    # The original pytest entry also generates an RTL wrapper beside its TB.
    source = work / "source"
    if not source.exists():
        shutil.copytree(repo, source, ignore=shutil.ignore_patterns(".git", "__pycache__", "sim_build"))
    tb = source / "tb" / dut / f"test_{dut}.py"
    module_spec = importlib.util.spec_from_file_location(f"test_{dut}", tb)
    module = importlib.util.module_from_spec(module_spec)
    module_spec.loader.exec_module(module)
    original_run = cocotb_test.simulator.run
    receipt = {"project": project, "seed": seed, "pins": {name: PINS[name]},
        "tb_sha256": hashlib.sha256(tb.read_bytes()).hexdigest(), "runs": []}

    def run(**kwargs):
        top = kwargs["toplevel"]
        dump = work / "traceweave_dump.sv"
        text = (f'`timescale 1ns/1ps\nmodule traceweave_dump;\n'
                f'initial begin $dumpfile("wave.vcd"); $dumpvars(0, {top}); end\n')
        # VCS VCD rejects memory-word dumpvars. The AES FSDB corpus covers
        # positive unpacked-memory reads; AXI VCD memory remains unavailable.
        text += '\ninitial begin #1ms; $dumpoff; end\nendmodule\n'
        dump.write_text(text)
        kwargs.update(simulator="vcs", sim_build=str(work), seed=seed, waves=False,
            compile_args=["-debug_access+all", "-top", "traceweave_dump", "-l", str(work / "compile.log")],
            sim_args=["-l", str(work / "sim.log")])
        kwargs["verilog_sources"].append(str(dump))
        kwargs["extra_env"]["COCOTB_LOG_LEVEL"] = "WARNING"
        receipt.update(top=top, parameters=kwargs["parameters"], dump_end_ps=1_000_000_000,
            compile_log=str(work / "compile.log"), sources={str(p): hashlib.sha256(Path(p).read_bytes()).hexdigest()
                for p in kwargs["verilog_sources"]},
            adaptations=["Original pytest entry; generated wrapper/dump only; RAM elements absent in VCD",
                         "cocotb 1.9.2 / cocotb-test 0.2.6 for shared-library Python and local VCS"],
            python_version=sys.version)
        save(work / "receipt.json", receipt)
        return original_run(**kwargs)

    cocotb_test.simulator.run = run
    # Upstream generator uses /usr/bin/env python, so use this isolated venv.
    os.environ["PATH"] = str(Path(sys.executable).parent) + os.pathsep + os.environ["PATH"]
    start = time.monotonic()
    error = None
    try:
        request = SimpleNamespace(node=SimpleNamespace(name=f"{dut}_{width}_{seed}"))
        if project == "P07":
            module.test_axis_arb_mux(request, ports=4, data_width=width, round_robin=1)
        else:
            module.test_axi_ram(request, data_width=width)
    except (Exception, SystemExit) as exc:
        error = f"{type(exc).__name__}: {exc}"
        traceback.print_exc()
    finally:
        cocotb_test.simulator.run = original_run
    suites = list(work.glob("*_results.xml"))
    count = failures = 0
    if suites:
        try:
            tree = ET.parse(max(suites, key=lambda p: p.stat().st_mtime))
            count = len(list(tree.iter("testcase")))
            failures = len(list(tree.iter("failure"))) + len(list(tree.iter("error")))
        except ET.ParseError:
            error = error or "incomplete_cocotb_results"
    row = {"seed": seed, "wave_path": str(work / "wave.vcd"), "sim_log": str(work / "sim.log"),
           "elapsed_sec": time.monotonic() - start,
           "checker": {"passed": error is None and count > 0 and failures == 0,
                       "testcases": count, "failures": failures, "error": error}}
    receipt["runs"] = [row]
    save(work / "receipt.json", receipt)
    print(json.dumps({"project": project, "width": width, "seed": seed, "checker": row["checker"]}), flush=True)
    return row["checker"]["passed"]


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--sources-root", required=True, type=Path)
    parser.add_argument("--work-dir", required=True, type=Path)
    parser.add_argument("--projects", nargs="+", choices=["P07", "P08"], default=["P07", "P08"])
    parser.add_argument("--seeds", nargs="+", type=int, default=SEEDS)
    parser.add_argument("--widths", nargs="+", type=int, choices=[8, 16, 32], default=[32])
    args = parser.parse_args()
    args.work_dir.mkdir(parents=True, exist_ok=True)
    logging.basicConfig(filename=args.work_dir / "cocotb_driver.log", level=logging.INFO)
    ok = True
    for project in args.projects:
        for width in args.widths:
            for seed in args.seeds:
                ok = run_case(args.sources_root.resolve(), args.work_dir.resolve(), project, seed, width) and ok
    raise SystemExit(0 if ok else 1)
