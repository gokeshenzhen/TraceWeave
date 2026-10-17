"""Independent source transport controls; no scheduler or native EDA runs.

The actual context codec, request/response codecs, source worker, query engines
and LSF parent route are exercised. Handwritten scope/class/file facts describe
only this fixture. Packed bits are a transport oracle, not a Typespec oracle.
These tests cannot establish real LSF placement, native cleanup or licensing.
"""
from __future__ import annotations

import builtins
from contextlib import contextmanager
from copy import deepcopy
from dataclasses import replace
import hashlib
import json
import os
from pathlib import Path
import subprocess
import threading
from types import SimpleNamespace

import pytest

from config import NpiExecutionConfig
from src import design_context_codec as codec
from src import npi_lsf, npi_source_lsf as parent, npi_source_worker as worker
from src.cancellation import OperationCancelled, pop_cancel_event, push_cancel_event
from src.compile_input_capture import CaptureAborted
from src.design_context import DesignContext
from src.hierarchy_source_ir import HierarchyQueryBudget
from src.hierarchy_source_request import prepare_source_query
from src.kdb_anchor import KdbAnchor, KdbBindingChanged
from src.npi_hierarchy_source import NpiHierarchyUnavailable
from tests.test_hierarchy_source_export_request import A, B, ALIAS, Provider, context


KDB_ID = "d" * 64
ARTIFACT = hashlib.sha256((KDB_ID + "\0top").encode()).hexdigest()
SECTIONS = ("compile_result", "include_tree", "filelist_tree", "interfaces",
            "files_full", "component_tree_full", "class_hierarchy_full")
QUERIES = (
    ("get_tb_subtree", {"root": "top", "depth": 3, "max_nodes": 8, "count_mode": "exact"}),
    ("find_tb_instance", {"path": "top.g[0].u"}),
    ("lookup_tb_files", {"has_module": "top"}),
    ("get_tb_file_detail", {"path": A}),
    ("get_tb_class_hierarchy", {}),
    *(("dump_tb_section", {"section": section}) for section in SECTIONS),
    ("resolve_packed_fields", {"source_signal": "top.packet", "fields": ["tag", "payload"]}),
)
COUNTERS = (
    ("scope_count", "scope_count", "max_scopes"),
    ("native_call_count", "call_count", "max_calls"),
    ("symbol_count", "symbol_count", "max_symbols"),
    ("file_count", "file_count", "max_files"),
    ("estimated_fact_bytes", "fact_bytes", "max_bytes"),
)


def forbidden(*args, **kwargs):
    raise AssertionError("source transport control reached forbidden I/O, EDA or fallback")


@pytest.fixture(autouse=True)
def no_native_or_scheduler(monkeypatch):
    original = builtins.__import__

    def importing(name, globals=None, locals=None, fromlist=(), level=0):
        if any(part in {"pynpi", "pyslang", "tb_hierarchy_builder", "structural_scanner"}
               for item in (name, *(str(x) for x in fromlist or ())) for part in item.split(".")):
            forbidden()
        return original(name, globals, locals, fromlist, level)

    monkeypatch.setattr(builtins, "__import__", importing)
    monkeypatch.setattr(subprocess, "Popen", forbidden)
    monkeypatch.setattr(subprocess, "run", forbidden)


@contextmanager
def no_filesystem(monkeypatch):
    with monkeypatch.context() as patch:
        patch.setattr(builtins, "open", forbidden)
        for name in ("open", "stat", "lstat", "listdir", "scandir", "readlink"):
            patch.setattr(os, name, forbidden)
        patch.setattr(os.path, "realpath", forbidden)
        for name in ("open", "read_text", "read_bytes", "stat", "lstat", "resolve"):
            patch.setattr(Path, name, forbidden)
        yield


def cancelled():
    return OperationCancelled()


