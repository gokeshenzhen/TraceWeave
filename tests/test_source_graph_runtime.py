from __future__ import annotations

import asyncio
import base64
from dataclasses import replace
import hashlib
import json
from pathlib import Path
import subprocess
import sys
import threading

import pytest

from src.connectivity_ir import CoverageGap, CoverageReport, CoverageStatus
from src.source_graph_contract import (
    BoundaryMode,
    CompileInputManifest,
    CompileProjectionMode,
    ConnectivityTarget,
    CoverageBoundary,
    QueryOperation,
    ScopeRelation,
    RequestedCone,
    SOURCE_GRAPH_COMPILE_PROJECTION_GAP,
    SOURCE_GRAPH_WORKER_PROTOCOL_VERSION,
    SourceGraphArtifactIdentity,
    SourceGraphArtifactScopeReceipt,
    SourceGraphArtifactScope,
    SourceGraphBuildRequest,
    SourceGraphBuildScope,
    SourceGraphCompileProjection,
    SourceGraphIdentity,
    SourceGraphQueryIdentity,
    SourceGraphSemanticContext,
    SourceGraphScopeReceipt,
)
from src.source_graph_runtime import (
    CacheDisposition,
    CacheLookupReason,
    CacheTier,
    FlightDisposition,
    IsolatedSourceGraphProcessRunner,
    PrepareStatus,
    SourceGraphRuntime,
    WorkerBuildResult,
    WorkerResourceMetrics,
)
from src.source_graph_worker import _frontend_args
from tests.connectivity_ir_fixtures import build_hand_ir


def _fingerprint(label: str = "runtime") -> str:
    return hashlib.sha256(label.encode()).hexdigest()


def _scope(
    *,
    operation: QueryOperation = QueryOperation.DRIVER,
    signal_path: str = "sg_top.lane_data[15:8]",
    ancestors=("sg_top",),
    max_hops: int = 2,
    cone_paths=("sg_top",),
    boundary_paths=("sg_top",),
    exclusions=(),
) -> SourceGraphBuildScope:
    return SourceGraphBuildScope(
        design="hand_runtime_fixture",
        top="sg_top",
        target=ConnectivityTarget(
            operation=operation,
            signal_path=signal_path,
        ),
        hierarchy_ancestors=tuple(ancestors),
        requested_cone=RequestedCone(
            operation=operation,
            max_hops=max_hops,
            instance_paths=tuple(cone_paths),
        ),
        coverage_boundary=CoverageBoundary(
            mode=BoundaryMode.EXPLICIT,
            instance_paths=tuple(boundary_paths),
            objective_exclusions=tuple(exclusions),
        ),
    )


def _request(
    *,
    label: str = "runtime",
    scope: SourceGraphBuildScope | None = None,
    complete: bool = True,
    explicit_artifact: bool = True,
) -> SourceGraphBuildRequest:
    build_scope = scope or _scope()
    source_identity = SourceGraphIdentity(
        compile_inputs=CompileInputManifest(
            fingerprint=_fingerprint(label),
            ordered_inputs=(
                "tests/fixtures/source_graph_frontend/hand_connectivity.sv",
            ),
            ordered_options=("--compat", "all"),
            ordered_tops=("sg_top",),
            inputs_complete=True,
            options_complete=complete,
            tops_complete=True,
        ),
        frontend_name="hand_oracle",
        frontend_version="1.0",
    )
    artifact = None
    query = None
    if explicit_artifact:
        artifact = SourceGraphArtifactIdentity(
            source=source_identity,
            scope=SourceGraphArtifactScope.from_build_scope(
                build_scope,
                hierarchy_snapshot_sha256=_fingerprint("hierarchy_snapshot"),
            ),
            compile_snapshot_sha256=_fingerprint(f"compile_snapshot:{label}"),
            adapter_version="test_adapter_3_0",
            worker_protocol_version=SOURCE_GRAPH_WORKER_PROTOCOL_VERSION,
        )
        query = SourceGraphQueryIdentity.from_build_scope(build_scope)
    return SourceGraphBuildRequest(
        identity=source_identity,
        scope=build_scope,
        artifact=artifact,
        query=query,
    )


def _ready_result(request: SourceGraphBuildRequest) -> WorkerBuildResult:
    ir = build_hand_ir()
    gap_codes: tuple[str, ...] = ()
    if (
        SOURCE_GRAPH_COMPILE_PROJECTION_GAP
        in request.scope.coverage_boundary.objective_exclusions
    ):
        gap = CoverageGap(
            code=SOURCE_GRAPH_COMPILE_PROJECTION_GAP,
            message="fixture compile projection omits unrelated inputs",
            impact=CoverageStatus.INCONCLUSIVE,
        )
        ir = replace(
            ir,
            coverage=CoverageReport(
                status=CoverageStatus.INCONCLUSIVE,
                files_total=4,
                files_projected=3,
                gaps=(gap,),
            ),
        )
        gap_codes = (SOURCE_GRAPH_COMPILE_PROJECTION_GAP,)
    receipt = SourceGraphScopeReceipt(
        scope=request.scope,
        coverage_status=ir.coverage.status,
        gap_codes=gap_codes,
    )
    payload = ir.to_json_bytes()
    return WorkerBuildResult.ready(
        ir,
        receipt,
        metrics=WorkerResourceMetrics(
            wall_time_ms=2.0,
            cpu_time_ms=1.0,
            rss_start_kib=100,
            rss_peak_kib=150,
            rss_end_kib=120,
            ir_bytes=len(payload),
        ),
    )


def test_worker_replays_all_ordered_tops_from_compile_manifest():
    request = _request(explicit_artifact=False)
    request = replace(
        request,
        identity=replace(
            request.identity,
            compile_inputs=replace(
                request.identity.compile_inputs,
                ordered_tops=("bind_top", "sg_top"),
            ),
        ),
    )

    assert _frontend_args(request) == [
        "--compat",
        "all",
        "tests/fixtures/source_graph_frontend/hand_connectivity.sv",
        "--top",
        "bind_top",
        "--top",
        "sg_top",
    ]


