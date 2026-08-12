from __future__ import annotations

import json
from pathlib import Path

import pytest

from scripts import benchmark_source_graph_soc as benchmark


def _args(tmp_path: Path, *extra: str):
    compile_log = tmp_path / "build.log"
    wave = tmp_path / "waves.fsdb"
    frontend = tmp_path / "python"
    compile_log.write_text("vcs -top soc_top rtl/top.sv\n", encoding="utf-8")
    wave.write_bytes(b"fixture")
    frontend.write_text("#!/bin/sh\n", encoding="utf-8")
    frontend.chmod(0o700)
    return benchmark.build_argument_parser().parse_args(
        [
            "--compile-log",
            str(compile_log),
            "--wave-path",
            str(wave),
            "--driver-signal",
            "tb.dut.u_cpu.err_o",
            "--load-signal",
            "tb.dut.u_cpu.bus_err",
            "--from-signal",
            "tb.dut.u_cpu.bus_err",
            "--to-signal",
            "tb.dut.u_cpu.err_o",
            "--frontend-python",
            str(frontend),
            *extra,
        ]
    )


def _query(operation: str, phase: str, *, facts: str = "same") -> dict:
    return {
        "operation": operation,
        "phase": phase,
        "wall_ms": 10.0 if phase == "cold" else 1.0,
        "actual_backend": "source_graph",
        "single_backend_provenance": True,
        "facts_sha256": facts,
        "source_graph": {
            "cache_tier": "build" if phase == "cold" else "memory",
            "effective_cache_tier": "build" if phase == "cold" else "memory",
            "metrics": {
                "prepare_total_wall_ms": 8.0,
                "build_wall_ms": 7.0,
                "worker_cpu_ms": 6.0,
                "rss_peak_kib": 100,
                "actual_build_count": 1 if phase == "cold" else 0,
                "frontend_launch_count": 1 if phase == "cold" else 0,
                "disk_hit_count": 0,
                "disk_miss_count": 0,
                "disk_corrupt_count": 0,
            },
        },
    }


def _run() -> dict:
    return {
        "preparation": {
            "wall_ms": 20.0,
            "hierarchy": {"stats": {"total_instances": 100}},
            "structural_scan": {"coverage_status": "complete"},
        },
        "queries": [
            _query("driver", "cold"),
            _query("driver", "memory"),
            _query("loads", "memory"),
            _query("path", "memory"),
        ],
        "process_memory": {"rss_peak_kib_after": 200},
    }


def test_distribution_uses_interpolated_p95():
    assert benchmark._distribution([1, 2, 3, None]) == {
        "n": 3,
        "min": 1.0,
        "median": 2.0,
        "p95": 2.9,
        "max": 3.0,
    }


def test_aggregate_distinguishes_cold_memory_and_disk_activity():
    result = benchmark._aggregate_runs([_run(), _run()])

    assert result["fresh_process_count"] == 2
    assert result["query_count"] == 8
    assert result["cache_tier_counts"] == {"build": 2, "memory": 6, "disk": 0}
    assert result["actual_build_count"] == 2
    assert result["frontend_launch_count"] == 2
    assert result["frontend_launch_metric_sample_count"] == 2
    assert result["all_queries_source_graph"] is True
    assert result["all_memory_queries_hit_memory"] is True
    assert result["disk_cache_inactive"] is True
    assert result["stable_facts"] is True


def test_aggregate_does_not_turn_omitted_launch_metric_into_zero():
    run = _run()
    del run["queries"][0]["source_graph"]["metrics"]["frontend_launch_count"]

    result = benchmark._aggregate_runs([run])

    assert result["actual_build_count"] == 1
    assert result["frontend_launch_count"] is None
    assert result["frontend_launch_metric_sample_count"] == 0


def test_aggregate_rejects_backend_fallback_and_unstable_facts():
    first = _run()
    second = _run()
    second["queries"][1]["actual_backend"] = "static"
    second["queries"][2]["facts_sha256"] = "changed"

    result = benchmark._aggregate_runs([first, second])

    assert result["all_queries_source_graph"] is False
    assert result["all_memory_queries_hit_memory"] is False
    assert result["stable_facts"] is False


def test_validate_args_accepts_relative_common_soc_paths(tmp_path, monkeypatch):
    args = _args(tmp_path)
    monkeypatch.chdir(tmp_path)
    args.compile_log = Path("build.log")
    args.wave_path = Path("waves.fsdb")
    args.frontend_python = Path("python")

    benchmark._validate_args(args)

    assert args.compile_log.is_absolute()
    assert args.wave_path.is_absolute()
    assert args.frontend_python.is_absolute()


def test_validate_args_preserves_virtualenv_python_symlink(tmp_path):
    args = _args(tmp_path)
    system_python = tmp_path / "python3.11"
    system_python.write_text("#!/bin/sh\n", encoding="utf-8")
    system_python.chmod(0o700)
    venv_python = tmp_path / "venv-python"
    venv_python.symlink_to(system_python)
    args.frontend_python = venv_python

    benchmark._validate_args(args)

    assert args.frontend_python == venv_python
    assert args.frontend_python.is_symlink()


@pytest.mark.parametrize(
    ("extra", "message"),
    [
        (("--repeats", "0"), "repeat counts"),
        (("--warm-repeats", "0"), "repeat counts"),
        (("--driver-max-depth", "0"), "driver max depth"),
        (("--worker-timeout-seconds", "0"), "timeouts"),
    ],
)
def test_validate_args_rejects_invalid_bounds(tmp_path, extra, message):
    args = _args(tmp_path, *extra)
    with pytest.raises(benchmark.BenchmarkInputError, match=message):
        benchmark._validate_args(args)


def test_run_benchmark_records_generic_workload_and_gate(tmp_path, monkeypatch):
    args = _args(tmp_path, "--repeats", "2")
    monkeypatch.setattr(benchmark, "_run_fresh_process", lambda unused: _run())
    monkeypatch.setattr(benchmark, "_git_head", lambda unused: "abc123")

    result = benchmark.run_benchmark(args)

    assert result["benchmark"] == "source_graph_soc_p3"
    assert result["workload"]["simulator"] == "auto"
    assert result["measurement_policy"]["production_backend_order_preserved"] is True
    assert result["measurement_policy"]["disk_cache_enabled"] is False
    assert result["aggregate"]["fresh_process_count"] == 2
    assert result["assessment"] == {
        "decision": "p3_source_graph_soc_baseline_recorded",
        "passed": True,
        "hierarchy_stats_stable": True,
        "structural_scan_complete": True,
        "source_graph_coverage_claim": "preserved_from_each_query_receipt",
        "performance_improvement_claimed": False,
    }


def test_write_json_atomic_round_trips(tmp_path):
    output = tmp_path / "nested/result.json"
    benchmark._write_json_atomic(output, {"value": 7})
    assert json.loads(output.read_text(encoding="utf-8")) == {"value": 7}