class RemoteBackend:
    """One copied-fact owner per request, never the real NPI adapter."""

    def __init__(self, world):
        self.world = world
        self.compile_result = self.anchor = None

    def bind_compile_context(self, value):
        self.compile_result = deepcopy(value)

    def bind_kdb_anchor(self, value):
        self.anchor = value

    def query_source(self, plan):
        world = self.world
        assert self.compile_result == plan.context.compile_result()
        assert self.anchor.identity_sha256 == KDB_ID
        world.owner_events.append("enter")
        provider = Provider(plan.budget)
        provider.origin, provider.artifact_identity = "verdi_npi", ARTIFACT
        provider.hook = world.stream_hook
        world.providers.append(provider)
        try:
            if world.query_hook:
                world.query_hook(plan)
            if plan.operation == "resolve_packed_fields":
                # Independent wire facts: the deliberately nonidentity mapping
                # must survive the transport without being sorted or widened.
                plan.budget.call()
                plan.budget.reserve_bytes(256)
                result = dict(schema_version=2, facts_origin="verdi_npi", artifact_identity=ARTIFACT,
                    coverage_status="complete", coverage_gaps=[], analysis_limits=[],
                    semantic_status="complete", coverage_domain="requested_packed_fields",
                    query_metrics=plan.budget.metrics(), source_signal="top.packet", status="resolved",
                    field_bits={"tag": [7, 5], "payload": [4, 3, 1, 0]},
                    declared_range={"left": 7, "right": 0})
            else:
                result = plan.execute(provider)
            result["kdb_binding"] = {**self.anchor.receipt(), "load_status": "loaded", "load_quality": "clean"}
            if world.result_hook:
                world.result_hook(plan, result)
            return result
        finally:
            assert provider.active == 0
            world.owner_events.append("cleanup")


class PoisonFallback:
    def bind_compile_context(self, value):
        pass

    find_driver = find_loads = find_path = get_dynamic_step = forbidden


class RoundTrip:
    """JSON transport double; executes the actual worker in this process."""

    def __init__(self, world):
        self.world, self.requests, self.responses, self.deadlines = world, [], [], []
        self.outcome_hook = None
        self.before_worker = None
        self.after_worker = None

    def execute(self, request, *, deadline=None):
        self.deadlines.append(deadline)
        decoded = npi_lsf.parse_worker_request_bytes(request.model_dump_json().encode("utf-8"))
        self.requests.append(decoded)
        assert decoded is not request and decoded.context_payload is not request.context_payload
        if self.before_worker:
            self.before_worker(decoded)
        response = npi_lsf.execute_worker_request(decoded, backend=self.world.remote)
        response = npi_lsf._RESPONSE_ADAPTER.validate_json(response.model_dump_json())
        self.responses.append(response)
        if isinstance(response, npi_lsf.WorkerSuccess):
            outcome = npi_lsf.LsfExecutionResult(response.result, "completed", "completed",
                                                kdb_load_quality=response.kdb_load_quality)
        elif isinstance(response, npi_lsf.WorkerUnavailable):
            outcome = npi_lsf.LsfExecutionResult(None, "completed", "npi_unavailable", "npi_lsf_npi_unavailable")
        else:
            outcome = npi_lsf.LsfExecutionResult(None, "completed", "failed", response.error_code)
        if self.after_worker:
            self.after_worker()
        return self.outcome_hook(outcome) if self.outcome_hook else outcome


class World:
    def __init__(self):
        self.context = context()
        self.anchor = KdbAnchor("/captured/kdb.elab++", "/captured/kdb.elab++", "elaborated_candidate",
                                KDB_ID, True, ("kdb_source_association_unverified",), (), None, 4096)
        self.source_current = True
        self.source_hook = self.anchor_hook = self.query_hook = self.stream_hook = self.result_hook = None
        self.source_checks, self.anchor_checks = [], []
        self.owner_events, self.providers, self.captures = [], [], []
        self.remote = RemoteBackend(self)
        self.transport = RoundTrip(self)

    def plan(self, operation="get_tb_subtree", arguments=None, *, budget=None):
        return prepare_source_query(self.context, operation, arguments if arguments is not None else QUERIES[0][1],
                                    budget=budget)

    def request(self, operation="get_tb_subtree", arguments=None, *, limits=None):
        return npi_lsf.HierarchySourceWorkerRequest(
            kdb_path=self.anchor.path, top="top", kdb_identity_sha256=KDB_ID,
            kdb_identity_max_bytes=4096, tool=operation,
            arguments=arguments if arguments is not None else QUERIES[0][1],
            context_payload=codec.encode_design_context(self.context),
            limits=npi_lsf.SourceWorkerLimits(**(limits or dict(timeout_sec=30.0, max_scopes=100,
                max_calls=1000, max_bytes=1024 * 1024, max_depth=32, max_symbols=100, max_files=100))))

    def backend(self, *, valid=True):
        config = NpiExecutionConfig(mode="lsf", queue="fixture_queue", bsub_bin="never-bsub",
            bkill_bin="never-bkill", python_bin="never-python", timeout_sec=120,
            staging_dir=Path("/unsubmitted/staging"), extra_args=(),
            error_code=None if valid else "queue_required")
        backend = npi_lsf.LsfConnectivityBackend(config, fallback=PoisonFallback(), transport=self.transport)
        backend.bind_compile_context(self.context.compile_result())
        backend.bind_kdb_anchor(self.anchor)
        return backend