def test_worker_replays_projected_inputs_and_only_the_proved_scope_top():
    request = _request(
        scope=_scope(exclusions=(SOURCE_GRAPH_COMPILE_PROJECTION_GAP,))
    )
    source = replace(
        request.identity,
        compile_inputs=replace(
            request.identity.compile_inputs,
            ordered_inputs=(
                "rtl/pkg.sv",
                "rtl/unrelated.sv",
                "tests/fixtures/source_graph_frontend/hand_connectivity.sv",
            ),
            ordered_tops=("bind_top", "sg_top"),
        ),
    )
    projection = SourceGraphCompileProjection(
        mode=CompileProjectionMode.HIERARCHY_DEPENDENCY_CLOSURE,
        ordered_inputs=(
            "rtl/pkg.sv",
            "tests/fixtures/source_graph_frontend/hand_connectivity.sv",
        ),
        full_input_count=3,
        seed_symbol_count=2,
        dependency_symbol_count=1,
    )
    artifact = replace(
        request.artifact_identity,
        source=source,
        compile_projection=projection,
    )
    request = replace(request, identity=source, artifact=artifact)

    assert _frontend_args(request) == [
        "--compat",
        "all",
        "rtl/pkg.sv",
        "tests/fixtures/source_graph_frontend/hand_connectivity.sv",
        "--top",
        "sg_top",
    ]


def test_worker_replays_semantic_context_instead_of_narrow_artifact_projection():
    request = _request(
        scope=_scope(exclusions=(SOURCE_GRAPH_COMPILE_PROJECTION_GAP,))
    )
    source = replace(
        request.identity,
        compile_inputs=replace(
            request.identity.compile_inputs,
            ordered_inputs=(
                "rtl/pkg.sv",
                "rtl/left.sv",
                "rtl/right.sv",
                "tests/fixtures/source_graph_frontend/hand_connectivity.sv",
            ),
        ),
    )
    narrow = SourceGraphCompileProjection(
        mode=CompileProjectionMode.HIERARCHY_DEPENDENCY_CLOSURE,
        ordered_inputs=(
            "rtl/pkg.sv",
            "tests/fixtures/source_graph_frontend/hand_connectivity.sv",
        ),
        full_input_count=4,
    )
    context_projection = SourceGraphCompileProjection(
        mode=CompileProjectionMode.HIERARCHY_DEPENDENCY_CLOSURE,
        ordered_inputs=(
            "rtl/pkg.sv",
            "rtl/right.sv",
            "tests/fixtures/source_graph_frontend/hand_connectivity.sv",
        ),
        full_input_count=4,
    )
    artifact = replace(
        request.artifact_identity,
        source=source,
        compile_projection=narrow,
        semantic_context=SourceGraphSemanticContext(
            scope=request.artifact_identity.scope,
            compile_projection=context_projection,
        ),
    )
    request = replace(request, identity=source, artifact=artifact)

    assert _frontend_args(request) == [
        "--compat",
        "all",
        "rtl/pkg.sv",
        "rtl/right.sv",
        "tests/fixtures/source_graph_frontend/hand_connectivity.sv",
        "--top",
        "sg_top",
    ]


def test_worker_keeps_vhdl_in_identity_but_not_slang_arguments():
    request = _request(explicit_artifact=False)
    request = replace(
        request,
        identity=replace(
            request.identity,
            compile_inputs=replace(
                request.identity.compile_inputs,
                ordered_inputs=(
                    "rtl/leaf.vhd",
                    "tests/fixtures/source_graph_frontend/hand_connectivity.sv",
                    "rtl/legacy.vhdl",
                ),
            ),
        ),
    )

    args = _frontend_args(request)

    assert "rtl/leaf.vhd" not in args
    assert "rtl/legacy.vhdl" not in args
    assert "tests/fixtures/source_graph_frontend/hand_connectivity.sv" in args


class ImmediateWorker:
    def __init__(self, results=None):
        self.count = 0
        self.results = list(results or [])
        self.timeout_seconds = []

    async def run(self, request, *, timeout_seconds, cancel_event):
        self.count += 1
        self.timeout_seconds.append(timeout_seconds)
        if self.results:
            result = self.results.pop(0)
            return result(request) if callable(result) else result
        return _ready_result(request)


class ControlledWorker:
    def __init__(self, result=None):
        self.count = 0
        self.result = result
        self.started = asyncio.Event()
        self.release = asyncio.Event()
        self.cancel_seen = False

    async def run(self, request, *, timeout_seconds, cancel_event):
        self.count += 1
        self.started.set()
        release_wait = asyncio.create_task(self.release.wait())
        cancel_wait = asyncio.create_task(cancel_event.wait())
        try:
            done, _ = await asyncio.wait(
                {release_wait, cancel_wait}, return_when=asyncio.FIRST_COMPLETED
            )
            if cancel_wait in done and cancel_event.is_set():
                self.cancel_seen = True
                return WorkerBuildResult.failed(
                    PrepareStatus.CANCELLED,
                    code="request_cancelled",
                    stage="worker_process",
                    metrics=WorkerResourceMetrics(cancel_to_exit_ms=0.1),
                )
            if self.result is not None:
                return self.result(request) if callable(self.result) else self.result
            return _ready_result(request)
        finally:
            for task in (release_wait, cancel_wait):
                if not task.done():
                    task.cancel()
            await asyncio.gather(release_wait, cancel_wait, return_exceptions=True)


@pytest.mark.parametrize(
    "kwargs",
    (
        {"incomplete_handoff_max_entries": 0},
        {"incomplete_handoff_max_entries": True},
        {"incomplete_handoff_max_bytes": 0},
        {"incomplete_handoff_max_bytes": True},
        {"incomplete_handoff_ttl_seconds": 0},
        {"incomplete_handoff_ttl_seconds": True},
        {"incomplete_handoff_ttl_seconds": float("nan")},
        {"incomplete_handoff_ttl_seconds": 3_600.1},
    ),
)
def test_incomplete_handoff_bounds_are_strict(kwargs):
    with pytest.raises(ValueError):
        SourceGraphRuntime(ImmediateWorker(), **kwargs)


