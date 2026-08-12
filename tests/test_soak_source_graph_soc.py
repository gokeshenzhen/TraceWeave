from __future__ import annotations

import json
from pathlib import Path

import pytest

from scripts import soak_source_graph_soc as soak


def _write_spec(path: Path) -> Path:
    path.write_text(
        json.dumps(
            [
                {
                    "tool": "explain_signal_driver",
                    "arguments": {"signal_path": "tb.dut.err_o"},
                },
                {
                    "tool": "find_signal_loads",
                    "arguments": {"signal_path": "tb.dut.err_i"},
                },
                {
                    "tool": "trace_signal_path",
                    "arguments": {
                        "from_signal": "tb.dut.err_i",
                        "to_signal": "tb.dut.err_o",
                    },
                },
            ]
        ),
        encoding="utf-8",
    )
    return path


def _query(tool: str, tier: str, *, hit: int = 0, miss: int = 0) -> dict:
    operation = soak.QUERY_TO_OPERATION[tool]
    return {
        "tool": tool,
        "operation": operation,
        "wall_ms": 2.0,
        "actual_backend": "source_graph",
        "single_backend_provenance": True,
        "positive_fact": True,
        "cache_tier": tier,
        "metrics": {
            "disk_hit_count": hit,
            "disk_miss_count": miss,
            "disk_corrupt_count": 0,
            "disk_build_skip_count": hit,
            "actual_build_count": miss,
            "frontend_launch_count": miss,
            "disk_eviction_count": 0,
            "disk_lookup_wall_ms": 1.0 if hit or miss else 0.0,
            "disk_read_wall_ms": 0.5 if hit else 0.0,
            "disk_entry_count": 3,
            "disk_bytes": 100,
        },
    }


def _run(index: int) -> dict:
    tier = "build" if index == 0 else "disk"
    hit = int(index > 0)
    miss = int(index == 0)
    return {
        "session_anchored": True,
        "hierarchy_stats": {"file_count": 10, "instance_count": 20},
        "structural_scan": {"coverage_status": "complete"},
        "queries": [
            _query("explain_signal_driver", tier, hit=hit, miss=miss),
            _query("find_signal_loads", tier, hit=hit, miss=miss),
            _query("trace_signal_path", "memory"),
            _query("explain_signal_driver", tier, hit=hit, miss=miss),
            _query("explain_signal_driver", tier, hit=hit, miss=miss),
            _query("explain_signal_driver", tier, hit=hit, miss=miss),
        ],
    }


def _telemetry_report() -> dict:
    return {
        "total_records": 40,
        "total_sessions": 5,
        "source_graph": {
            "calls_with_metrics": 30,
            "sessions_with_metrics": 5,
            "cache_tiers": {},
            "disk": {"hit_count": 20, "miss_count": 5, "corrupt_count": 0},
            "execution": {},
            "validation_outcomes": {},
            "by_tool": {},
        },
    }


def _args(tmp_path: Path):
    for name in ("build.log", "run.log", "waves.fsdb"):
        (tmp_path / name).write_text("fixture", encoding="utf-8")
    frontend = tmp_path / "venv-python"
    frontend.write_text("#!/bin/sh\n", encoding="utf-8")
    frontend.chmod(0o700)
    return soak.build_argument_parser().parse_args(
        [
            "--verif-root",
            str(tmp_path),
            "--compile-log",
            str(tmp_path / "build.log"),
            "--sim-log",
            str(tmp_path / "run.log"),
            "--wave-path",
            str(tmp_path / "waves.fsdb"),
            "--simulator",
            "vcs",
            "--top-hint",
            "tb",
            "--query-spec",
            str(_write_spec(tmp_path / "queries.json")),
            "--cache-root",
            str(tmp_path / "cache"),
            "--frontend-python",
            str(frontend),
        ]
    )


def test_query_spec_is_project_agnostic_and_requires_all_operations(tmp_path):
    spec = _write_spec(tmp_path / "queries.json")
    queries = soak._load_query_spec(spec)
    assert [query["tool"] for query in queries] == [
        "explain_signal_driver",
        "find_signal_loads",
        "trace_signal_path",
    ]


def test_query_spec_rejects_harness_owned_paths(tmp_path):
    spec = tmp_path / "queries.json"
    spec.write_text(
        json.dumps(
            [
                {
                    "tool": "explain_signal_driver",
                    "arguments": {
                        "signal_path": "tb.dut.q",
                        "compile_log": "/other/build.log",
                    },
                }
            ]
        ),
        encoding="utf-8",
    )
    with pytest.raises(soak.SoakInputError, match="overrides"):
        soak._load_query_spec(spec)


def test_build_result_passes_five_sessions_and_twenty_five_lookups(tmp_path):
    args = _args(tmp_path)
    soak._validate_args(args)
    result = soak._build_result(
        args,
        [_run(index) for index in range(5)],
        _telemetry_report(),
    )

    assert result["assessment"]["passed"] is True
    assert result["assessment"]["telemetry_cross_check_passed"] is True
    assert result["aggregate"]["fresh_process_count"] == 5
    assert result["aggregate"]["disk_lookup_outcome_count"] == 25
    assert result["aggregate"]["disk_miss_count"] == 5
    assert result["aggregate"]["disk_hit_count"] == 20
    assert result["aggregate"]["verified_disk_hits_skip_build"] is True
    assert result["aggregate"]["memory_hits_skip_disk"] is True
    assert soak._contains_forbidden_evidence_key(result) is False


def test_build_result_fails_when_disk_hit_launches_frontend(tmp_path):
    args = _args(tmp_path)
    soak._validate_args(args)
    runs = [_run(index) for index in range(5)]
    runs[1]["queries"][0]["metrics"]["actual_build_count"] = 1

    result = soak._build_result(args, runs, _telemetry_report())

    assert result["assessment"]["passed"] is False
    assert result["aggregate"]["verified_disk_hits_skip_build"] is False


def test_forbidden_evidence_key_detection_is_recursive():
    assert soak._contains_forbidden_evidence_key({"nested": [{"signal_path": "q"}]})
    assert soak._contains_forbidden_evidence_key({"artifact_digest": "abc"})
    assert not soak._contains_forbidden_evidence_key({"disk_hit_count": 3})
    assert not soak._contains_forbidden_evidence_key(
        {"trace_signal_path": {"calls": 2}}
    )


def test_cache_root_requires_opt_in_resume_when_nonempty(tmp_path):
    args = _args(tmp_path)
    soak._validate_args(args)
    args.cache_root.mkdir()
    (args.cache_root / "existing").write_text("keep", encoding="utf-8")

    with pytest.raises(soak.SoakInputError, match="not empty"):
        soak._prepare_cache_root(args)

    args.resume = True
    soak._prepare_cache_root(args)
    assert (args.cache_root / "existing").read_text(encoding="utf-8") == "keep"


def test_validate_args_preserves_frontend_virtualenv_symlink(tmp_path):
    args = _args(tmp_path)
    target = tmp_path / "python3.11"
    target.write_text("#!/bin/sh\n", encoding="utf-8")
    target.chmod(0o700)
    args.frontend_python.unlink()
    args.frontend_python.symlink_to(target)

    soak._validate_args(args)

    assert args.frontend_python.is_symlink()