@pytest.fixture
def world(monkeypatch):
    value = World()

    def current(ctx, **kwargs):
        value.source_checks.append((ctx.identity_sha256, kwargs))
        if value.source_hook:
            value.source_hook(ctx)
        return value.source_current

    def capture(path, **kwargs):
        value.captures.append((path, kwargs))
        return value.anchor

    def anchor_current(anchor):
        value.anchor_checks.append(anchor.identity_sha256)
        if value.anchor_hook:
            value.anchor_hook(anchor)

    monkeypatch.setattr(DesignContext, "current", current)
    monkeypatch.setattr(worker, "capture_kdb_anchor", capture)
    monkeypatch.setattr(KdbAnchor, "require_current", anchor_current)
    yield value
    assert value.owner_events.count("enter") == value.owner_events.count("cleanup")
    for provider in value.providers:
        assert provider.active == 0
        assert sum(e[0] == "open" for e in provider.events) == sum(e[0] == "close" for e in provider.events)


def test_context_codec_roundtrip_is_json_only_detached_and_has_no_filesystem(monkeypatch):
    original = context()
    with no_filesystem(monkeypatch):
        wire = codec.encode_design_context(original)
        decoded = codec.decode_design_context(json.loads(json.dumps(wire)))
    assert decoded == original and decoded is not original
    assert decoded.snapshot is not original.snapshot
    assert decoded.aliases == ((ALIAS, A),)
    assert {r.path for r in decoded.snapshot.records} == {A, B}
    assert "body" not in wire["context"] and "handle" not in wire["context"]
    wire["context"]["aliases"][0][0] = "/caller/change"
    assert decoded.aliases == original.aliases


@pytest.mark.parametrize("mutation", ["version", "digest", "tamper", "extra_envelope", "extra_context",
    "extra_snapshot", "extra_record", "duplicate_record", "bad_identity", "bad_simulator", "bad_policy"])
def test_context_codec_rejects_corruption_and_unknown_fields(monkeypatch, mutation):
    wire = codec.encode_design_context(context())
    if mutation == "version": wire["version"] = True
    elif mutation == "digest": wire["payload_sha256"] = "0" * 64
    elif mutation == "tamper": wire["context"]["selected_top"] = "different"
    elif mutation == "extra_envelope": wire["body"] = "forbidden"
    else:
        raw = wire["context"]
        if mutation == "extra_context": raw["source_body"] = "forbidden"
        elif mutation == "extra_snapshot": raw["snapshot"]["body"] = "forbidden"
        elif mutation == "extra_record": raw["snapshot"]["records"][0]["body"] = "forbidden"
        elif mutation == "duplicate_record": raw["snapshot"]["records"].append(deepcopy(raw["snapshot"]["records"][0]))
        elif mutation == "bad_identity": raw["identity_sha256"] = "short"
        elif mutation == "bad_simulator": raw["simulator"] = "unsupported"
        elif mutation == "bad_policy": raw["backend_preference"] = "licensed_only"
        encoded = json.dumps(raw, sort_keys=True, separators=(",", ":"), ensure_ascii=True, allow_nan=False).encode("ascii")
        wire["payload_sha256"] = hashlib.sha256(encoded).hexdigest()
    with no_filesystem(monkeypatch), pytest.raises((ValueError, TypeError)):
        codec.decode_design_context(wire)


def test_context_codec_byte_cap_and_pre_cancel_are_observable(monkeypatch):
    monkeypatch.setattr(codec, "MAX_CONTEXT_WIRE_BYTES", 64)
    with pytest.raises(codec.ContextTransportLimit):
        codec.encode_design_context(context())
    event = threading.Event()
    event.set()
    token = push_cancel_event(event)
    try:
        with pytest.raises(OperationCancelled):
            codec.encode_design_context(context())
    finally:
        pop_cancel_event(token)


