from pathlib import Path

from scripts.benchmark_tb_hierarchy import (
    BENCHMARK_NAME,
    build_argument_parser,
    run_benchmark,
)


def _write_fixture(root: Path) -> Path:
    leaf = root / "leaf.sv"
    top = root / "top.sv"
    leaf.write_text("module leaf; endmodule\n")
    top.write_text("module top; leaf u_leaf(); endmodule\n")
    compile_log = root / "build.log"
    compile_log.write_text(
        "Chronologic VCS simulator\n"
        f"Command: vcs -sverilog {leaf} {top} -top top\n"
        f"Parsing design file '{leaf}'\n"
        f"Parsing design file '{top}'\n"
        "Top Level Modules:\n"
        "       top\n"
    )
    return compile_log


def test_real_hierarchy_benchmark_reports_stable_oracle_without_paths(tmp_path):
    compile_log = _write_fixture(tmp_path)
    args = build_argument_parser().parse_args(
        ["--compile-log", str(compile_log), "--simulator", "vcs"]
    )

    first = run_benchmark(args)
    second = run_benchmark(args)

    assert first["benchmark"] == BENCHMARK_NAME
    assert first["workload"] == {
        "simulator": "vcs",
        "compile_log_count": 1,
        "npi_source_overlay": False,
        "hierarchy_template_sharing": True,
    }
    assert first["hierarchy_oracle"]["file_count"] == 2
    assert first["hierarchy_oracle"]["instance_count"] == 1
    assert len(first["hierarchy_oracle"]["structural_sha256"]) == 64
    assert first["hierarchy_oracle"]["structural_sha256"] == (
        second["hierarchy_oracle"]["structural_sha256"]
    )
    assert first["preprocessor_metrics"]["preprocessor_source_load_count"] == 2
    rendered = str(first)
    assert str(tmp_path) not in rendered


def test_real_hierarchy_benchmark_parser_defaults_to_no_npi_overlay():
    args = build_argument_parser().parse_args(["--compile-log", "/tmp/build.log"])

    assert args.simulator == "auto"
    assert args.npi_source_overlay is False
    assert args.hierarchy_template_sharing is True
    assert args.supplementary_compile_log == []
