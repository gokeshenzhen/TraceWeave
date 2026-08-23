from scripts import benchmark_source_graph_query as benchmark


def test_bounded_high_fanout_benchmark_reports_stable_truncation():
    result = benchmark.run_benchmark(fanout=300, repeats=2, mode="bounded")

    assert result["benchmark"] == "source_graph_query_fanout_v1"
    assert result["result"]["match_count"] == 256
    assert result["result"]["inspected_edge_count"] == 257
    assert result["result"]["query_truncated"] is True
    assert result["result"]["match_truncated"] is True
    assert result["result"]["coverage_status"] == "inconclusive"
    assert result["result"]["coverage_gap_codes"] == ["query_match_limit"]
    assert result["result"]["stable_across_repeats"] is True


def test_full_counterfactual_benchmark_materializes_every_match():
    result = benchmark.run_benchmark(fanout=12, repeats=1, mode="full")

    assert result["result"]["match_count"] == 12
    assert result["result"]["inspected_edge_count"] == 12
    assert result["result"]["query_truncated"] is False
    assert result["result"]["coverage_status"] == "complete"
    assert result["result"]["coverage_gap_codes"] == []