def assert_fixture_result(operation, arguments, result):
    if operation == "get_tb_subtree":
        root = result["node"]
        assert root["path"] == "top" and root["children"][0]["path"] == "top.g[0]"
        assert root["children"][0]["children"][0]["path"] == "top.g[0].u"
        assert result["total_descendants"] == 2
    elif operation == "find_tb_instance":
        assert [r["path"] for r in result["hits"]] == ["top.g[0].u"]
    elif operation == "lookup_tb_files":
        assert [r["path"] for r in result["matches"]] == [A]
    elif operation == "get_tb_file_detail":
        assert result["path"] == A
        assert {r["name"] for r in result["symbols"]} == {"top", "bus_if", "pkg::Base"}
    elif operation == "get_tb_class_hierarchy":
        assert result["roots"][0]["qualified_name"] == "pkg::Base"
        assert result["roots"][0]["children"][0]["qualified_name"] == "pkg::Child"
    elif operation == "resolve_packed_fields":
        assert result["field_bits"] == {"tag": [7, 5], "payload": [4, 3, 1, 0]}
        assert result["declared_range"] == {"left": 7, "right": 0}
    else:
        section = arguments["section"]
        assert result["section"] == section and result["export_complete"]
        if section == "compile_result": assert result["data"] == context().compile_result()
        elif section == "include_tree": assert result["data"] == {}
        elif section == "filelist_tree": assert result["data"] == context().compile_result()["filelist_tree"]
        elif section == "component_tree_full":
            assert result["data"]["children"][0]["children"][0]["path"] == "top.g[0].u"
        elif section == "class_hierarchy_full":
            assert result["data"][0]["children"][0]["qualified_name"] == "pkg::Child"
        elif section == "files_full": assert {r["path"] for r in result["data"]} == {A, B}
        elif section == "interfaces": assert {r["name"] for r in result["data"]} == {"bus_if", "aux_if"}


@pytest.mark.parametrize("operation,arguments", QUERIES)
def test_source_worker_seven_operations_and_all_sections_with_independent_facts(world, operation, arguments):
    request = world.request(operation, arguments)
    response = npi_lsf.execute_worker_request(request, backend=world.remote)
    assert isinstance(response, npi_lsf.WorkerSuccess), response
    result = response.result
    assert result["context_identity_sha256"] == world.context.identity_sha256
    assert not ({"handle", "design_handle", "design_context", "backend_status"} & result.keys())
    assert_fixture_result(operation, arguments, result)
    metadata = operation == "dump_tb_section" and arguments["section"] in SECTIONS[:3]
    assert result["facts_origin"] == ("compile_metadata" if metadata else "verdi_npi")
    assert bool(world.owner_events) is not metadata
    assert len(world.source_checks) == 2
    if metadata:
        assert not world.captures and response.kdb_load_quality is None
    else:
        assert result["artifact_identity"] == ARTIFACT and response.kdb_load_quality == "clean"
        assert result["kdb_binding"]["load_status"] == "loaded"
        assert world.owner_events == ["enter", "cleanup"]


@pytest.mark.parametrize("operation,arguments", QUERIES)
def test_lsf_source_parent_actual_json_worker_roundtrip_and_cumulative_budget(world, operation, arguments):
    plan = world.plan(operation, arguments)
    before = plan.budget.metrics()
    backend = world.backend()
    result = backend.query_source(plan)
    assert_fixture_result(operation, arguments, result)
    metadata = operation == "dump_tb_section" and arguments["section"] in SECTIONS[:3]
    if metadata:
        assert world.transport.requests == [] and backend.kdb_status is None
        return
    request, response = world.transport.requests[0], world.transport.responses[0]
    assert world.transport.deadlines == [plan.budget.deadline]
    assert request.top == "top" and request.kdb_identity_sha256 == KDB_ID
    assert codec.decode_design_context(request.context_payload) == world.context
    for metric, counter, limit in COUNTERS:
        assert getattr(request.limits, limit) == getattr(plan.budget, limit) - before[metric]
        assert result["query_metrics"][metric] == before[metric] + response.result["query_metrics"][metric]
    assert result["query_metrics"]["context_transport_bytes"] > 0
    assert result["query_metrics"]["worker_query_ms"] >= 0
    assert result[npi_lsf.NPI_EXECUTION_STATUS_KEY] == dict(
        execution_mode="lsf", scheduler_status="completed", worker_status="completed")
    assert backend.kdb_status["load_quality"] == "clean"


