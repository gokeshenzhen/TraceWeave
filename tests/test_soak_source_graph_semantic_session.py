from __future__ import annotations

import argparse
import json
from pathlib import Path

import pytest

from scripts import soak_source_graph_semantic_session as soak


def _args() -> argparse.Namespace:
    return argparse.Namespace(
        semantic_idle_ttl=300.0,
        semantic_max_rss_kib=768 * 1024,
        semantic_max_instances=64,
        semantic_max_inputs=256,
    )


def _row(ordinal: int, *, persistent: bool) -> dict:
    operation_ms = 4_200.0 if persistent and ordinal == 0 else (
        1_500.0 if persistent else 4_000.0
    )
    cumulative = (
        4_200.0 + ordinal * 1_500.0
        if persistent
        else (ordinal + 1) * 4_000.0
    )
    return {
        "ordinal": ordinal,
        "query_sha256": f"{ordinal:064x}",
        "operation": "driver",
        "semantic_context_status": "selected" if persistent else "disabled",
        "prepare_status": "ready",
        "result_status": "resolved",
        "positive_fact": True,
        "match_count": 1,
        "coverage_status": "inconclusive",
        "fact_sha256": f"{ordinal + 100:064x}",
        "coverage_semantics_sha256": f"{ordinal + 200:064x}",
        "plan_wall_ms": 100.0,
        "prepare_wall_ms": operation_ms - 101.0,
        "query_wall_ms": 1.0,
        "operation_wall_ms": operation_ms,
        "cumulative_wall_ms": cumulative,
        "frontend_launch_count": int(not persistent or ordinal == 0),
        "semantic_session_hit_count": int(persistent and ordinal > 0),
        "semantic_session_miss_count": int(persistent and ordinal == 0),
        "semantic_session_restart_count": 0,
        "semantic_session_eviction_count": 0,
        "rss_peak_kib": 550_000 + (ordinal * 100 if persistent else 0),
        "rss_end_kib": 540_000 if persistent else 30_000,
        "ir_bytes": 2_000_000,
    }


def _run(*, persistent: bool) -> dict:
    rows = [_row(index, persistent=persistent) for index in range(20)]
    runtime = {
        "actual_build_count": 20,
        "frontend_launch_count": 1 if persistent else 20,
        "semantic_session_hit_count": 19 if persistent else 0,
        "semantic_session_miss_count": 1 if persistent else 0,
        "semantic_session_restart_count": 0,
        "semantic_session_eviction_count": 0,
        "cache_hit_count": 0,
        "cache_miss_count": 20,
        "cache_eviction_count": 12,
        "cache_peak_entry_count": 8,
        "cache_peak_bytes": 16_000_000,
        "cache_entry_count": 8,
        "cache_bytes": 16_000_000,
        "timeout_count": 0,
        "worker_failure_count": 0,
    }
    return {
        "mode": "persistent" if persistent else "one_shot",
        "query_count": 20,
        "hierarchy_wall_ms": 5_000.0,
        "sequence_wall_ms": rows[-1]["cumulative_wall_ms"],
        "queries": rows,
        "runtime": runtime,
        "process_memory": {"child_peak_rss_kib": 552_000},
    }


def test_query_spec_requires_unique_deep_semantic_queries(tmp_path: Path):
    path = tmp_path / "queries.json"
    path.write_text(
        json.dumps(
            {
                "schema_version": "1.0",
                "queries": [
                    {
                        "operation": "driver",
                        "signal_path": "tb.dut.err_o",
                        "recursive": True,
                        "max_depth": 10,
                    },
                    {
                        "operation": "loads",
                        "signal_path": "tb.dut.req_i",
                        "max_depth": 2,
                    },
                ],
            }
        ),
        encoding="utf-8",
    )

    queries = soak.load_query_spec(path, minimum_queries=2)

    assert queries[0]["recursive"] is True
    assert queries[1]["recursive"] is False
    assert soak._query_identity(queries[0]) != soak._query_identity(queries[1])


@pytest.mark.parametrize(
    "query",
    [
        {"operation": "driver", "signal_path": "tb.dut.err_o", "recursive": False},
        {"operation": "loads", "signal_path": "tb.dut.req_i", "max_depth": 1},
        {"operation": "path", "signal_path": "tb.dut.err_o"},
        {"operation": "driver", "signal_path": "tb.*.err_o"},
    ],
)
def test_query_spec_rejects_ineligible_or_non_exact_queries(query):
    with pytest.raises(soak.SoakInputError):
        soak._normalize_query(query)


def test_build_result_passes_long_session_but_keeps_default_opt_in(monkeypatch):
    monkeypatch.setattr(soak, "_git_head", lambda _root: "abc123")
    monkeypatch.setattr(soak, "_git_dirty", lambda: False)

    result = soak.build_result(_args(), _run(persistent=False), _run(persistent=True))

    assert result["assessment"]["implementation_gate_passed"] is True
    assert result["assessment"]["default_on_authorized"] is False
    assert result["assessment"]["decision"] == (
        "semantic_session_soak_passed_keep_opt_in_pending_multi_design_usage"
    )
    assert result["comparison"]["facts_equal"] is True
    assert result["comparison"]["break_even_ordinal"] == 2
    assert result["comparison"]["sequence_reduction_percent"] == 59.125
    assert result["comparison"]["persistent_rss_growth_kib"] == 1_900
    assert soak._contains_forbidden_report_key(result) is False


def test_build_result_fails_on_fact_drift_or_session_restart(monkeypatch):
    monkeypatch.setattr(soak, "_git_head", lambda _root: "abc123")
    monkeypatch.setattr(soak, "_git_dirty", lambda: False)
    persistent = _run(persistent=True)
    persistent["queries"][3]["fact_sha256"] = "f" * 64
    persistent["runtime"]["semantic_session_restart_count"] = 1

    result = soak.build_result(_args(), _run(persistent=False), persistent)

    assert result["assessment"]["implementation_gate_passed"] is False
    assert result["assessment"]["checks"]["fact_payloads_equal"] is False
    assert result["assessment"]["checks"]["no_session_restart_or_eviction"] is False
    assert result["assessment"]["default_on_authorized"] is False


def test_build_result_classifies_existing_artifact_scope_as_not_needed(monkeypatch):
    monkeypatch.setattr(soak, "_git_head", lambda _root: "abc123")
    monkeypatch.setattr(soak, "_git_dirty", lambda: False)
    persistent = _run(persistent=True)
    for row in persistent["queries"]:
        row["semantic_context_status"] = "artifact_scope_sufficient"
        row["semantic_session_hit_count"] = 0
        row["semantic_session_miss_count"] = 0
    persistent["runtime"]["semantic_session_hit_count"] = 0
    persistent["runtime"]["semantic_session_miss_count"] = 0

    result = soak.build_result(_args(), _run(persistent=False), persistent)

    assert result["assessment"]["implementation_gate_passed"] is False
    assert result["workload"]["semantic_session_eligibility"] == (
        "not_needed_existing_artifact_scope"
    )
    assert result["assessment"]["decision"] == (
        "semantic_session_not_needed_existing_artifact_scope_keep_opt_in"
    )
    assert soak._contains_forbidden_report_key(result) is False