@pytest.mark.anyio
async def test_cold_prepare_then_exact_memory_hit_builds_once():
    worker = ImmediateWorker()
    runtime = SourceGraphRuntime(worker)
    request = _request()

    cold = await runtime.prepare(request)
    warm = await runtime.prepare(request)

    assert cold.status is PrepareStatus.READY
    assert cold.metrics.cache_disposition is CacheDisposition.MISS
    assert cold.cache_lookup_reason is CacheLookupReason.NO_CACHED_ARTIFACT
    assert cold.metrics.actual_build_count == 1
    assert warm.status is PrepareStatus.READY
    assert warm.metrics.cache_disposition is CacheDisposition.HIT_EXACT
    assert warm.cache_lookup_reason is CacheLookupReason.EXACT_ARTIFACT
    assert warm.metrics.actual_build_count == 0
    assert worker.count == 1
    assert warm.entry is cold.entry
    assert warm.entry.ir_bytes == len(warm.entry.ir_json_bytes)
    assert warm.entry.cache_bytes == warm.entry.ir_bytes
    assert warm.entry.ir_fingerprint_sha256 == warm.entry.ir.fingerprint_sha256()
    assert warm.entry.scope_receipt.scope == request.scope
    query = warm.entry.query_engine.query_driver("sg_top.lane_data[15:8]")
    assert query.status.value == "found"
    assert runtime.stats_snapshot()["cache_entry_count"] == 1


@pytest.mark.anyio
async def test_prepare_receipt_records_validated_effective_timeout():
    worker = ImmediateWorker()
    runtime = SourceGraphRuntime(worker)

    outcome = await runtime.prepare(_request(), timeout_seconds=7.5)

    assert worker.timeout_seconds == [7.5]
    assert outcome.effective_timeout_sec == 7.5
    assert outcome.to_receipt()["effective_timeout_sec"] == 7.5

    for invalid in (True, 0.0, float("nan"), float("inf"), 86_400.1):
        with pytest.raises(ValueError):
            await runtime.prepare(
                _request(label=f"invalid_{invalid}"),
                timeout_seconds=invalid,
            )


@pytest.mark.anyio
async def test_proven_superset_hits_but_subset_builds_again():
    worker = ImmediateWorker()
    runtime = SourceGraphRuntime(worker)
    superset_scope = _scope(
        signal_path="sg_top.u_bridge.q",
        ancestors=("sg_top", "sg_top.u_bridge"),
        max_hops=4,
        cone_paths=("sg_top", "sg_top.u_bridge"),
        boundary_paths=("sg_top", "sg_top.u_bridge"),
    )
    requested_scope = _scope(max_hops=2)
    larger_scope = _scope(
        signal_path="sg_top.u_other.q",
        ancestors=("sg_top", "sg_top.u_other"),
        max_hops=6,
        cone_paths=("sg_top", "sg_top.u_other"),
        boundary_paths=("sg_top", "sg_top.u_other"),
    )

    await runtime.prepare(_request(scope=superset_scope))
    hit = await runtime.prepare(_request(scope=requested_scope))
    miss = await runtime.prepare(_request(scope=larger_scope))

    assert hit.metrics.cache_disposition is CacheDisposition.HIT_SUPERSET
    assert hit.cache_lookup_reason is CacheLookupReason.DOMINATING_ARTIFACT
    assert hit.scope_match.relation.value == "superset"
    assert miss.metrics.cache_disposition is CacheDisposition.MISS
    assert miss.cache_lookup_reason is CacheLookupReason.CACHED_SCOPE_NOT_DOMINATING
    assert worker.count == 2


@pytest.mark.anyio
async def test_compile_projection_superset_reuses_dominating_scope_artifact():
    worker = ImmediateWorker()
    runtime = SourceGraphRuntime(worker)
    full_inputs = tuple(f"rtl/input_{index}.sv" for index in range(4))
    superset_scope = _scope(
        signal_path="sg_top.u_bridge.q",
        ancestors=("sg_top", "sg_top.u_bridge"),
        max_hops=4,
        cone_paths=("sg_top", "sg_top.u_bridge"),
        boundary_paths=("sg_top", "sg_top.u_bridge"),
        exclusions=(SOURCE_GRAPH_COMPILE_PROJECTION_GAP,),
    )
    available = _request(scope=superset_scope)
    requested = _request(
        scope=_scope(
            max_hops=2,
            exclusions=(SOURCE_GRAPH_COMPILE_PROJECTION_GAP,),
        )
    )
    source = replace(
        available.identity,
        compile_inputs=replace(
            available.identity.compile_inputs,
            ordered_inputs=full_inputs,
        ),
    )

    def projected(request, count):
        artifact = replace(
            request.artifact_identity,
            source=source,
            compile_projection=SourceGraphCompileProjection(
                mode=CompileProjectionMode.HIERARCHY_DEPENDENCY_CLOSURE,
                ordered_inputs=full_inputs[:count],
                full_input_count=len(full_inputs),
            ),
        )
        return replace(request, identity=source, artifact=artifact)

    available = projected(available, 3)
    requested = projected(requested, 2)

    cold = await runtime.prepare(available)
    hit = await runtime.prepare(requested)

    assert cold.entry.artifact_identity == available.artifact_identity
    with pytest.raises(ValueError, match="same scope"):
        replace(cold.entry, artifact_identity=requested.artifact_identity)
    assert hit.status is PrepareStatus.READY
    assert hit.metrics.cache_disposition is CacheDisposition.HIT_SUPERSET
    assert hit.cache_lookup_reason is CacheLookupReason.DOMINATING_ARTIFACT
    assert hit.scope_match.relation is ScopeRelation.SUPERSET
    assert hit.entry is cold.entry
    assert worker.count == 1


@pytest.mark.anyio
async def test_compile_projection_subset_cannot_dominate_larger_request():
    worker = ImmediateWorker()
    runtime = SourceGraphRuntime(worker)
    full_inputs = tuple(f"rtl/input_{index}.sv" for index in range(4))
    request = _request(
        scope=_scope(exclusions=(SOURCE_GRAPH_COMPILE_PROJECTION_GAP,))
    )
    source = replace(
        request.identity,
        compile_inputs=replace(
            request.identity.compile_inputs,
            ordered_inputs=full_inputs,
        ),
    )

    def projected(count):
        return replace(
            request,
            identity=source,
            artifact=replace(
                request.artifact_identity,
                source=source,
                compile_projection=SourceGraphCompileProjection(
                    mode=CompileProjectionMode.HIERARCHY_DEPENDENCY_CLOSURE,
                    ordered_inputs=full_inputs[:count],
                    full_input_count=len(full_inputs),
                ),
            ),
        )

    await runtime.prepare(projected(2))
    miss = await runtime.prepare(projected(3))

    assert miss.metrics.cache_disposition is CacheDisposition.MISS
    assert miss.cache_lookup_reason is CacheLookupReason.CACHED_SCOPE_NOT_DOMINATING
    assert worker.count == 2


