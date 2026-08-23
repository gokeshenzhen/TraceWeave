from pathlib import Path

from scripts.benchmark_compile_source_index import (
    BENCHMARK_NAME,
    aggregate_runs,
    build_argument_parser,
    run_trial,
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


def test_trial_reports_shared_index_and_stable_path_free_oracles(tmp_path):
    compile_log = _write_fixture(tmp_path)
    parser = build_argument_parser()
    enabled_args = parser.parse_args(
        [
            "--compile-log",
            str(compile_log),
            "--simulator",
            "vcs",
            "--trial-index",
            "enabled",
        ]
    )
    disabled_args = parser.parse_args(
        [
            "--compile-log",
            str(compile_log),
            "--simulator",
            "vcs",
            "--trial-index",
            "disabled",
        ]
    )

    enabled = run_trial(enabled_args)
    disabled = run_trial(disabled_args)
    aggregate = aggregate_runs([disabled, enabled])

    assert enabled["benchmark"] == BENCHMARK_NAME
    assert enabled["workload"]["source_file_count"] == 1
    assert enabled["source_index"]["runtime"][
        "compile_source_runtime_build_count"
    ] == 1
    assert enabled["measurement"]["source_open_count"] == 1
    assert disabled["measurement"]["source_open_count"] == 3
    assert aggregate["comparison"]["behavior_equal"] is True
    assert enabled["behavior_oracle"]["hierarchy_sha256"] == disabled[
        "behavior_oracle"
    ]["hierarchy_sha256"]
    assert enabled["behavior_oracle"]["structural_sha256"] == disabled[
        "behavior_oracle"
    ]["structural_sha256"]
    assert str(tmp_path) not in str(enabled)
