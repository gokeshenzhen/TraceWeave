from scripts.benchmark_hierarchy_materialization import (
    BENCHMARK_NAME,
    aggregate_runs,
    build_argument_parser,
    run_trial,
)


def test_hierarchy_materialization_benchmark_preserves_logical_tree():
    parser = build_argument_parser()
    eager = run_trial(
        parser.parse_args(
            ["--branches", "4", "--fanout", "3", "--mode", "eager"]
        )
    )
    shared = run_trial(
        parser.parse_args(
            ["--branches", "4", "--fanout", "3", "--mode", "shared"]
        )
    )
    aggregate = aggregate_runs([eager, shared])

    assert eager["benchmark"] == BENCHMARK_NAME
    assert eager["behavior_oracle"] == shared["behavior_oracle"]
    assert eager["behavior_oracle"]["logical_instance_count"] == 16
    assert eager["representation"]["hierarchy_physical_node_count"] == 16
    assert shared["representation"]["hierarchy_physical_node_count"] == 7
    assert shared["representation"]["hierarchy_template_reused_node_count"] == 9
    assert aggregate["comparison"]["behavior_equal"] is True


def test_hierarchy_materialization_benchmark_defaults_are_bounded():
    args = build_argument_parser().parse_args([])

    assert args.branches == 200
    assert args.fanout == 1000
    assert args.mode == "compare"
    assert args.repeats == 3
