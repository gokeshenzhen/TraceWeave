from types import SimpleNamespace
from pathlib import Path
import os
import subprocess
import sys

from src.source_graph_runtime import (
    CacheDisposition,
    FlightDisposition,
    PrepareStatus,
    SourceGraphPrepareMetrics,
)
from scripts.benchmark_source_graph_initial_scope import (
    BENCHMARK_NAME,
    _NPI_OVERLAY_ENV,
    _without_npi_hierarchy_overlay,
    _prepare_summary,
    build_argument_parser,
)


ROOT = Path(__file__).resolve().parents[1]


def _outcome(*, build_count: int, wall_ms: float, peak_kib: int):
    return SimpleNamespace(
        status=PrepareStatus.READY,
        metrics=SourceGraphPrepareMetrics(
            cache_disposition=CacheDisposition.MISS,
            flight_disposition=FlightDisposition.BUILDER,
            total_wall_ms=wall_ms,
            build_wall_ms=wall_ms - 1.0,
            load_wall_ms=1.0,
            actual_build_count=build_count,
            frontend_launch_count=build_count,
            rss_peak_kib=peak_kib,
            ir_bytes=1234,
        ),
    )


def test_prepare_summary_aggregates_reactive_sequence():
    summary = _prepare_summary(
        (
            _outcome(build_count=1, wall_ms=11.0, peak_kib=100),
            _outcome(build_count=1, wall_ms=13.0, peak_kib=120),
        )
    )

    assert summary["statuses"] == ["ready", "ready"]
    assert summary["actual_build_count"] == 2
    assert summary["aggregate_prepare_wall_ms"] == 24.0
    assert summary["max_worker_peak_rss_kib"] == 120
    assert summary["final_ir_bytes"] == 1234


def test_parser_requires_explicit_strategy_and_inputs():
    parser = build_argument_parser()
    args = parser.parse_args(
        [
            "--compile-log",
            "/tmp/build.log",
            "--signal",
            "top.u.q",
            "--strategy",
            "bounded-adjacent",
        ]
    )

    assert BENCHMARK_NAME == "source_graph_initial_scope_ab_v1"
    assert args.strategy == "bounded-adjacent"
    assert args.recursive is True


def test_import_does_not_change_hierarchy_overlay_environment():
    env = os.environ.copy()
    env.pop(_NPI_OVERLAY_ENV, None)
    completed = subprocess.run(
        [
            sys.executable,
            "-c",
            (
                "import os; import scripts.benchmark_source_graph_initial_scope; "
                f"print(os.environ.get('{_NPI_OVERLAY_ENV}', 'unset'))"
            ),
        ],
        cwd=ROOT,
        env=env,
        check=True,
        capture_output=True,
        text=True,
    )

    assert completed.stdout.strip() == "unset"


def test_overlay_override_is_scoped(monkeypatch):
    monkeypatch.setenv(_NPI_OVERLAY_ENV, "force")

    with _without_npi_hierarchy_overlay():
        assert os.environ[_NPI_OVERLAY_ENV] == "off"

    assert os.environ[_NPI_OVERLAY_ENV] == "force"
