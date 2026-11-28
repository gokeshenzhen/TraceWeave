"""Build and run the FIFO pause regression with Verilator or VCS."""

import argparse
from pathlib import Path
import shlex
import shutil
import subprocess
import sys
import tempfile

from replay import CASE, HOLD_FAILURE, inspect_run


def simulate(simulator, directory, rtl, *, apply_repair=False):
    directory.mkdir()
    shutil.copyfile(rtl, directory / "axis_fifo.v")
    shutil.copyfile(CASE / "tb.sv", directory / "tb.sv")
    if apply_repair:
        subprocess.run(["patch", "--silent", "--batch", "--fuzz=0", "-p1", "-i", str(CASE / "repair.patch")],
                       cwd=directory, check=True)

    sources = ["axis_fifo.v", "tb.sv"]
    if simulator == "verilator":
        shutil.copyfile(CASE / "clock.cpp", directory / "clock.cpp")
        sources.append("clock.cpp")
        build = ["verilator", "--cc", "--exe", "--build", "--trace", "--trace-depth", "2",
                 "-Wno-fatal", "--top-module", "tb", *sources]
        executable = "./obj_dir/Vtb"
    else:
        build = ["vcs", "-full64", "-sverilog", "-debug_access+all", "-top", "tb",
                 *sources, "-o", "simv"]
        executable = "./simv"

    compile_log = directory / "compile.log"
    sim_log = directory / "sim.log"
    # Verilator's generated makefiles require a build path without spaces.
    temp_root = tempfile.gettempdir()
    if simulator == "verilator" and any(char.isspace() for char in temp_root):
        temp_root = "/tmp"
    with tempfile.TemporaryDirectory(prefix="axis-fifo-build-", dir=temp_root) as scratch:
        work = Path(scratch)
        for name in sources:
            shutil.copyfile(directory / name, work / name)
        with compile_log.open("w") as log:
            print(shlex.join(build), file=log, flush=True)
            compiled = subprocess.run(build, cwd=work, stdout=log, stderr=subprocess.STDOUT)
        if compiled.returncode:
            raise ValueError(f"Compilation failed ({compiled.returncode}). See {compile_log}")
        with sim_log.open("w") as log:
            result = subprocess.run([executable], cwd=work, stdout=log, stderr=subprocess.STDOUT)
        if not (work / "wave.vcd").is_file():
            raise ValueError(f"Simulation produced no wave.vcd. See {sim_log}")
        shutil.move(work / "wave.vcd", directory / "wave.vcd")

    verdict = inspect_run(directory)
    if verdict == "PASS" and result.returncode:
        raise ValueError(f"Simulator exited with {result.returncode} after PASS. See {sim_log}")
    return verdict


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("simulator", choices=("verilator", "vcs"))
    parser.add_argument("--rtl", type=Path, help="check your axis_fifo module in place of the demo pair")
    parser.add_argument("--out", type=Path, help="parent for a fresh run directory (default: system temp)")
    args = parser.parse_args()
    if not shutil.which(args.simulator):
        parser.error(f"{args.simulator} is not on PATH")
    if args.rtl is not None and not args.rtl.is_file():
        parser.error(f"RTL file not found: {args.rtl}")
    if args.out is not None:
        args.out.mkdir(parents=True, exist_ok=True)
    run = Path(tempfile.mkdtemp(prefix=f"axis-fifo-{args.simulator}-", dir=args.out)).resolve()
    print(f"Run directory: {run}", flush=True)
    if args.rtl is not None:
        if simulate(args.simulator, run / "candidate", args.rtl) != "PASS":
            raise ValueError("Candidate failed the regression")
    else:
        rtl = CASE / "axis_fifo.v"
        if simulate(args.simulator, run / "before", rtl) != HOLD_FAILURE:
            raise ValueError("The original RTL did not reproduce the expected hold violation")
        if simulate(args.simulator, run / "repaired", rtl, apply_repair=True) != "PASS":
            raise ValueError("The example repair failed the regression")


if __name__ == "__main__":
    try:
        main()
    except (OSError, ValueError, subprocess.CalledProcessError) as error:
        sys.exit(str(error))
