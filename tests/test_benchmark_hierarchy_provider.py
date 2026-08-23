from scripts.benchmark_hierarchy_provider import (
    BENCHMARK_NAME,
    aggregate_runs,
    build_argument_parser,
    run_trial,
)


def test_hierarchy_provider_benchmark_preserves_lookup_result():
    parser = build_argument_parser()
    legacy = run_trial(
        parser.parse_args(
            ["--instances", "20", "--lookups", "7", "--mode", "legacy"]
        )
    )
    provider = run_trial(
        parser.parse_args(
            ["--instances", "20", "--lookups", "7", "--mode", "provider"]
        )
    )
    aggregate = aggregate_runs([legacy, provider])

    assert legacy["benchmark"] == BENCHMARK_NAME
    assert legacy["behavior_oracle"] == provider["behavior_oracle"]
    assert provider["measurement"]["scope_wall_ms"] is not None
    assert aggregate["comparison"]["behavior_equal"] is True


def test_hierarchy_provider_benchmark_defaults_are_bounded():
    args = build_argument_parser().parse_args([])

    assert args.instances == 100_000
    assert args.lookups == 100
    assert args.mode == "compare"
    assert args.repeats == 3