@pytest.mark.parametrize("operation,arguments", [
    *(("dump_tb_section", {"section": section}) for section in SECTIONS[:3]),
    ("lookup_tb_files", {"basename": "a.sv"}),
    ("lookup_tb_files", {"basename": "missing.sv", "has_module": "top"}),
    ("get_tb_file_detail", {"path": "/not/compiled/a.sv"}),
])
def test_metadata_and_nonmember_queries_never_touch_lsf_config_binding_or_encoding(world, monkeypatch, operation, arguments):
    plan = world.plan(operation, arguments)
    assert not plan.needs_provider
    backend = SimpleNamespace(_last_kdb_status={"load_quality": "clean"})
    monkeypatch.setattr(parent, "encode_design_context", forbidden)
    result = parent.query_source_over_lsf(backend, plan)
    assert result["facts_origin"] == "compile_metadata"
    assert backend._last_kdb_status is None and not world.owner_events
    assert not world.source_checks and not world.transport.requests


@pytest.mark.parametrize("where", ["before", "after", "ordinary_error"])
def test_worker_source_identity_change_wins_over_facts_or_ordinary_failure(world, where):
    if where == "before": world.source_current = False
    else:
        def change(plan):
            world.source_current = False
            if where == "ordinary_error": raise RuntimeError("private backend diagnostic")
        world.query_hook = change
    response = worker.execute_source_worker(world.request(), backend=world.remote)
    assert isinstance(response, npi_lsf.WorkerError)
    assert response.error_code == "context_identity_changed"
    assert "private backend diagnostic" not in response.model_dump_json()
    assert bool(world.owner_events) is (where != "before")


@pytest.mark.parametrize("failure,code", [
    (lambda: TimeoutError("private"), "query_timeout"),
    (lambda: CaptureAborted("timeout"), "kdb_identity_timeout"),
    (lambda: KdbBindingChanged(), "kdb_identity_changed"),
    (lambda: NpiHierarchyUnavailable("unsupported_language"), "hierarchy_source_unavailable"),
    (lambda: RuntimeError("private"), "worker_internal_error"),
])
def test_worker_typed_failure_is_fixed_and_never_runs_fallback(world, failure, code):
    def fail(plan): raise failure()
    world.query_hook = fail
    response = worker.execute_source_worker(world.request(), backend=world.remote)
    assert isinstance(response, npi_lsf.WorkerError) and response.error_code == code
    assert response.stage == "query" and world.owner_events == ["enter", "cleanup"]
    assert "private" not in response.model_dump_json()


def test_worker_load_failure_retains_unavailable_kind(world):
    def fail(plan): raise NpiHierarchyUnavailable("npi_load_failed")
    world.query_hook = fail
    assert isinstance(worker.execute_source_worker(world.request(), backend=world.remote), npi_lsf.WorkerUnavailable)


@pytest.mark.parametrize("phase", ["query", "next", "close"])
def test_worker_cancellation_propagates_and_closes_all_handwritten_owners(world, phase):
    def query(plan):
        if phase == "query": raise cancelled()
    def stream(event, label, index):
        if event == phase: raise cancelled()
    world.query_hook, world.stream_hook = query, stream
    with pytest.raises(OperationCancelled):
        worker.execute_source_worker(world.request(), backend=world.remote)
    assert world.owner_events == ["enter", "cleanup"]


@pytest.mark.parametrize("kind", ["wrong_top", "source_policy", "bad_context_digest", "changed_anchor"])
def test_worker_rejects_invalid_context_or_anchor_before_query(world, kind):
    request = world.request()
    if kind == "wrong_top": request = request.model_copy(update={"top": "other"})
    elif kind == "source_policy":
        request = request.model_copy(update={"context_payload": codec.encode_design_context(replace(world.context, backend_preference="source"))})
    elif kind == "bad_context_digest": request.context_payload["payload_sha256"] = "0" * 64
    else: world.anchor = replace(world.anchor, identity_sha256="e" * 64)
    response = worker.execute_source_worker(request, backend=world.remote)
    assert isinstance(response, npi_lsf.WorkerError)
    assert response.error_code == ("kdb_identity_changed" if kind == "changed_anchor" else "request_invalid")
    assert not world.owner_events