@pytest.mark.anyio
async def test_incomplete_key_uses_one_bounded_sequential_handoff():
    worker = ImmediateWorker()
    runtime = SourceGraphRuntime(worker)
    request = _request(complete=False)

    first = await runtime.prepare(request)
    second = await runtime.prepare(request)

    assert first.status is PrepareStatus.READY
    assert second.status is PrepareStatus.READY
    assert first.build_key.cross_request_reusable is False
    assert first.metrics.cache_disposition is CacheDisposition.BYPASS_INCOMPLETE_KEY
    assert second.metrics.cache_disposition is CacheDisposition.BYPASS_INCOMPLETE_KEY
    assert first.metrics.cache_tier is CacheTier.BUILD
    assert second.metrics.cache_tier is CacheTier.HANDOFF
    assert second.metrics.flight_disposition is FlightDisposition.NONE
    assert second.cache_lookup_reason is (
        CacheLookupReason.SAME_ARTIFACT_SESSION_HANDOFF
    )
    assert worker.count == 1
    assert runtime.stats_snapshot()["cache_entry_count"] == 0
    assert runtime.stats_snapshot()["incomplete_handoff_entry_count"] == 0
    assert runtime.stats_snapshot()["incomplete_handoff_publish_count"] == 1
    assert runtime.stats_snapshot()["incomplete_handoff_hit_count"] == 1

    third = await runtime.prepare(request)
    fourth = await runtime.prepare(request)
    assert third.metrics.flight_disposition is FlightDisposition.BUILDER
    assert fourth.metrics.cache_tier is CacheTier.HANDOFF
    assert worker.count == 2
    assert runtime.stats_snapshot()["cache_entry_count"] == 0
    assert runtime.stats_snapshot()["incomplete_handoff_entry_count"] == 0


@pytest.mark.anyio
async def test_incomplete_handoff_expires_and_rebuilds():
    worker = ImmediateWorker()
    runtime = SourceGraphRuntime(
        worker,
        incomplete_handoff_ttl_seconds=0.01,
    )
    request = _request(complete=False)

    await runtime.prepare(request)
    assert runtime.stats_snapshot()["incomplete_handoff_entry_count"] == 1
    await asyncio.sleep(0.02)
    assert runtime.stats_snapshot()["incomplete_handoff_entry_count"] == 0

    rebuilt = await runtime.prepare(request)
    stats = runtime.stats_snapshot()
    assert rebuilt.metrics.flight_disposition is FlightDisposition.BUILDER
    assert worker.count == 2
    assert stats["incomplete_handoff_expiration_count"] == 1
    assert stats["cache_entry_count"] == 0


@pytest.mark.anyio
async def test_incomplete_handoff_respects_byte_capacity():
    worker = ImmediateWorker()
    runtime = SourceGraphRuntime(
        worker,
        incomplete_handoff_max_bytes=1,
    )
    request = _request(complete=False)

    first = await runtime.prepare(request)
    second = await runtime.prepare(request)
    stats = runtime.stats_snapshot()

    assert first.status is PrepareStatus.READY
    assert second.status is PrepareStatus.READY
    assert worker.count == 2
    assert stats["incomplete_handoff_capacity_bypass_count"] == 2
    assert stats["incomplete_handoff_entry_count"] == 0
    assert stats["cache_entry_count"] == 0


@pytest.mark.anyio
async def test_incomplete_handoff_requires_content_anchored_identity():
    worker = ImmediateWorker()
    runtime = SourceGraphRuntime(worker)
    request = _request(complete=False)
    incomplete_inputs = replace(
        request.identity.compile_inputs,
        fingerprint=None,
    )
    incomplete_identity = replace(
        request.identity,
        compile_inputs=incomplete_inputs,
    )
    request = replace(
        request,
        identity=incomplete_identity,
        artifact=replace(
            request.artifact_identity,
            source=incomplete_identity,
        ),
    )

    await runtime.prepare(request)
    await runtime.prepare(request)
    stats = runtime.stats_snapshot()

    assert worker.count == 2
    assert stats["incomplete_handoff_ineligible_count"] == 2
    assert stats["incomplete_handoff_publish_count"] == 0
    assert stats["incomplete_handoff_entry_count"] == 0
    assert stats["cache_entry_count"] == 0


@pytest.mark.anyio
async def test_incomplete_handoff_evicts_oldest_identity_at_entry_bound():
    worker = ImmediateWorker()
    runtime = SourceGraphRuntime(worker)
    first_request = _request(label="first", complete=False)
    second_request = _request(label="second", complete=False)

    await runtime.prepare(first_request)
    await runtime.prepare(second_request)
    rebuilt = await runtime.prepare(first_request)
    stats = runtime.stats_snapshot()

    assert rebuilt.metrics.flight_disposition is FlightDisposition.BUILDER
    assert worker.count == 3
    assert stats["incomplete_handoff_eviction_count"] == 2
    assert stats["incomplete_handoff_entry_count"] == 1
    assert stats["incomplete_handoff_peak_entry_count"] == 1
    assert stats["cache_entry_count"] == 0


@pytest.mark.anyio
async def test_concurrent_incomplete_key_coalesces_then_leaves_one_handoff():
    worker = ControlledWorker()
    runtime = SourceGraphRuntime(worker)
    request = _request(complete=False)

    first_task = asyncio.create_task(runtime.prepare(request))
    await worker.started.wait()
    second_task = asyncio.create_task(runtime.prepare(request))
    await asyncio.sleep(0)
    worker.release.set()
    first, second = await asyncio.gather(first_task, second_task)
    stats = runtime.stats_snapshot()

    assert worker.count == 1
    assert first.entry is second.entry
    assert {first.metrics.flight_disposition, second.metrics.flight_disposition} == {
        FlightDisposition.BUILDER,
        FlightDisposition.COALESCED,
    }
    assert first.metrics.cache_disposition is CacheDisposition.BYPASS_INCOMPLETE_KEY
    assert second.metrics.cache_disposition is CacheDisposition.BYPASS_INCOMPLETE_KEY
    assert sum(item.metrics.actual_build_count for item in (first, second)) == 1
    assert stats["actual_build_count"] == 1
    assert stats["coalesced_waiter_count"] == 1
    assert stats["cache_entry_count"] == 0
    assert stats["inflight_count"] == 0
    assert stats["incomplete_handoff_entry_count"] == 1

    later = await runtime.prepare(request)
    assert later.metrics.flight_disposition is FlightDisposition.NONE
    assert later.metrics.cache_disposition is CacheDisposition.BYPASS_INCOMPLETE_KEY
    assert later.metrics.cache_tier is CacheTier.HANDOFF
    assert worker.count == 1
    assert runtime.stats_snapshot()["incomplete_handoff_entry_count"] == 0

    rebuilt = await runtime.prepare(request)
    assert rebuilt.metrics.flight_disposition is FlightDisposition.BUILDER
    assert worker.count == 2
    assert runtime.stats_snapshot()["cache_entry_count"] == 0


