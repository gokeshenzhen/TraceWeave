import pytest

from scripts import benchmark_source_graph_scope_reuse as benchmark


@pytest.mark.anyio
async def test_cross_projection_benchmark_skips_second_build():
    result = await benchmark.run_benchmark(delay_ms=0, repeats=2)

    assert result["benchmark"] == "source_graph_cross_projection_reuse_v1"
    assert result["result"]["first_statuses"] == ["ready", "ready"]
    assert result["result"]["second_statuses"] == ["ready", "ready"]
    assert result["result"]["worker_build_counts"] == [1, 1]
    assert result["result"]["second_cache_dispositions"] == [
        "hit_superset",
        "hit_superset",
    ]
    assert result["result"]["second_cache_lookup_reasons"] == [
        "dominating_artifact",
        "dominating_artifact",
    ]
    assert result["result"]["second_scope_relations"] == [
        "superset",
        "superset",
    ]
    assert result["result"]["selected_first_artifact"] is True
