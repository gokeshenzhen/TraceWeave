from pathlib import Path

from scripts.benchmark_structural_scan import (
    BENCHMARK_NAME,
    build_argument_parser,
    run_benchmark,
)


def _write_fixture(root: Path) -> Path:
    source = root / "top.sv"
    source.write_text(
        "module top(input logic [3:0] mode, output logic y);\n"
        "  always_comb if (mode == 4'h2) y = 1'b1;\n"
        "endmodule\n"
    )
    compile_log = root / "build.log"
    compile_log.write_text(
        "Chronologic VCS simulator\n"
        f"Command: vcs -sverilog {source} -top top\n"
        f"Parsing design file '{source}'\n"
        "Top Level Modules:\n"
        "       top\n"
    )
    return compile_log


def test_real_structural_benchmark_reports_stable_oracle_without_paths(
    tmp_path,
):
    compile_log = _write_fixture(tmp_path)
    args = build_argument_parser().parse_args(
        ["--compile-log", str(compile_log), "--simulator", "vcs"]
    )

    first = run_benchmark(args)
    second = run_benchmark(args)

    assert first["benchmark"] == BENCHMARK_NAME
    assert first["workload"] == {
        "simulator": "vcs",
        "categories": [
            "slice_overlap",
            "narrow_condition_injection",
            "multi_drive",
            "incomplete_case",
            "magic_condition",
        ],
    }
    assert first["measurement"]["source_open_count"] == 2
    assert first["structural_oracle"]["eligible_file_count"] == 1
    assert first["structural_oracle"]["total_risks"] == 1
    assert len(first["structural_oracle"]["result_sha256"]) == 64
    assert first["structural_oracle"]["result_sha256"] == (
        second["structural_oracle"]["result_sha256"]
    )
    assert str(tmp_path) not in str(first)


def test_structural_benchmark_accepts_bounded_category_selection():
    args = build_argument_parser().parse_args(
        [
            "--compile-log",
            "/tmp/build.log",
            "--category",
            "magic_condition",
        ]
    )

    assert args.simulator == "auto"
    assert args.category == ["magic_condition"]