@pytest.mark.parametrize("proof", ["positive", "negative", "export", "packed"])
def test_late_worker_final_validation_cannot_publish_complete_proof(world, proof):
    operation, arguments = {
        "positive": ("find_tb_instance", {"path": "top.g[0].u"}),
        "negative": ("find_tb_instance", {"path": "top.missing"}),
        "export": ("dump_tb_section", {"section": "component_tree_full"}),
        "packed": QUERIES[-1],
    }[proof]
    def expire(plan, result): plan.budget.deadline = -1
    world.result_hook = expire
    response = worker.execute_source_worker(world.request(operation, arguments), backend=world.remote)
    assert isinstance(response, npi_lsf.WorkerSuccess), response
    result = response.result
    assert result["coverage_status"] == "partial" and "timeout_sec" in result["analysis_limits"]
    if proof == "positive": assert result["hits"][0]["path"] == "top.g[0].u" and result["total"] is None
    elif proof == "negative":
        # find returns a hit list; unlike subtree it has no not-found error.
        # Late validation must remove the exact-zero proof, keeping only an
        # empty observed prefix and its honest lower bound.
        assert result["hits"] == [] and result["total"] is None
        assert result["count_status"] == "lower_bound" and result["count_lower_bound"] == 0
    elif proof == "export": assert result["export_complete"] is False and result["total_descendants"] is None
    else: assert result["status"] == "mapping_required" and result["field_bits"] == {} and result["declared_range"] is None


@pytest.mark.parametrize("mutation", ["context", "ownership", "artifact", "kdb", "load", "origin",
    "handle", "design_handle", "design_context", "backend_status", "work", "scheduler", "worker", "quality"])
def test_parent_discards_foreign_or_impossible_worker_facts(world, mutation):
    def corrupt(outcome):
        result = deepcopy(outcome.result)
        if mutation == "context": result["context_identity_sha256"] = "f" * 64
        elif mutation == "ownership": result["ownership_identity"] = "f" * 64
        elif mutation == "artifact": result["artifact_identity"] = "foreign-artifact"
        elif mutation == "kdb": result["kdb_binding"]["identity_sha256"] = "f" * 64
        elif mutation == "load": result["kdb_binding"]["load_status"] = "not_checked"
        elif mutation == "origin": result["facts_origin"] = "slang"
        elif mutation in {"handle", "design_handle"}: result[mutation] = "foreign_handle"
        elif mutation == "design_context": result[mutation] = None
        elif mutation == "backend_status": result[mutation] = None
        elif mutation == "work": result["query_metrics"]["scope_count"] = 100001
        elif mutation == "scheduler": return replace(outcome, scheduler_status="failed")
        elif mutation == "worker": return replace(outcome, worker_status="failed")
        elif mutation == "quality": return replace(outcome, kdb_load_quality="degraded")
        return replace(outcome, result=result)
    world.transport.outcome_hook = corrupt
    backend = world.backend()
    with pytest.raises(parent.LsfSourceUnavailable) as failure:
        backend.query_source(world.plan())
    assert failure.value.reason == "npi_lsf_result_invalid"
    assert backend.kdb_status is None and len(world.transport.requests) == 1


@pytest.mark.parametrize("reason,exception", [
    ("kdb_identity_changed", KdbBindingChanged), ("kdb_identity_timeout", CaptureAborted),
    ("context_identity_changed", CaptureAborted), ("context_identity_timeout", CaptureAborted),
    ("query_timeout", TimeoutError), ("npi_lsf_timeout", TimeoutError),
])
def test_parent_remote_typed_failure_cannot_fall_back_locally(world, reason, exception):
    world.transport.outcome_hook = lambda outcome: npi_lsf.LsfExecutionResult(None, "completed", "failed", reason)
    with pytest.raises(exception):
        world.backend().query_source(world.plan())
    assert len(world.transport.requests) == 1