@pytest.mark.anyio
async def test_incomplete_driver_and_load_share_exact_artifact_flight():
    worker = ControlledWorker()
    runtime = SourceGraphRuntime(worker)
    requests = (
        _request(
            complete=False,
            scope=_scope(operation=QueryOperation.DRIVER, max_hops=10),
        ),
        _request(
            complete=False,
            scope=_scope(operation=QueryOperation.LOADS, max_hops=1),
        ),
    )

    first_task = asyncio.create_task(runtime.prepare(requests[0]))
    await worker.started.wait()
    second_task = asyncio.create_task(runtime.prepare(requests[1]))
    await asyncio.sleep(0)
    worker.release.set()
    first, second = await asyncio.gather(first_task, second_task)

    assert first.build_key.digest == second.build_key.digest
    assert worker.count == 1
    assert first.entry is second.entry
    assert second.metrics.flight_disposition is FlightDisposition.COALESCED
    assert second.cache_lookup_reason is CacheLookupReason.SAME_ARTIFACT_INFLIGHT
    assert runtime.stats_snapshot()["cache_entry_count"] == 0
    assert runtime.stats_snapshot()["incomplete_handoff_entry_count"] == 1


@pytest.mark.anyio
async def test_incomplete_flight_one_waiter_cancel_keeps_other_alive():
    worker = ControlledWorker()
    runtime = SourceGraphRuntime(worker)
    request = _request(complete=False)
    first_cancel = asyncio.Event()
    second_cancel = asyncio.Event()

    first_task = asyncio.create_task(
        runtime.prepare(request, cancel_event=first_cancel)
    )
    await worker.started.wait()
    second_task = asyncio.create_task(
        runtime.prepare(request, cancel_event=second_cancel)
    )
    await asyncio.sleep(0)
    first_cancel.set()
    first = await first_task

    assert first.status is PrepareStatus.CANCELLED
    assert worker.cancel_seen is False

    worker.release.set()
    second = await second_task
    assert second.status is PrepareStatus.READY
    assert worker.count == 1
    assert worker.cancel_seen is False
    assert runtime.stats_snapshot()["cache_entry_count"] == 0


@pytest.mark.anyio
async def test_all_incomplete_waiters_cancel_worker_and_allow_retry():
    worker = ControlledWorker()
    runtime = SourceGraphRuntime(worker)
    request = _request(complete=False)
    first_cancel = asyncio.Event()
    second_cancel = asyncio.Event()

    first_task = asyncio.create_task(
        runtime.prepare(request, cancel_event=first_cancel)
    )
    await worker.started.wait()
    second_task = asyncio.create_task(
        runtime.prepare(request, cancel_event=second_cancel)
    )
    await asyncio.sleep(0)
    first_cancel.set()
    first = await first_task
    assert first.status is PrepareStatus.CANCELLED
    assert worker.cancel_seen is False

    second_cancel.set()
    second = await second_task
    await runtime.wait_idle()

    assert second.status is PrepareStatus.CANCELLED
    assert worker.count == 1
    assert worker.cancel_seen is True
    assert runtime.stats_snapshot()["inflight_count"] == 0
    assert runtime.stats_snapshot()["cache_entry_count"] == 0

    worker.started.clear()
    worker.release.set()
    retried = await runtime.prepare(request)
    assert retried.status is PrepareStatus.READY
    assert worker.count == 2
    assert runtime.stats_snapshot()["cache_entry_count"] == 0


@pytest.mark.anyio
async def test_incomplete_timeout_flight_clears_and_allows_retry():
    timeout = WorkerBuildResult.failed(
        PrepareStatus.TIMED_OUT,
        code="worker_timeout",
        stage="worker_process",
    )
    worker = ControlledWorker(timeout)
    runtime = SourceGraphRuntime(worker)
    request = _request(complete=False)

    first_task = asyncio.create_task(runtime.prepare(request))
    await worker.started.wait()
    second_task = asyncio.create_task(runtime.prepare(request))
    await asyncio.sleep(0)
    worker.release.set()
    first, second = await asyncio.gather(first_task, second_task)

    assert {first.status, second.status} == {PrepareStatus.TIMED_OUT}
    assert worker.count == 1
    assert runtime.stats_snapshot()["inflight_count"] == 0
    assert runtime.stats_snapshot()["cache_entry_count"] == 0

    worker.result = None
    retried = await runtime.prepare(request)
    assert retried.status is PrepareStatus.READY
    assert worker.count == 2
    assert runtime.stats_snapshot()["cache_entry_count"] == 0


@pytest.mark.anyio
async def test_timeout_participates_in_exact_flight_identity():
    worker = ControlledWorker()
    runtime = SourceGraphRuntime(worker)
    request = _request(complete=False)

    first_task = asyncio.create_task(
        runtime.prepare(request, timeout_seconds=1.0)
    )
    await worker.started.wait()
    second_task = asyncio.create_task(
        runtime.prepare(request, timeout_seconds=2.0)
    )
    await asyncio.sleep(0)
    worker.release.set()
    first, second = await asyncio.gather(first_task, second_task)

    assert worker.count == 2
    assert first.metrics.flight_disposition is FlightDisposition.BUILDER
    assert second.metrics.flight_disposition is FlightDisposition.BUILDER
    assert first.effective_timeout_sec == 1.0
    assert second.effective_timeout_sec == 2.0


@pytest.mark.anyio
async def test_concurrent_same_key_is_single_flight_with_one_actual_build():
    worker = ControlledWorker()
    runtime = SourceGraphRuntime(worker)
    request = _request()

    first_task = asyncio.create_task(runtime.prepare(request))
    await worker.started.wait()
    second_task = asyncio.create_task(runtime.prepare(request))
    await asyncio.sleep(0)
    worker.release.set()
    first, second = await asyncio.gather(first_task, second_task)

    assert worker.count == 1
    assert {first.metrics.flight_disposition, second.metrics.flight_disposition} == {
        FlightDisposition.BUILDER,
        FlightDisposition.COALESCED,
    }
    assert first.entry is second.entry
    assert sum(item.metrics.actual_build_count for item in (first, second)) == 1
    assert runtime.stats_snapshot()["actual_build_count"] == 1
    assert runtime.stats_snapshot()["coalesced_waiter_count"] == 1


