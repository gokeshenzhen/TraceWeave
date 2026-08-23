from __future__ import annotations

from pathlib import Path
from types import SimpleNamespace

import pytest

from scripts import benchmark_hierarchy_provider_soc as benchmark


def _resolution(
    target: str,
    *,
    definition: str = "definition",
    source: str = "source",
    line: int = 12,
) -> dict:
    return {
        "target_sha256": target,
        "status": "resolved",
        "ancestor_count": 2,
        "binding_oracle": [
            {
                "instance_path_sha256": "path",
                "definition_sha256": definition,
                "source_sha256": source,
                "source_line": line,
            }
        ],
    }


def _run(resolution: dict) -> dict:
    return {"targets": [{"resolution": resolution}]}


def test_source_identity_matches_absolute_and_kdb_relative_fusesoc_paths():
    absolute = (
        "/tmp/build/fusesoc-work/src/library/rtl/autogen/top.sv"
    )

    assert benchmark._source_identity(absolute) == (
        "src/library/rtl/autogen/top.sv"
    )
    assert benchmark._source_identity("src/library/rtl/autogen/top.sv") == (
        "src/library/rtl/autogen/top.sv"
    )


def test_provider_comparison_accepts_exact_identity_free_bindings():
    npi = _run(_resolution("target"))
    slang = _run(_resolution("target"))

    comparison = benchmark.compare_provider_runs(npi, slang)

    assert comparison["target_count"] == 1
    assert comparison["compared_binding_count"] == 1
    assert comparison["exact_binding_match"] is True
    assert not any(comparison["mismatch_counts"].values())


def test_provider_comparison_classifies_definition_and_source_mismatch():
    npi = _run(_resolution("target"))
    slang = _run(
        _resolution("target", definition="other", source="other", line=13)
    )

    comparison = benchmark.compare_provider_runs(npi, slang)

    assert comparison["exact_binding_match"] is False
    assert comparison["mismatch_counts"]["definition_mismatch"] == 1
    assert comparison["mismatch_counts"]["source_mismatch"] == 1
    assert comparison["mismatch_counts"]["source_line_mismatch"] == 1


def test_provider_benchmark_defaults_keep_exact_lookup_bounded(tmp_path: Path):
    compile_log = tmp_path / "build.log"
    frontend = tmp_path / "python"
    compile_log.write_text("vcs -top tb top.sv\n", encoding="utf-8")
    frontend.write_text("#!/bin/sh\n", encoding="utf-8")
    args = benchmark.build_argument_parser().parse_args(
        [
            "--compile-log",
            str(compile_log),
            "--signal",
            "tb.dut.value",
            "--frontend-python",
            str(frontend),
        ]
    )

    benchmark._validate_args(args)

    assert args.provider == "compare"
    assert args.max_candidate_paths == 256
    assert args.max_depth == 20
    assert args.timeout_seconds == 240.0


def test_child_parser_ignores_native_npi_shutdown_banner(monkeypatch):
    args = SimpleNamespace()
    monkeypatch.setattr(
        benchmark,
        "_child_command",
        lambda unused_args, unused_provider: ["unused"],
    )
    monkeypatch.setattr(
        benchmark.subprocess,
        "run",
        lambda *unused_args, **unused_kwargs: SimpleNamespace(
            returncode=0,
            stdout='{"provider":"npi"}\nNPI native shutdown banner\n',
        ),
    )

    assert benchmark._run_child(args, "npi") == {"provider": "npi"}


@pytest.mark.parametrize(
    ("option", "value"),
    (
        ("--timeout-seconds", "0"),
        ("--max-candidate-paths", "0"),
        ("--max-candidate-paths", "1025"),
        ("--max-depth", "0"),
    ),
)
def test_provider_benchmark_rejects_unbounded_inputs(
    tmp_path: Path,
    option: str,
    value: str,
):
    compile_log = tmp_path / "build.log"
    frontend = tmp_path / "python"
    compile_log.write_text("vcs -top tb top.sv\n", encoding="utf-8")
    frontend.write_text("#!/bin/sh\n", encoding="utf-8")
    args = benchmark.build_argument_parser().parse_args(
        [
            "--compile-log",
            str(compile_log),
            "--signal",
            "tb.dut.value",
            "--frontend-python",
            str(frontend),
            option,
            value,
        ]
    )

    with pytest.raises(benchmark.BenchmarkInputError, match="bounded"):
        benchmark._validate_args(args)