def test_parent_ordinary_failure_has_fixed_execution_receipt_and_no_local_retry(world):
    world.transport.outcome_hook = lambda outcome: npi_lsf.LsfExecutionResult(
        None, "completed", "npi_unavailable", "npi_lsf_npi_unavailable")
    with pytest.raises(parent.LsfSourceUnavailable) as failure:
        world.backend().query_source(world.plan())
    assert failure.value.execution_status == dict(execution_mode="lsf", scheduler_status="completed",
        worker_status="npi_unavailable", fallback_reason="npi_lsf_npi_unavailable")
    assert len(world.transport.requests) == 1


@pytest.mark.parametrize("counter,limit,reason", [
    ("scope_count", "max_scopes", "scope_limit"), ("call_count", "max_calls", "native_call_limit"),
    ("symbol_count", "max_symbols", "symbol_limit"), ("file_count", "max_files", "file_limit"),
    ("fact_bytes", "max_bytes", "fact_byte_limit"),
])
def test_exhausted_parent_work_budget_returns_partial_without_submission(world, counter, limit, reason):
    plan = world.plan()
    setattr(plan.budget, counter, getattr(plan.budget, limit))
    backend = world.backend()
    result = backend.query_source(plan)
    assert result["analysis_limits"] == [reason] and result["coverage_status"] == "partial"
    assert result["facts_origin"] == "compile_metadata" and not world.transport.requests
    assert backend.kdb_status is None


def test_worker_unstarted_partial_does_not_infer_loaded_from_kdb_filesystem(world, monkeypatch):
    original = worker.prepare_source_query
    def exhausted(context, tool, arguments, *, budget):
        budget.deadline = -1
        return original(context, tool, arguments, budget=budget)
    monkeypatch.setattr(worker, "prepare_source_query", exhausted)
    monkeypatch.setattr(npi_lsf, "kdb_has_elaboration_errors", forbidden)
    backend = world.backend()
    result = backend.query_source(world.plan())
    assert result["facts_origin"] == "compile_metadata" and result["semantic_status"] == "not_run"
    assert result["coverage_status"] == "partial" and result["analysis_limits"] == ["timeout_sec"]
    assert "kdb_binding" not in result and backend.kdb_status is None
    assert not world.owner_events and not world.captures
    assert result[npi_lsf.NPI_EXECUTION_STATUS_KEY]["worker_status"] == "completed"


def test_parent_deadline_is_original_and_late_roundtrip_drops_exact_counts(world):
    plan = world.plan()
    deadline = plan.budget.deadline
    world.transport.after_worker = lambda: setattr(plan.budget, "deadline", -1)
    result = world.backend().query_source(plan)
    assert world.transport.deadlines == [deadline]
    assert result["coverage_status"] == "partial" and result["total_descendants"] is None
    assert result["count_status"] == "lower_bound" and "timeout_sec" in result["analysis_limits"]


def test_parent_pre_cancel_and_mid_transport_cancel_never_retry(world):
    backend, plan = world.backend(), world.plan()
    event = threading.Event()
    event.set()
    token = push_cancel_event(event)
    try:
        with pytest.raises(OperationCancelled): backend.query_source(plan)
    finally:
        pop_cancel_event(token)
    assert not world.transport.requests
    world.transport.before_worker = lambda request: (_ for _ in ()).throw(OperationCancelled())
    with pytest.raises(OperationCancelled): backend.query_source(plan)
    assert len(world.transport.requests) == 1 and not world.owner_events


def test_parent_config_context_and_transport_capacity_fail_before_submission(world, monkeypatch):
    with pytest.raises(parent.LsfSourceUnavailable) as failure:
        world.backend(valid=False).query_source(world.plan())
    assert failure.value.reason == "npi_lsf_config_invalid"
    backend = world.backend()
    backend.bind_compile_context({"foreign": "compile context"})
    with pytest.raises(NpiHierarchyUnavailable) as failure:
        backend.query_source(world.plan())
    assert failure.value.reason == "npi_compile_context_mismatch"
    monkeypatch.setattr(codec, "MAX_CONTEXT_WIRE_BYTES", 64)
    with pytest.raises(parent.LsfSourceUnavailable) as failure:
        world.backend().query_source(world.plan())
    assert failure.value.reason == "design_context_transport_byte_limit"
    assert not world.transport.requests and not world.owner_events


