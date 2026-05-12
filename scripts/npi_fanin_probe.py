#!/usr/bin/env python3
"""Probe NPI fan_in_reg_list behavior against the uart KDB.

Compares three NPI traversal modes on the same signals:
  1. net.driver_list()                — current backend behaviour
  2. net.fan_in_reg_list(stop_at_pin=True, report_primary_port=True, top_scope_name=top)
  3. net.fan_in_reg_list(... top_scope_name=None)  — no boundary

Run with VERDI_HOME pointing at the Verdi install.
"""
from __future__ import annotations

import ctypes
import os
import sys

KDB = "/cache/Projects/vcs-verification-of-apb-based-uart-master-core/work/simv.daidir/kdb.elab++"
TOPS_TO_TRY = ["uart_tb_top", "uvm_custom_install_recording"]
SIGNALS = [
    "uart_tb_top.DUV1.rx_channel.rx_fifo.count",
    "uart_tb_top.DUV1.rx_channel.bit_counter",
    "uart_tb_top.DUV1.rx_channel.rx_fifo.push",
    "uart_tb_top.DUV1.control.rx_fifo_full",
]


def setup():
    verdi_home = os.environ["VERDI_HOME"]
    sys.path.insert(0, os.path.join(verdi_home, "share", "NPI", "python"))
    lib_dir = os.path.join(verdi_home, "share", "NPI", "lib", "LINUX64")
    for lib in ("libNPI.so", "libnpiL1.so"):
        p = os.path.join(lib_dir, lib)
        if os.path.exists(p):
            ctypes.CDLL(p, ctypes.RTLD_GLOBAL)
    from pynpi import npisys, netlist  # noqa
    return npisys, netlist


def load(npisys, top: str) -> bool:
    npisys.init(["probe"])
    rc = npisys.load_design(["probe", "-simflow", "-dbdir", KDB, "-top", top])
    print(f"[load] top={top} rc={rc}")
    return rc == 1


def probe_signal(netlist, sig: str, top: str) -> None:
    print(f"\n=== {sig} ===")
    net = netlist.get_net(sig)
    if net is None:
        net = netlist.get_actual_net(sig)
    if net is None:
        print("  get_net / get_actual_net both returned None")
        return
    print(f"  net.full_name={net.full_name()!r}")
    print(f"  net.type={net.type()!r}")

    try:
        drvs = net.driver_list() or []
        print(f"  driver_list ({len(drvs)}):")
        for d in drvs:
            print(f"    - {d.full_name()!r}  type={d.type()!r}")
    except Exception as exc:
        print(f"  driver_list FAILED: {exc}")

    for tsn in (top, None):
        try:
            regs = net.fan_in_reg_list(
                stop_at_pin=True,
                report_primary_port=True,
                top_scope_name=tsn,
            ) or []
            print(f"  fan_in_reg_list(top_scope_name={tsn!r}) ({len(regs)}):")
            for r in regs[:15]:
                print(f"    - {r.full_name()!r}  type={r.type()!r}")
            if len(regs) > 15:
                print(f"    ... +{len(regs)-15} more")
        except Exception as exc:
            print(f"  fan_in_reg_list({tsn!r}) FAILED: {exc}")


def main() -> int:
    npisys, netlist = setup()
    chosen = None
    for t in TOPS_TO_TRY:
        if load(npisys, t):
            chosen = t
            break
    if not chosen:
        print("Could not load_design with any top from", TOPS_TO_TRY)
        return 1
    for s in SIGNALS:
        probe_signal(netlist, s, chosen)
    return 0


if __name__ == "__main__":
    sys.exit(main())
