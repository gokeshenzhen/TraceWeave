"""Shared control scaffolding; labels here are never real NPI evidence."""
import json
from pathlib import Path

import server
from src import connectivity_backend
from tests.test_design_context_connectivity_server import ControlNpi, BackendControls


def project(root, *, top="top", candidate=True):
    root.mkdir(parents=True, exist_ok=True)
    source, log = root / "design.sv", root / "compile.log"
    source.write_text(f"module leaf(input logic a, output logic y); assign y=a; endmodule\n"
        f"module {top}(input logic a, output logic y); leaf u(a,y); endmodule\n")
    log.write_text(f"Chronologic VCS simulator\nCommand: vcs -sverilog {source} -top {top}\n"
        f"Parsing design file '{source}'\nTop Level Modules:\n       {top}\n")
    kdb = root / "simv.daidir" / "kdb.elab++"
    if candidate:
        kdb.mkdir(parents=True)
        (kdb / "_libFileMap").write_bytes(b"lib-A")
        (kdb / "_stnElabDB").write_bytes(b"elab-A")
    return source, log, kdb


async def public(tool, **arguments):
    if tool in server.schemas.COMPACT_OUTPUT_TOOLS:
        arguments.setdefault("output_format", "full")
    result = await server.call_tool(tool, arguments)
    return json.loads(result[0].text)


def arguments(tool, **extra):
    value = ({"from_signal": "top.a", "to_signal": "top.y"} if tool == "trace_signal_path"
             else {"signal_path": "top.y"})
    if tool == "explain_signal_driver":
        value["wave_path"] = "/unused/native-route-control.vcd"
    return {**value, **extra}


class NativeControls(BackendControls):
    def select(self, status, *, fallback):
        backend = super().select(status, fallback=fallback)
        def bind(inputs):
            backend.inputs = inputs
            if inputs.compile_result_json is not None:
                backend.bind_compile_context(inputs.compile_result())
        backend.bind_native_inputs = bind
        return backend
