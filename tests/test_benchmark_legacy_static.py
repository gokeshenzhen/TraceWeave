import json
from pathlib import Path
import subprocess
import sys

import pytest

from scripts import benchmark_legacy_static as benchmark


ROOT = Path(__file__).resolve().parents[1]
SCRIPT = ROOT / "scripts" / "benchmark_legacy_static.py"
DEEP_X_FIXTURE = ROOT / "tests" / "fixtures" / "deep_x_npi"


def _write_compile_log(path: Path, sources: list[Path]) -> None:
    parsed_files = "".join(
        f"Parsing design file '{source.resolve()}'\n" for source in sources
    )
    path.write_text(
        "Chronologic VCS simulator\n"
        f"{parsed_files}"
        "Top Level Modules:\n"
        "       uart_deep_x_tb\n",
        encoding="utf-8",
    )


def _command(compile_log: Path, wave_path: Path) -> list[str]:
    return [
        sys.executable,
        str(SCRIPT),
        "--compile-log",
        str(compile_log),
        "--wave-path",
        str(wave_path),
        "--cold-repeats",
        "2",
        "--same-process-repeats",
        "3",
    ]


def test_benchmark_reports_stable_static_baselines_without_npi(tmp_path):
    compile_log = tmp_path / "compile.log"
    sources = [
        DEEP_X_FIXTURE / "rtl" / "deep_uart_x.sv",
        DEEP_X_FIXTURE / "tb" / "deep_x_tb.sv",
    ]
    _write_compile_log(compile_log, sources)

    completed = subprocess.run(
        _command(compile_log, tmp_path / "identity-only.fsdb"),
        cwd=ROOT,
        stdin=subprocess.DEVNULL,
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
        text=True,
        timeout=30,
        check=False,
    )

    assert completed.returncode == 0, completed.stderr
    result = json.loads(completed.stdout)
    assert result["status"] == "ok"
    assert result["backend"] == "static"
    assert result["backend_selection"] == "forced_direct_no_probe_no_npi"
    assert result["correctness_outcome_taxonomy"] == [
        "resolved",
        "partial",
        "unsupported",
    ]
    assert result["stable_across_modes"] is True
    assert result["workload"]["wave_path_role"] == "identity_only_not_opened"
    assert result["workload"]["source_count"] == 2

    expected = {
        "explain_signal_driver": {
            "outcome": "partial",
            "depth": 0,
            "source_file": str(sources[1].resolve()),
            "source_line": None,
            "unsupported_reason": None,
            "coverage": {
                "driver_kind": "unknown",
                "stopped_at": "unresolved",
            },
        },
        "find_signal_loads": {
            "outcome": "partial",
            "depth": 1,
            "source_file": None,
            "source_line": None,
            "unsupported_reason": None,
            "coverage": {
                "completeness": "shallow_only",
                "stopped_at": "no_static_load_found",
            },
        },
        "trace_signal_path": {
            "outcome": "unsupported",
            "depth": 0,
            "source_file": None,
            "source_line": None,
            "unsupported_reason": "static_backend_no_path_api",
            "coverage": {"found": False, "hops": 0},
        },
    }

    for operation, expected_result in expected.items():
        cold = result["modes"]["cold_process"]["operations"][operation]
        repeated = result["modes"]["same_process"]["operations"][operation]
        assert cold["sample_count"] == 2
        assert repeated["sample_count"] == 3
        assert cold["result"]["stable_across_samples"] is True
        assert repeated["result"]["stable_across_samples"] is True
        assert cold["result"]["fingerprint_sha256"] == repeated["result"][
            "fingerprint_sha256"
        ]
        assert len(cold["result"]["fingerprint_sha256"]) == 64
        assert cold["result"]["size_bytes"] > 0

        correctness = cold["result"]["correctness"]
        for field in (
            "outcome",
            "depth",
            "source_file",
            "source_line",
            "unsupported_reason",
        ):
            assert correctness[field] == expected_result[field]
        for field, value in expected_result["coverage"].items():
            assert correctness["coverage"][field] == value

        for mode_result in (cold, repeated):
            for timing in (
                "query_wall_time_ms",
                "query_cpu_time_ms",
                "process_wall_time_ms",
                "process_cpu_time_ms",
            ):
                assert mode_result[timing]["p50"] >= 0
                assert mode_result[timing]["p95"] >= mode_result[timing]["p50"]
            for rss_point in ("start", "peak", "end"):
                rss = mode_result["rss_kib"][rss_point]
                expected_rss_fields = {
                    "supported",
                    "count",
                    "min",
                    "p50",
                    "p95",
                    "max",
                }
                assert expected_rss_fields <= set(rss)
                if rss["supported"]:
                    assert rss["p50"] > 0


def test_fingerprint_is_stable_across_checkout_paths():
    def workload(root: str) -> dict:
        return {
            "compile_log": f"{root}/work/compile.log",
            "wave_path": f"{root}/work/wave.fsdb",
            "source_paths": [f"{root}/rtl/design.sv"],
        }

    def result(root: str) -> dict:
        return {
            "signal": "top.out[7:0]",
            "wave_path": f"{root}/work/wave.fsdb",
            "compile_log": f"{root}/work/compile.log",
            "source_file": f"{root}/rtl/design.sv",
            "source_line": 17,
        }

    first = benchmark.result_artifact(result("/checkout/a"), workload("/checkout/a"))
    second = benchmark.result_artifact(
        result("/a/much/longer/checkout/b"),
        workload("/a/much/longer/checkout/b"),
    )

    assert first["fingerprint_sha256"] == second["fingerprint_sha256"]


def test_invalid_repeat_count_is_a_machine_classifiable_input_error():
    with pytest.raises(benchmark.BenchmarkInputError) as exc_info:
        benchmark.parse_args(["--cold-repeats", "0"])

    assert exc_info.value.error_code == "invalid_arguments"
    assert "value must be >= 1" in str(exc_info.value)


def test_missing_compile_log_fails_loudly_with_machine_readable_error(tmp_path):
    missing = tmp_path / "missing" / "compile.log"
    completed = subprocess.run(
        _command(missing, tmp_path / "identity-only.fsdb"),
        cwd=ROOT,
        stdin=subprocess.DEVNULL,
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
        text=True,
        timeout=10,
        check=False,
    )

    assert completed.returncode == 2
    assert completed.stdout == ""
    error = json.loads(completed.stderr)
    assert error["status"] == "error"
    assert error["error_code"] == "fixture_missing"
    assert str(missing.resolve()) in error["message"]


def test_missing_source_named_by_compile_log_fails_before_measurement(tmp_path):
    compile_log = tmp_path / "compile.log"
    missing_source = tmp_path / "rtl" / "missing.sv"
    _write_compile_log(compile_log, [missing_source])

    completed = subprocess.run(
        _command(compile_log, tmp_path / "identity-only.fsdb"),
        cwd=ROOT,
        stdin=subprocess.DEVNULL,
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
        text=True,
        timeout=10,
        check=False,
    )

    assert completed.returncode == 2
    assert completed.stdout == ""
    error = json.loads(completed.stderr)
    assert error["status"] == "error"
    assert error["error_code"] == "fixture_missing"
    assert str(missing_source.resolve()) in error["message"]
