from scripts import benchmark_connectivity_query_indexes as benchmark


def test_instance_resolution_benchmark_reports_stable_result():
    result = benchmark.run_benchmark(
        workload="instance-resolution",
        size=32,
        repeats=2,
    )

    assert result["benchmark"] == "source_graph_query_indexes_v1"
    assert result["workload"]["instance_count"] == 33
    assert result["result"]["resolved_width"] == 1
    assert result["result"]["stable_across_repeats"] is True


def test_wide_load_benchmark_reports_complete_ordered_match():
    result = benchmark.run_benchmark(
        workload="wide-load",
        size=64,
        repeats=2,
    )

    assert result["workload"]["signal_width"] == 64
    assert result["result"]["match_count"] == 1
    assert result["result"]["inspected_edge_count"] == 2
    assert result["result"]["query_truncated"] is False
    assert result["result"]["covered_bit_count"] == 64
    assert result["result"]["stable_across_repeats"] is True