@pytest.mark.anyio
async def test_concurrent_mixed_queries_share_one_artifact_flight():
    worker = ControlledWorker()
    runtime = SourceGraphRuntime(worker)
    requests = (
        _request(scope=_scope(signal_path="sg_top.lane_data[15:8]")),
        _request(scope=_scope(signal_path="sg_top.other_data")),
        _request(
            scope=_scope(
                operation=QueryOperation.LOADS,
                signal_path="sg_top.lane_data[15:8]",
                max_hops=5,
            )
        ),
    )

    tasks = [asyncio.create_task(runtime.prepare(request)) for request in requests]
    await worker.started.wait()
    await asyncio.sleep(0)
    worker.release.set()
    outcomes = await asyncio.gather(*tasks)

    assert worker.count == 1
    assert runtime.stats_snapshot()["actual_build_count"] == 1
    assert all(outcome.entry is outcomes[0].entry for outcome in outcomes)
    assert sum(outcome.metrics.actual_build_count for outcome in outcomes) == 1
    assert {outcome.metrics.flight_disposition for outcome in outcomes} == {
        FlightDisposition.BUILDER,
        FlightDisposition.COALESCED,
    }


@pytest.mark.anyio
async def test_cancelled_query_waiter_does_not_pollute_or_evict_ready_artifact():
    worker = ImmediateWorker()
    runtime = SourceGraphRuntime(worker)
    request = _request()
    ready = await runtime.prepare(request)
    cancel = asyncio.Event()
    cancel.set()

    cancelled = await runtime.prepare(
        _request(scope=_scope(signal_path="sg_top.other_data")),
        cancel_event=cancel,
    )
    warm = await runtime.prepare(request)

    assert ready.status is PrepareStatus.READY
    assert cancelled.status is PrepareStatus.CANCELLED
    assert warm.metrics.cache_disposition is CacheDisposition.HIT_EXACT
    assert worker.count == 1
    assert runtime.stats_snapshot()["cache_entry_count"] == 1


@pytest.mark.anyio
async def test_lru_entry_boundary_evicts_safely_and_rebuilds_oldest():
    worker = ImmediateWorker()
    runtime = SourceGraphRuntime(worker, max_cache_entries=2)
    requests = tuple(_request(label=f"artifact_{index}") for index in range(3))

    first = await runtime.prepare(requests[0])
    await runtime.prepare(requests[1])
    await runtime.prepare(requests[2])
    rebuilt = await runtime.prepare(requests[0])
    stats = runtime.stats_snapshot()

    assert first.entry is not None
    # The immutable entry remains usable through the first outcome even after
    # its cache reference is evicted.
    assert first.entry.query_engine.query_driver("sg_top.lane_data[15:8]").matches
    assert rebuilt.metrics.cache_disposition is CacheDisposition.MISS
    assert worker.count == 4
    assert stats["cache_entry_count"] == 2
    assert stats["cache_peak_entry_count"] == 2
    assert stats["cache_eviction_count"] == 2


@pytest.mark.anyio
async def test_cache_byte_boundary_bypasses_oversize_artifact_without_pollution():
    worker = ImmediateWorker()
    ir_bytes = len(build_hand_ir().to_json_bytes())
    runtime = SourceGraphRuntime(worker, max_cache_bytes=ir_bytes - 1)
    request = _request()

    first = await runtime.prepare(request)
    second = await runtime.prepare(request)
    stats = runtime.stats_snapshot()

    assert first.status is PrepareStatus.READY
    assert first.metrics.cache_disposition is CacheDisposition.BYPASS_CAPACITY
    assert second.metrics.cache_disposition is CacheDisposition.BYPASS_CAPACITY
    assert worker.count == 2
    assert stats["cache_entry_count"] == 0
    assert stats["cache_oversize_bypass_count"] == 2


@pytest.mark.anyio
async def test_one_cancelled_waiter_does_not_cancel_shared_worker():
    worker = ControlledWorker()
    runtime = SourceGraphRuntime(worker)
    request = _request()
    first_cancel = asyncio.Event()
    second_cancel = asyncio.Event()

    first_task = asyncio.create_task(
        runtime.prepare(request, cancel_event=first_cancel)
    )
    await worker.started.wait()
    second_task = asyncio.create_task(
        runtime.prepare(request, cancel_event=second_cancel)
    )
    await asyncio.sleep(0)
    first_cancel.set()
    first = await first_task
    assert first.status is PrepareStatus.CANCELLED
    assert worker.cancel_seen is False

    worker.release.set()
    second = await second_task
    assert second.status is PrepareStatus.READY
    assert worker.count == 1
    assert worker.cancel_seen is False
    assert runtime.stats_snapshot()["cache_entry_count"] == 1


@pytest.mark.anyio
async def test_sole_waiter_cancel_stops_worker_does_not_cache_and_can_retry():
    worker = ControlledWorker()
    runtime = SourceGraphRuntime(worker)
    request = _request()
    cancel = asyncio.Event()

    task = asyncio.create_task(runtime.prepare(request, cancel_event=cancel))
    await worker.started.wait()
    cancel.set()
    cancelled = await task
    await runtime.wait_idle()

    assert cancelled.status is PrepareStatus.CANCELLED
    assert cancelled.fallback_used is False
    assert cancelled.metrics.cancel_to_exit_ms == pytest.approx(0.1)
    assert worker.cancel_seen is True
    assert runtime.stats_snapshot()["cache_entry_count"] == 0

    worker.started.clear()
    worker.release.set()
    retried = await runtime.prepare(request)
    assert retried.status is PrepareStatus.READY
    assert worker.count == 2
    assert runtime.stats_snapshot()["cache_entry_count"] == 1


