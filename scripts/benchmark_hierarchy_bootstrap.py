#!/usr/bin/env python3
"""Reproducible large-log benchmark for hierarchy and bounded bootstrap.

The default fixture mirrors the reported scale: a 51 MiB / 200,000-line VCS
compile log, a 658 KiB / 8,913-line elaboration log, and 3,843 project sources.
Generation is streamed so fixture construction does not itself retain the log
or aggregate source contents in memory.
"""

from __future__ import annotations

import argparse
import json
from pathlib import Path
import resource
import sys
import tempfile
import time


REPO_ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(REPO_ROOT))

from config import BoundedBootstrapConfig  # noqa: E402
from src.bounded_hierarchy_bootstrap import (  # noqa: E402
    build_bounded_connectivity_context,
)
from src.compile_log_parser import (  # noqa: E402
    merge_compile_results,
    parse_compile_log,
)
from src.operation_metrics import read_process_rss_kib  # noqa: E402
from src.tb_hierarchy_builder import build_hierarchy  # noqa: E402


def _write_sized_source(path: Path, body: str, target_bytes: int) -> None:
    encoded = body.encode("utf-8")
    if len(encoded) > target_bytes:
        raise ValueError("source_bytes is too small for benchmark module body")
    padding = target_bytes - len(encoded)
    with path.open("wb") as stream:
        stream.write(encoded)
        if padding:
            if padding < 5:
                stream.write(b" " * padding)
            else:
                stream.write(b"/*")
                stream.write(b"x" * (padding - 4))
                stream.write(b"*/")


def _write_exact_log(
    path: Path,
    *,
    target_bytes: int,
    target_lines: int,
    prefix_lines: list[bytes],
    suffix_lines: list[bytes],
) -> None:
    fixed_lines = [*prefix_lines, *suffix_lines]
    fixed_bytes = sum(len(line) for line in fixed_lines)
    filler_lines = target_lines - len(fixed_lines)
    filler_bytes = target_bytes - fixed_bytes
    if filler_lines < 0 or filler_bytes < filler_lines:
        raise ValueError("requested exact log shape is smaller than fixed evidence")
    quotient, remainder = divmod(filler_bytes, filler_lines)
    with path.open("wb") as stream:
        for line in prefix_lines:
            stream.write(line)
        for index in range(filler_lines):
            line_bytes = quotient + (1 if index < remainder else 0)
            stream.write(b"x" * (line_bytes - 1))
            stream.write(b"\n")
        for line in suffix_lines:
            stream.write(line)
    stat = path.stat()
    if stat.st_size != target_bytes:
        raise AssertionError((stat.st_size, target_bytes))


def _fixture(
    root: Path,
    *,
    source_count: int,
    interface_count: int,
    source_bytes: int,
    compile_bytes: int,
    compile_lines: int,
    elaborate_bytes: int,
    elaborate_lines: int,
) -> tuple[Path, Path]:
    sources: list[Path] = []
    top = root / "target_top.sv"
    _write_sized_source(
        top,
        "module target_top(input logic a, output logic y);\n"
        "  logic gate_en;\n"
        "  assign gate_en = a;\n"
        "  assign y = gate_en;\n"
        "endmodule\n",
        source_bytes,
    )
    sources.append(top)
    interface_names = [
        f"bench_if_{index:04d}" for index in range(interface_count)
    ]
    for index in range(1, source_count):
        if index <= interface_count:
            interface_name = interface_names[index - 1]
            path = root / f"{interface_name}.sv"
            body = f"interface {interface_name}; endinterface\n"
        else:
            path = root / f"unit_{index:04d}.sv"
            if interface_names and index == interface_count + 1:
                references = "".join(
                    f"  {name} if_{offset:04d}();\n"
                    for offset, name in enumerate(interface_names)
                )
                body = (
                    f"module unit_{index:04d};\n"
                    f"{references}"
                    "endmodule\n"
                )
            else:
                body = f"module unit_{index:04d}; endmodule\n"
        _write_sized_source(
            path,
            body,
            source_bytes,
        )
        sources.append(path)

    command = (
        "Command: vcs -sverilog "
        + " ".join(str(path) for path in sources)
        + "\n"
    ).encode()
    parse_lines = [
        f"Parsing design file '{path}'\n".encode() for path in sources
    ]
    compile_log = root / "comp.log"
    _write_exact_log(
        compile_log,
        target_bytes=compile_bytes,
        target_lines=compile_lines,
        prefix_lines=[b"Chronologic VCS simulator\n", command, *parse_lines],
        suffix_lines=[],
    )
    elaborate_log = root / "elab.log"
    _write_exact_log(
        elaborate_log,
        target_bytes=elaborate_bytes,
        target_lines=elaborate_lines,
        prefix_lines=[
            b"Chronologic VCS simulator\n",
            b"Command: vcs -top target_top\n",
        ],
        suffix_lines=[b"Top Level Modules:\n", b"       target_top\n"],
    )
    return compile_log, elaborate_log


