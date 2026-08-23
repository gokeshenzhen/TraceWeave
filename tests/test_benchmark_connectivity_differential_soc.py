from __future__ import annotations

import json
from pathlib import Path

import pytest

from scripts import benchmark_connectivity_differential_soc as benchmark


def _write_corpus(path: Path, queries: list[dict]) -> Path:
    path.write_text(
        json.dumps({"schema_version": "1.0", "queries": queries}),
        encoding="utf-8",
    )
    return path


def _query(query_sha256: str, **overrides) -> dict:
    result = {
        "query_sha256": query_sha256,
        "operation": "loads",
        "positive": True,
        "fact_anchor_sha256": ["shared", "npi-only"],
        "evidence_locus_sha256": ["shared-locus"],
        "source_file_sha256": ["shared-file"],
        "search_exhaustive": False,
        "path_hops": None,
    }
    result.update(overrides)
    return result


def test_corpus_normalization_applies_bounded_defaults(tmp_path: Path):
    corpus = _write_corpus(
        tmp_path / "queries.json",
        [
            {
                "name": "driver case",
                "operation": "driver",
                "signal_path": "tb.dut.value",
            },
            {
                "operation": "loads",
                "signal_path": "tb.dut.request",
            },
            {
                "operation": "path",
                "from_signal": "tb.dut.a",
                "to_signal": "tb.dut.b",
            },
        ],
    )

    queries = benchmark.load_corpus(corpus)

    assert queries[0] == {
        "operation": "driver",
        "signal_path": "tb.dut.value",
        "recursive": True,
        "max_depth": 10,
    }
    assert queries[1]["max_depth"] == 1
    assert queries[2]["expand_assigns"] is False
    assert all("name" not in query for query in queries)


@pytest.mark.parametrize(
    "payload",
    (
        {"schema_version": "2.0", "queries": []},
        {"schema_version": "1.0", "queries": []},
        {
            "schema_version": "1.0",
            "queries": [
                {"operation": "loads", "signal_path": "tb.dut.value"}
            ]
            * (benchmark.MAX_CORPUS_QUERIES + 1),
        },
        {
            "schema_version": "1.0",
            "queries": [{"operation": "loads", "signal_path": "unscoped"}],
        },
        {
            "schema_version": "1.0",
            "queries": [
                {
                    "operation": "driver",
                    "signal_path": "tb.dut.value",
                    "max_depth": 0,
                }
            ],
        },
    ),
)
def test_corpus_rejects_invalid_or_unbounded_inputs(tmp_path: Path, payload: dict):
    path = tmp_path / "queries.json"
    path.write_text(json.dumps(payload), encoding="utf-8")

    with pytest.raises(benchmark.BenchmarkInputError):
        benchmark.load_corpus(path)


def test_corpus_rejects_duplicate_queries_even_when_labels_differ(tmp_path: Path):
    corpus = _write_corpus(
        tmp_path / "queries.json",
        [
            {
                "name": "first label",
                "operation": "loads",
                "signal_path": "tb.dut.value",
            },
            {
                "name": "second label",
                "operation": "loads",
                "signal_path": "tb.dut.value",
            },
        ],
    )

    with pytest.raises(benchmark.BenchmarkInputError, match="duplicate"):
        benchmark.load_corpus(corpus)


def test_normalized_result_contains_no_raw_signal_source_or_scope():
    signal = "tb.secret_block.secret_signal"
    source = "/private/project/src/secret_block.sv"
    scope = "tb.secret_block.secret_consumer"
    query = {
        "operation": "loads",
        "signal_path": signal,
        "max_depth": 1,
    }
    raw = {
        "loads": [
            {
                "load_path": scope,
                "kind": "rhs_expr",
                "source_file": source,
                "source_line": 42,
            }
        ],
        "enumeration": {"search_exhaustive": True},
    }

    normalized = benchmark.normalize_query_result(
        "npi", query, raw, npi_load_quality="clean"
    )
    encoded = json.dumps(normalized, sort_keys=True)

    assert signal not in encoded
    assert source not in encoded
    assert scope not in encoded
    assert normalized["positive"] is True
    assert len(normalized["fact_anchor_sha256"]) == 1
    assert len(normalized["evidence_locus_sha256"]) == 1
    assert len(normalized["source_file_sha256"]) == 1


def test_comparison_separates_coverage_explained_and_unexpected_npi_facts():
    npi = {
        "queries": [
            _query("partial"),
            _query("complete", fact_anchor_sha256=["other-npi"]),
        ]
    }
    source_graph = {
        "queries": [
            _query(
                "partial",
                fact_anchor_sha256=["shared", "source-only"],
                search_exhaustive=False,
            ),
            _query(
                "complete",
                fact_anchor_sha256=["other-source"],
                search_exhaustive=True,
            ),
        ]
    }

    comparison = benchmark.compare_provider_runs(npi, source_graph)

    assert comparison["counts"]["common_fact"] == 1
    assert comparison["counts"]["common_evidence_locus"] == 2
    assert comparison["counts"]["common_source_file"] == 2
    assert comparison["counts"]["coverage_explained_npi_only_fact"] == 1
    assert comparison["counts"]["unexpected_npi_only_fact"] == 1
    assert comparison["counts"]["source_graph_only_fact"] == 2


def test_comparison_reports_path_reachability_and_hop_mismatches():
    npi = {
        "queries": [
            _query(
                "reachability",
                operation="path",
                positive=True,
                path_hops=2,
            ),
            _query("hops", operation="path", positive=True, path_hops=3),
        ]
    }
    source_graph = {
        "queries": [
            _query(
                "reachability",
                operation="path",
                positive=False,
                path_hops=0,
            ),
            _query("hops", operation="path", positive=True, path_hops=4),
        ]
    }

    comparison = benchmark.compare_provider_runs(npi, source_graph)

    assert comparison["counts"]["path_reachability_mismatch"] == 1
    assert comparison["counts"]["path_hop_count_mismatch"] == 1