@pytest.mark.anyio
async def test_task_cancellation_returns_structured_outcome_and_cleanup_completes():
    worker = ControlledWorker()
    runtime = SourceGraphRuntime(worker)
    task = asyncio.create_task(runtime.prepare(_request()))
    await worker.started.wait()

    task.cancel()
    outcome = await task
    await runtime.wait_idle()

    assert outcome.status is PrepareStatus.CANCELLED
    assert outcome.blocker.code == "request_cancelled"
    assert worker.cancel_seen is True
    assert runtime.stats_snapshot()["cache_entry_count"] == 0


@pytest.mark.anyio
async def test_crash_timeout_and_failure_do_not_cache_and_failure_retries():
    def crash(_request):
        return WorkerBuildResult.failed(
            PrepareStatus.WORKER_CRASH,
            code="worker_exit_failure",
            stage="worker_process",
        )

    def timeout(_request):
        return WorkerBuildResult.failed(
            PrepareStatus.TIMED_OUT,
            code="worker_timeout",
            stage="worker_process",
        )

    def dependency(_request):
        return WorkerBuildResult.failed(
            PrepareStatus.DEPENDENCY_BLOCKED,
            code="frontend_unavailable",
            stage="frontend_import",
        )

    worker = ImmediateWorker(results=[crash, dependency, timeout, _ready_result])
    runtime = SourceGraphRuntime(worker)
    request = _request()

    crashed = await runtime.prepare(request)
    blocked = await runtime.prepare(request)
    timed_out = await runtime.prepare(request)
    ready = await runtime.prepare(request)

    assert crashed.status is PrepareStatus.WORKER_CRASH
    assert blocked.status is PrepareStatus.DEPENDENCY_BLOCKED
    assert timed_out.status is PrepareStatus.TIMED_OUT
    assert ready.status is PrepareStatus.READY
    assert all(
        item.fallback_used is False for item in (crashed, blocked, timed_out, ready)
    )
    assert worker.count == 4
    assert runtime.stats_snapshot()["cache_entry_count"] == 1


@pytest.mark.anyio
async def test_partial_coverage_remains_partial_on_warm_hit():
    ir = build_hand_ir()
    gap = CoverageGap(
        code="runtime_force_not_modeled",
        message="fixture gap",
        impact=CoverageStatus.INCONCLUSIVE,
    )
    partial_ir = replace(
        ir,
        coverage=CoverageReport(
            status=CoverageStatus.INCONCLUSIVE,
            files_total=1,
            files_projected=1,
            gaps=(gap,),
        ),
    )

    def partial_result(request):
        return WorkerBuildResult.ready(
            partial_ir,
            SourceGraphScopeReceipt(
                scope=request.scope,
                coverage_status=CoverageStatus.INCONCLUSIVE,
                gap_codes=("runtime_force_not_modeled",),
            ),
        )

    runtime = SourceGraphRuntime(ImmediateWorker(results=[partial_result]))
    request = _request()
    cold = await runtime.prepare(request)
    warm = await runtime.prepare(request)

    assert cold.coverage_status is CoverageStatus.INCONCLUSIVE
    assert warm.coverage_status is CoverageStatus.INCONCLUSIVE
    assert warm.scope_match.complete_for_request is False
    assert warm.scope_match.reason == "coverage_preserved_inconclusive"


class AdmissionTrackingWorker:
    def __init__(self, tracker):
        self.tracker = tracker

    async def run(self, request, *, timeout_seconds, cancel_event):
        self.tracker["active"] += 1
        self.tracker["max_active"] = max(
            self.tracker["max_active"], self.tracker["active"]
        )
        await asyncio.sleep(0.03)
        self.tracker["active"] -= 1
        return _ready_result(request)


@pytest.mark.anyio
async def test_process_wide_cold_admission_allows_only_one_build_at_a_time():
    tracker = {"active": 0, "max_active": 0}
    first_runtime = SourceGraphRuntime(AdmissionTrackingWorker(tracker))
    second_runtime = SourceGraphRuntime(AdmissionTrackingWorker(tracker))

    first, second = await asyncio.gather(
        first_runtime.prepare(_request(label="one")),
        second_runtime.prepare(_request(label="two")),
    )

    assert first.status is PrepareStatus.READY
    assert second.status is PrepareStatus.READY
    assert tracker["max_active"] == 1
    assert max(first.metrics.admission_wait_ms, second.metrics.admission_wait_ms) > 0


@pytest.mark.anyio
async def test_ir_load_is_offloaded_so_event_loop_light_work_stays_responsive(
    monkeypatch,
):
    runtime = SourceGraphRuntime(ImmediateWorker())
    original = runtime._load_cache_entry
    load_started = threading.Event()
    load_release = threading.Event()

    def slow_load(*args, **kwargs):
        load_started.set()
        load_release.wait(timeout=5)
        return original(*args, **kwargs)

    monkeypatch.setattr(runtime, "_load_cache_entry", slow_load)
    prepare_task = asyncio.create_task(runtime.prepare(_request()))
    while not load_started.is_set():
        await asyncio.sleep(0.001)

    light_completed = False

    async def light_call():
        nonlocal light_completed
        await asyncio.sleep(0)
        light_completed = True

    await asyncio.wait_for(light_call(), timeout=0.2)
    assert light_completed is True
    load_release.set()
    assert (await prepare_task).status is PrepareStatus.READY


@pytest.mark.anyio
async def test_metrics_are_numeric_or_fixed_labels_and_contain_no_private_inputs():
    outcome = await SourceGraphRuntime(ImmediateWorker()).prepare(_request())
    metrics = outcome.metrics.to_dict()
    rendered = json.dumps(metrics, sort_keys=True)

    assert "sg_top" not in rendered
    assert "hand_connectivity" not in rendered
    assert "lane_data" not in rendered
    assert set(metrics) <= {
        "cache_disposition",
        "flight_disposition",
        "cache_tier",
        "disk_validation_outcome",
        "total_wall_ms",
        "admission_wait_ms",
        "build_wall_ms",
        "load_wall_ms",
        "actual_build_count",
        "coalesced_waiter_count",
        "cancel_to_exit_ms",
        "worker_cpu_ms",
        "rss_start_kib",
        "rss_peak_kib",
        "rss_end_kib",
        "ir_bytes",
        "cache_bytes",
        "cache_entry_count",
        "cache_peak_entry_count",
        "cache_peak_bytes",
        "cache_eviction_count",
        "cache_oversize_bypass_count",
        "frontend_launch_count",
        "disk_lookup_wall_ms",
        "disk_read_wall_ms",
        "disk_validate_wall_ms",
        "disk_publish_wall_ms",
        "disk_write_wall_ms",
        "disk_eviction_wall_ms",
        "disk_hit_count",
        "disk_miss_count",
        "disk_corrupt_count",
        "disk_build_skip_count",
        "disk_bytes_read",
        "disk_bytes_written",
        "disk_entry_count",
        "disk_bytes",
        "disk_eviction_count",
    }
    assert all(
        isinstance(value, (int, float))
        or value
        in {
            *(item.value for item in CacheDisposition),
            *(item.value for item in FlightDisposition),
            *(item.value for item in CacheTier),
            "disabled",
            "not_checked",
        }
        for value in metrics.values()
    )


