#!/usr/bin/env python3
"""Opt-in, pinned upstream simulation corpus. Never downloads or installs implicitly.

Use --sources-root for clean checkouts and --work-dir for private generated
sources, simulator outputs and receipts. Ordinary pytest does not invoke EDA.
"""
import argparse
import hashlib
import json
from pathlib import Path
import re
import shlex
import subprocess
import time

PINS = {
    "common_cells": ("pulp-platform/common_cells", "e73baaec2ca665cd80c3c384e9258e35242b829c"),
    "common_verification": ("pulp-platform/common_verification", "6fc76fb013315af9fabbb90b431863d498df2d6d"),
    "verilog-axis": ("alexforencich/verilog-axis", "48ff7a7e2ef782cf778d47910cf85835c64b1bce"),
    "verilog-axi": ("alexforencich/verilog-axi", "516bd5dadc3365b7f9e225d2af8fe0b8d804fe53"),
    "sha256": ("secworks/sha256", "837c5cc396f001d18f2c765721c585716eb439ae"),
    "aes": ("secworks/aes", "80dc4718e1dcbbdb4b0dd1bdb393d8f7b98981dc"),
    "i2c": ("freecores/i2c", "3b067f00ccced753b0502024766a51f58f3e04bc"),
}
COMMON = {
    "P01": ("cc_fifo_tb", ["cc_fifo"], {"NumChecks": 1000}),
    "P02": ("cc_rr_arb_tree_tb", ["cc_lzc", "cc_rr_arb_tree"], {}),
    "P03": ("cc_stream_downsizer_tb", ["cc_delta_counter", "cc_trip_counter", "cc_stream_downsizer"], {"NumChecks": 512}),
    "P04": ("cc_stream_upsizer_tb", ["cc_delta_counter", "cc_trip_counter", "cc_stream_upsizer"], {"NumChecks": 512}),
    "P05": ("cc_addr_decode_tb", ["cc_addr_decode_dync", "cc_addr_decode"], {}),
    "P06": ("cc_popcount_tb", ["cc_popcount"], {}),
}
NATIVE = list(COMMON) + ["P09", "P10", "P11"]
SEEDS = [230923, 230924, 230925]


def save(path, value):
    path.write_text(json.dumps(value, indent=2) + "\n")


def validate_checkout(root, name):
    repo = root / name
    head = subprocess.check_output(["git", "-C", str(repo), "rev-parse", "HEAD"], text=True).strip()
    if head != PINS[name][1]:
        raise ValueError(f"Wrong commit for {name}: {head}")
    dirty = subprocess.check_output(["git", "-C", str(repo), "status", "--porcelain", "--untracked-files=no"], text=True)
    if dirty.strip():
        raise ValueError(f"Tracked upstream sources modified: {name}")
    return repo


