from __future__ import annotations

from pathlib import Path
from types import SimpleNamespace

import pytest

from scripts import benchmark_semantic_design_store as benchmark


def _run(mode: str, *digests: str) -> dict:
    return {
        "mode": mode,
        "measurement": {
            "targets": [
                {
                    "resolution": {"binding_oracle_sha256": digest},
                    "artifact": {"connectivity_oracle_sha256": digest},
                }
                for digest in digests
            ]
        },
    }


def _args(tmp_path: Path, *extra: str):
    compile_log = tmp_path / "build.log"
    frontend = tmp_path / "python"
    compile_log.write_text("vcs -top tb top.sv\n", encoding="utf-8")
    frontend.write_text("#!/bin/sh\n", encoding="utf-8")
    return benchmark.build_argument_parser().parse_args(
        [
            "--compile-log",
            str(compile_log),
            "--signal",
            "tb.dut.u_a.value",
            "--signal",
            "tb.dut.u_b.value",
            "--frontend-python",
            str(frontend),
            *extra,
        ]
    )


def test_semantic_store_comparison_requires_equal_ordered_binding_oracles():
    equal = benchmark.compare_runs(
        [
            _run("scoped", "a", "b"),
            _run("persistent_parent", "a", "b"),
            _run("union", "a", "b"),
        ]
    )
    changed = benchmark.compare_runs(
        [
            _run("scoped", "a", "b"),
            _run("persistent_parent", "a", "changed"),
            _run("union", "a", "changed"),
        ]
    )

    assert equal["all_binding_oracles_equal"] is True
    assert equal["scoped_persistent_connectivity_equal"] is True
    assert changed["all_binding_oracles_equal"] is False
    assert changed["scoped_persistent_connectivity_equal"] is False


def test_semantic_store_lca_preserves_exact_path_atoms():
    assert benchmark._lca(
        (
            ("tb", "tb.dut", "tb.dut.u_a"),
            ("tb", "tb.dut", "tb.dut.u_b"),
        )
    ) == "tb.dut"


def test_semantic_store_defaults_bound_parent_prefetch(tmp_path: Path):
    args = _args(tmp_path)

    benchmark._validate_args(args)

    assert args.mode == "compare"
    assert args.parent_max_children == 64
    assert args.parent_max_inputs == 256
    assert args.timeout_seconds == 240.0
    assert args.include_details is False


def test_semantic_store_strategy_summary_tracks_time_to_each_target():
    scoped = {
        "mode": "scoped",
        "hierarchy_wall_ms": 5.0,
        "plans": {},
        "measurement": {
            "prepares": [
                {
                    "prepare_wall_ms": 11.0,
                    "metrics": {
                        "build_wall_ms": 9.0,
                        "load_wall_ms": 2.0,
                        "frontend_launch_count": 1,
                        "rss_peak_kib": 100,
                        "ir_bytes": 20,
                    },
                },
                {
                    "prepare_wall_ms": 13.0,
                    "metrics": {
                        "build_wall_ms": 10.0,
                        "load_wall_ms": 3.0,
                        "frontend_launch_count": 1,
                        "rss_peak_kib": 120,
                        "ir_bytes": 30,
                    },
                },
            ],
            "targets": [
                {
                    "lookup_wall_ms": 0.1,
                    "resolution": {"binding_oracle_sha256": "a"},
                },
                {
                    "lookup_wall_ms": 0.2,
                    "resolution": {"binding_oracle_sha256": "b"},
                },
            ],
            "runtime": {"cache_bytes": 50},
            "process_memory": {
                "self_peak_rss_kib": 80,
                "child_peak_rss_kib": 120,
            },
        },
    }

    summary = benchmark._strategy_summary(scoped)

    assert summary["target_ready_after_ms"] == [11.0, 24.0]
    assert summary["aggregate_prepare_wall_ms"] == 24.0
    assert summary["serialized_ir_bytes_total"] == 50
    assert summary["retained_ir_bytes"] == 50
    assert summary["max_worker_peak_rss_kib"] == 120


def test_semantic_store_persistent_summary_amortizes_session_build():
    run = {
        "mode": "persistent_parent",
        "plans": {},
        "measurement": {
            "session": {
                "configure_wall_ms": 1.0,
                "parse_wall_ms": 2.0,
                "compilation_wall_ms": 3.0,
                "elaboration_wall_ms": 4.0,
                "diagnostics_wall_ms": 5.0,
                "compile_input_count": 7,
                "peak_rss_kib": 200,
            },
            "targets": [
                {
                    "projection_wall_ms": 6.0,
                    "serialization_wall_ms": 1.0,
                    "lookup_wall_ms": 0.1,
                    "resolution": {"binding_oracle_sha256": "a"},
                },
                {
                    "projection_wall_ms": 8.0,
                    "serialization_wall_ms": 2.0,
                    "lookup_wall_ms": 0.2,
                    "resolution": {"binding_oracle_sha256": "b"},
                },
            ],
        },
    }

    summary = benchmark._strategy_summary(run)

    assert summary["semantic_session_build_wall_ms"] == 15.0
    assert summary["target_ready_after_ms"] == [22.0, 32.0]
    assert summary["frontend_launch_count"] == 1


@pytest.mark.parametrize(
    ("option", "value"),
    (
        ("--timeout-seconds", "0"),
        ("--max-depth", "0"),
        ("--parent-max-children", "0"),
        ("--parent-max-children", "257"),
        ("--parent-max-inputs", "0"),
        ("--parent-max-inputs", "1025"),
    ),
)
def test_semantic_store_rejects_unbounded_budgets(
    tmp_path: Path,
    option: str,
    value: str,
):
    args = _args(tmp_path, option, value)

    with pytest.raises(benchmark.BenchmarkInputError, match="bounded"):
        benchmark._validate_args(args)


def test_semantic_store_requires_cross_scope_sequence(tmp_path: Path):
    args = _args(tmp_path)
    args.signal = args.signal[:1]

    with pytest.raises(benchmark.BenchmarkInputError, match="two"):
        benchmark._validate_args(args)


def test_semantic_store_child_parser_accepts_trailing_native_output(monkeypatch):
    monkeypatch.setattr(
        benchmark,
        "_child_command",
        lambda unused_args, unused_mode: ["unused"],
    )
    monkeypatch.setattr(
        benchmark.subprocess,
        "run",
        lambda *unused_args, **unused_kwargs: SimpleNamespace(
            returncode=0,
            stdout='{"mode":"scoped"}\nnative shutdown output\n',
        ),
    )

    assert benchmark._run_child(SimpleNamespace(), "scoped") == {
        "mode": "scoped"
    }