@pytest.mark.parametrize("failure,code", [
    (lambda: TimeoutError("private cleanup failure"), "query_timeout"),
    (lambda: RuntimeError("private cleanup failure"), "worker_internal_error"),
])
def test_worker_cleanup_failure_cannot_publish_a_complete_result(world, failure, code):
    def fail_close(event, label, index):
        if event == "close": raise failure()
    world.stream_hook = fail_close
    response = worker.execute_source_worker(world.request(), backend=world.remote)
    assert isinstance(response, npi_lsf.WorkerError) and response.error_code == code
    assert world.owner_events == ["enter", "cleanup"]
    assert "private cleanup failure" not in response.model_dump_json()


def test_worker_final_anchor_change_discards_success_and_typed_query_failure_stays_primary(world):
    def change(plan, result):
        world.anchor_hook = lambda anchor: (_ for _ in ()).throw(KdbBindingChanged())
    world.result_hook = change
    response = worker.execute_source_worker(world.request(), backend=world.remote)
    assert isinstance(response, npi_lsf.WorkerError) and response.error_code == "kdb_identity_changed"
    world.result_hook = None
    world.anchor_hook = None
    def fail(plan):
        world.source_current = False
        raise TimeoutError("original query timeout")
    world.query_hook = fail
    response = worker.execute_source_worker(world.request(), backend=world.remote)
    assert isinstance(response, npi_lsf.WorkerError) and response.error_code == "query_timeout"


def test_worker_scope_budget_retains_only_bounded_positive_facts(world):
    request = world.request()
    request = request.model_copy(update={"limits": request.limits.model_copy(update={"max_scopes": 1})})
    response = worker.execute_source_worker(request, backend=world.remote)
    assert isinstance(response, npi_lsf.WorkerSuccess), response
    result = response.result
    assert result["node"]["path"] == "top" and result["total_descendants"] is None
    assert result["coverage_status"] == "partial" and "scope_limit" in result["analysis_limits"]
    assert result["query_metrics"]["scope_count"] == 1
    assert world.owner_events == ["enter", "cleanup"]


def test_source_policy_never_submits_licensed_source_work(world):
    world.context = replace(world.context, backend_preference="source")
    with pytest.raises(NpiHierarchyUnavailable) as failure:
        world.backend().query_source(world.plan())
    assert failure.value.reason == "npi_source_only_context"
    assert not world.transport.requests and not world.owner_events


def test_encoding_and_queue_time_do_not_reset_original_parent_deadline(world, monkeypatch):
    # Virtual time only: no sleep or scheduler. Encoding consumes three seconds
    # and queue wait ten; a completed worker arriving after the original bound
    # cannot certify an exact count even though its own clock was still within
    # the duration transferred before queueing.
    clock = [100.0]
    monkeypatch.setattr(parent.time, "monotonic", lambda: clock[0])
    budget = HierarchyQueryBudget(timeout_sec=30)
    plan = world.plan(budget=budget)
    clock[0] = 105.0
    original = parent.encode_design_context
    def encode(ctx):
        clock[0] += 3.0
        return original(ctx)
    monkeypatch.setattr(parent, "encode_design_context", encode)
    world.transport.before_worker = lambda request: clock.__setitem__(0, clock[0] + 10.0)
    world.transport.after_worker = lambda: clock.__setitem__(0, 135.0)
    result = world.backend().query_source(plan)
    assert world.transport.deadlines == [130.0]
    assert world.transport.requests[0].limits.timeout_sec == 22.0
    assert result["query_metrics"]["context_transport_encode_ms"] == 3000.0
    assert result["query_metrics"]["lsf_round_trip_ms"] == 27000.0
    assert result["coverage_status"] == "partial" and result["total_descendants"] is None


@pytest.mark.parametrize("field,value", [("timeout_sec", True), ("timeout_sec", float("nan")),
    ("max_calls", True), ("max_depth", 0), ("max_bytes", 64 * 1024 * 1024 + 1)])
def test_source_worker_protocol_rejects_unbounded_or_coerced_limits(world, field, value):
    raw = world.request().model_dump(mode="json")
    raw["limits"][field] = value
    with pytest.raises(ValueError):
        npi_lsf.parse_worker_request_bytes(json.dumps(raw).encode("utf-8"))