def native_sources(root, project, work):
    adaptations = []
    if project in COMMON:
        repo = validate_checkout(root, "common_cells")
        cv = validate_checkout(root, "common_verification")
        top, modules, parameters = COMMON[project]
        sources = [repo / "src" / f"{n}.sv" for n in ["assert_rpt_pkg", "cc_pkg"]]
        sources += [cv / "src" / f"{n}.sv" for n in ["rand_verif_pkg", "clk_rst_gen",
            "rand_synch_holdable_driver", "rand_synch_driver", "rand_stream_mst", "rand_stream_slv"]]
        if project == "P02":
            original = cv / "src/clk_rst_gen.sv"
            text = original.read_text()
            assert text.count("rst_no = 1'b1;") == 1
            mirror = work / "clk_rst_gen.sv"
            mirror.write_text(text.replace("rst_no = 1'b1;", "@(negedge clk); rst_no = 1'b1;"))
            sources[sources.index(original)] = mirror
            adaptations.append("common_verification v0.2.0: release reset on falling edge to avoid active-region TB/DUT race")
        sources += [repo / "src" / f"{n}.sv" for n in modules]
        tb = repo / "test" / f"{top}.sv"
        if project == "P01":
            # rand_wait's ref argument is read-only in practice. A procedural
            # mirror avoids VCS's continuous/procedural driver conflict without
            # replacing ref by input (which would freeze the clock in the task).
            text = tb.read_text()
            assert text.count("assign clk = clk_i;") == 1
            tb = work / tb.name
            tb.write_text(text.replace("assign clk = clk_i;", "always @(clk_i) clk = clk_i;"))
            adaptations.append("FIFO TB clock mirror: continuous -> procedural for VCS ref compatibility")
        sources += [tb]
        include = [repo / "include"]
    elif project in {"P09", "P10"}:
        name = "sha256" if project == "P09" else "aes"
        repo = validate_checkout(root, name)
        top = f"tb_{name}_core"
        modules = (["sha256_core", "sha256_k_constants", "sha256_w_mem"] if project == "P09"
            else ["aes_core", "aes_key_mem", "aes_sbox", "aes_inv_sbox", "aes_encipher_block", "aes_decipher_block"])
        sources = [repo / "src/rtl" / f"{n}.v" for n in modules]
        sources += [repo / "src/tb" / f"{top}.v"]
        parameters, include = {}, []
    elif project == "P11":
        repo = validate_checkout(root, "i2c")
        top, parameters = "tst_bench_top", {}
        include = [repo / "rtl/verilog", repo / "bench/verilog"]
        sources = [repo / "rtl/verilog" / f"{n}.v" for n in
                   ["i2c_master_bit_ctrl", "i2c_master_byte_ctrl", "i2c_master_top"]]
        sources += [repo / "bench/verilog" / f"{n}.v" for n in
                    ["wb_master_model", "i2c_slave_model", top]]
    else:
        raise ValueError(project)
    return top, sources, include, parameters, adaptations


def dump_source(project, top):
    # Bound only waveform recording; the original checker keeps running.
    wave = "wave.fsdb" if project == "P10" else "wave.vcd"
    body = "" if project == "P10" else f'initial begin $dumpfile("{wave}"); $dumpvars(0, {top}); end\n'
    if project == "P10":
        body += f'initial begin $fsdbDumpfile("{wave}"); $fsdbDumpvars(0, {top}); $fsdbDumpMDA(); end\n'
    if project == "P06":
        body += '\ninitial begin #3100ns; $display("TW_POPCOUNT_COMPLETED"); $finish; end\n'
    if project == "P02":
        body += f'\ninitial begin wait (&{top}.end_of_sim); $display("TW_RR_COMPLETED"); end\n'
    body += '\ninitial begin #50us; $dumpoff; end\n'
    body += '\ninitial begin #20ms; $fatal(1, "TW_WATCHDOG"); end\n'
    return ("`timescale 1ns/1ps\nmodule traceweave_dump;\n"
        "reg probe_clk = 0; always #5ns probe_clk = ~probe_clk;\n"
        "initial $dumpvars(0, probe_clk);\n" + body + "endmodule\n"), wave


def checker_result(project, log):
    errors = re.findall(r"(?im)^.*(?:\b(?:error|fatal)\b|assert(?:ion)?[^\n]*fail|NOT successful|did not complete successfully|TW_WATCHDOG|is unfair).*$", log)
    if project == "P01":
        completed = len(re.findall(r"Checked 1000 stimuli", log)) == 6
    elif project in {"P03", "P04"}:
        completed = "Checked 512 stimuli" in log
    elif project == "P02":
        completed = "TW_RR_COMPLETED" in log
    elif project == "P05":
        completed = bool(re.search(r"Failed:\s+0\b", log)) and bool(re.search(r"Passed:\s+[1-9]\d*", log))
    elif project == "P06":
        completed = "TW_POPCOUNT_COMPLETED" in log and "Randomization failed" not in log
    elif project in {"P09", "P10"}:
        completed = bool(re.search(r"All \d+ test cases completed successfully", log))
    elif project == "P11":
        completed = "Testbench done" in log
    else:
        raise ValueError(project)
    return {"passed": completed and not errors, "completed": completed, "errors": errors[:20]}