def test_parent_runtime_import_does_not_import_optional_pyslang():
    command = [
        sys.executable,
        "-c",
        (
            "import sys; import src.source_graph_runtime; "
            "print('yes' if 'pyslang' in sys.modules else 'no')"
        ),
    ]
    result = subprocess.run(
        command,
        cwd=Path(__file__).resolve().parents[1],
        check=True,
        capture_output=True,
        text=True,
        timeout=10,
    )

    assert result.stdout.strip() == "no"


@pytest.mark.anyio
async def test_isolated_process_crash_is_structured_and_temp_state_is_cleaned(
    tmp_path,
):
    script = tmp_path / "crash_worker.py"
    script.write_text("import os\nos._exit(7)\n")
    staging = tmp_path / "staging"
    staging.mkdir()
    runner = IsolatedSourceGraphProcessRunner(
        worker_script=script,
        working_directory=tmp_path,
        staging_directory=staging,
    )

    result = await runner.run(
        _request(), timeout_seconds=2.0, cancel_event=asyncio.Event()
    )

    assert result.status is PrepareStatus.WORKER_CRASH
    assert result.blocker.code == "worker_exit_failure"
    assert result.fallback_used is False
    assert list(staging.iterdir()) == []


@pytest.mark.anyio
async def test_real_worker_reports_unsupported_frontend_identity_without_fallback(
    tmp_path,
):
    staging = tmp_path / "staging"
    staging.mkdir()
    runner = IsolatedSourceGraphProcessRunner(staging_directory=staging)

    result = await runner.run(
        _request(), timeout_seconds=2.0, cancel_event=asyncio.Event()
    )

    assert result.status is PrepareStatus.DEPENDENCY_BLOCKED
    assert result.blocker.code == "frontend_identity_unsupported"
    assert result.fallback_used is False
    assert list(staging.iterdir()) == []


@pytest.mark.anyio
async def test_isolated_process_timeout_reaps_worker_and_cleans_temp_state(
    tmp_path,
):
    script = tmp_path / "sleep_worker.py"
    script.write_text("import time\ntime.sleep(10)\n")
    staging = tmp_path / "staging"
    staging.mkdir()
    runner = IsolatedSourceGraphProcessRunner(
        worker_script=script,
        working_directory=tmp_path,
        staging_directory=staging,
    )

    result = await runner.run(
        _request(), timeout_seconds=0.05, cancel_event=asyncio.Event()
    )

    assert result.status is PrepareStatus.TIMED_OUT
    assert result.blocker.code == "worker_timeout"
    assert result.metrics.cancel_to_exit_ms is not None
    assert result.fallback_used is False
    assert list(staging.iterdir()) == []


@pytest.mark.anyio
async def test_atomic_ready_response_terminates_lingering_worker_without_timeout(
    tmp_path,
):
    request = _request()
    ready = _ready_result(request)
    assert ready.ir_json_bytes is not None
    assert ready.ir_fingerprint_sha256 is not None
    response_payload = {
        "protocol_version": SOURCE_GRAPH_WORKER_PROTOCOL_VERSION,
        "status": PrepareStatus.READY.value,
        "ir_json_base64": base64.b64encode(ready.ir_json_bytes).decode("ascii"),
        "ir_fingerprint_sha256": ready.ir_fingerprint_sha256,
        "scope_receipt": SourceGraphArtifactScopeReceipt(
            scope=request.artifact_identity.scope,
            coverage_status=ready.scope_receipt.coverage_status,
        ).to_dict(),
        "metrics": ready.metrics.to_dict(),
        "fallback_used": False,
    }
    ready_path = tmp_path / "ready.json"
    ready_path.write_text(json.dumps(response_payload), encoding="utf-8")
    script = tmp_path / "linger_worker.py"
    script.write_text(
        "\n".join(
            (
                "import argparse",
                "import os",
                "from pathlib import Path",
                "import time",
                "parser = argparse.ArgumentParser()",
                "parser.add_argument('--request')",
                "parser.add_argument('--response', required=True)",
                "args = parser.parse_args()",
                f"source = Path({str(ready_path)!r})",
                "target = Path(args.response)",
                "temporary = target.with_suffix('.complete')",
                "temporary.write_bytes(source.read_bytes())",
                "os.replace(temporary, target)",
                "time.sleep(10)",
            )
        )
        + "\n",
        encoding="utf-8",
    )
    staging = tmp_path / "staging"
    staging.mkdir()
    runner = IsolatedSourceGraphProcessRunner(
        worker_script=script,
        working_directory=tmp_path,
        staging_directory=staging,
    )

    result = await runner.run(
        request, timeout_seconds=3.0, cancel_event=asyncio.Event()
    )

    assert result.status is PrepareStatus.READY
    assert result.ir_json_bytes == ready.ir_json_bytes
    assert result.metrics.wall_time_ms < 3000
    assert list(staging.iterdir()) == []


@pytest.mark.anyio
async def test_isolated_process_cancellation_reaps_worker_and_cleans_temp_state(
    tmp_path,
):
    script = tmp_path / "sleep_worker.py"
    script.write_text("import time\ntime.sleep(10)\n")
    staging = tmp_path / "staging"
    staging.mkdir()
    runner = IsolatedSourceGraphProcessRunner(
        worker_script=script,
        working_directory=tmp_path,
        staging_directory=staging,
    )
    cancel = asyncio.Event()

    task = asyncio.create_task(
        runner.run(_request(), timeout_seconds=2.0, cancel_event=cancel)
    )
    await asyncio.sleep(0.03)
    cancel.set()
    result = await task

    assert result.status is PrepareStatus.CANCELLED
    assert result.blocker.code == "request_cancelled"
    assert result.metrics.cancel_to_exit_ms is not None
    assert result.fallback_used is False
    assert list(staging.iterdir()) == []