def _parse_merged(compile_log: Path, elaborate_log: Path) -> tuple[dict, dict]:
    started = time.perf_counter()
    primary = parse_compile_log(str(compile_log), "vcs")
    after_primary = time.perf_counter()
    supplementary = parse_compile_log(str(elaborate_log), "vcs")
    after_supplementary = time.perf_counter()
    merged = merge_compile_results(
        primary,
        [supplementary],
        primary_log=str(compile_log),
        supplementary_logs=[str(elaborate_log)],
    )
    after_merge = time.perf_counter()
    return merged, {
        "parse_compile_ms": round((after_primary - started) * 1000.0, 3),
        "parse_elaborate_ms": round(
            (after_supplementary - after_primary) * 1000.0, 3
        ),
        "merge_ms": round((after_merge - after_supplementary) * 1000.0, 3),
    }


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--mode", choices=("hierarchy", "bootstrap"), required=True)
    parser.add_argument("--source-count", type=int, default=3843)
    parser.add_argument("--interface-count", type=int, default=0)
    parser.add_argument("--source-bytes", type=int, default=16 * 1024)
    parser.add_argument("--compile-bytes", type=int, default=51 * 1024 * 1024)
    parser.add_argument("--compile-lines", type=int, default=200_000)
    parser.add_argument("--elaborate-bytes", type=int, default=658 * 1024)
    parser.add_argument("--elaborate-lines", type=int, default=8_913)
    args = parser.parse_args()
    if args.interface_count < 0 or args.interface_count >= args.source_count:
        parser.error("--interface-count must be >= 0 and < --source-count")

    with tempfile.TemporaryDirectory(prefix="traceweave_hierarchy_bench_") as tmp:
        root = Path(tmp)
        compile_log, elaborate_log = _fixture(
            root,
            source_count=args.source_count,
            interface_count=args.interface_count,
            source_bytes=args.source_bytes,
            compile_bytes=args.compile_bytes,
            compile_lines=args.compile_lines,
            elaborate_bytes=args.elaborate_bytes,
            elaborate_lines=args.elaborate_lines,
        )
        rss_before_kib = read_process_rss_kib()
        compile_result, parse_metrics = _parse_merged(compile_log, elaborate_log)
        rss_after_parse_kib = read_process_rss_kib()
        operation_started = time.perf_counter()
        if args.mode == "hierarchy":
            result = build_hierarchy(compile_result)
            operation_receipt = result["build_metrics"]
            retained_source_text_bytes = sum(
                len(str(scan.get("source_text") or "").encode())
                for scan in result["_scan_results"]
            )
            status = "completed"
        else:
            bootstrap_config = BoundedBootstrapConfig()
            result = build_bounded_connectivity_context(
                compile_result=compile_result,
                hierarchy_snapshot_sha256="0" * 64,
                signal_path="target_top.gate_en",
                top_hint=None,
                config=bootstrap_config,
            )
            operation_receipt = result.receipt
            retained_source_text_bytes = 0
            status = result.status
        operation_wall_ms = (time.perf_counter() - operation_started) * 1000.0
        output = {
            "mode": args.mode,
            "status": status,
            "fixture": {
                "compile_bytes": compile_log.stat().st_size,
                "compile_lines": args.compile_lines,
                "elaborate_bytes": elaborate_log.stat().st_size,
                "elaborate_lines": args.elaborate_lines,
                "source_count": args.source_count,
                "interface_count": args.interface_count,
                "source_bytes_each": args.source_bytes,
                "source_bytes_total": args.source_count * args.source_bytes,
            },
            "parse": parse_metrics,
            "operation_wall_ms": round(operation_wall_ms, 3),
            "rss_before_parse_kib": rss_before_kib,
            "rss_after_parse_kib": rss_after_parse_kib,
            "rss_end_kib": read_process_rss_kib(),
            "process_peak_rss_kib": resource.getrusage(resource.RUSAGE_SELF).ru_maxrss,
            "retained_source_text_bytes": retained_source_text_bytes,
            "receipt": operation_receipt,
        }
        print(json.dumps(output, sort_keys=True))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