def command_run(args, cwd, output, timeout):
    start = time.monotonic()
    with output.open("w") as stream:
        try:
            result = subprocess.run(args, cwd=cwd, stdout=stream, stderr=subprocess.STDOUT, timeout=timeout)
            code = result.returncode
        except subprocess.TimeoutExpired:
            code = "timeout"
    return {"command": args, "returncode": code, "elapsed_sec": time.monotonic() - start}


def run_native(root, dest, project, seeds):
    work = dest / project
    work.mkdir(parents=True, exist_ok=True)
    top, sources, includes, params, changes = native_sources(root, project, work)
    dump, wave_name = dump_source(project, top)
    (work / "traceweave_dump.sv").write_text(dump)
    sources += [work / "traceweave_dump.sv"]
    compile_args = ["vcs", "-full64", "-sverilog", "-timescale=1ns/1ps", "-debug_access+all"]
    compile_args += ["+incdir+" + str(p) for p in includes] + [str(p) for p in sources]
    compile_args += ["-top", top, "-top", "traceweave_dump"]
    compile_args += [f"-pvalue+{top}.{k}={v}" for k, v in params.items()]
    compile_args += ["-o", str(work / "simv"), "-l", str(work / "compile.log")]
    receipt = {"project": project, "pins": PINS, "top": top, "parameters": params,
        "adaptations": changes, "dump_end_ps": 50_000_000,
        "sources": {str(p): hashlib.sha256(p.read_bytes()).hexdigest() for p in sources}, "runs": []}
    (work / "compile.sh").write_text("#!/bin/sh\n" + shlex.join(compile_args) + "\n")
    receipt["compile"] = command_run(compile_args, work, work / "compile.stdout.log", 180)
    save(work / "receipt.json", receipt)
    if receipt["compile"]["returncode"] != 0:
        return receipt
    for seed in seeds if project in {"P01", "P02", "P03", "P04", "P06"} else seeds[:1]:
        run = work / str(seed)
        run.mkdir(exist_ok=True)
        (run / "batch.tcl").write_text("run\nquit\n")
        cmd = [str(work / "simv"), "-ucli", "-i", str(run / "batch.tcl"),
               f"+ntb_random_seed={seed}", "-l", str(run / "sim.log")]
        row = {"seed": seed, "wave_path": str(run / wave_name), "sim_log": str(run / "sim.log")}
        row.update(command_run(cmd, run, run / "sim.stdout.log", 180))
        row["checker"] = checker_result(project, (run / "sim.log").read_text(errors="replace"))
        row["checker"]["passed"] = row["checker"]["passed"] and row["returncode"] == 0
        row["wave_bytes"] = (run / wave_name).stat().st_size if (run / wave_name).exists() else 0
        receipt["runs"].append(row)
        save(work / "receipt.json", receipt)
    return receipt


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--sources-root", required=True, type=Path)
    parser.add_argument("--work-dir", required=True, type=Path)
    parser.add_argument("--projects", nargs="+", choices=NATIVE, default=NATIVE)
    parser.add_argument("--seeds", nargs="+", type=int, default=SEEDS)
    args = parser.parse_args()
    results = []
    for project in args.projects:
        result = run_native(args.sources_root.resolve(), args.work_dir.resolve(), project, args.seeds)
        results.append(result)
        print(json.dumps({"project": project, "compile": result["compile"]["returncode"],
            "checker_passes": [r["checker"]["passed"] for r in result["runs"]]}), flush=True)
    return 0 if all(r["runs"] and all(x["checker"]["passed"] for x in r["runs"]) for r in results) else 1


if __name__ == "__main__":
    raise SystemExit(main())
