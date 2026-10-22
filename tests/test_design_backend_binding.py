"""First-query selection and metadata ownership, without native execution.

Real text/map bytes establish identity and resource observations. Synthetic KDB
directories are candidates only; no test establishes loadability, licenses,
source association, public routing or HDL semantic coverage.
"""
from __future__ import annotations

import builtins
from dataclasses import FrozenInstanceError, replace
import os
import subprocess

import pytest

import config
from src import compile_input_capture, design_backend_binding as binding_module
from src import kdb_builder, structural_scanner, tb_hierarchy_builder, verdi_npi_backend
from src.cancellation import OperationCancelled
from src.compile_input_capture import CaptureAborted, CompileInputCapture
from src.design_backend_binding import ExecutionBindingChanged, prepare_backend_binding
from src.design_context_runtime import ContextUnavailable, DesignContextStore, prepare_design_context
from src.kdb_anchor import KdbBindingChanged


def _forbidden(*args, **kwargs):
    pytest.fail("metadata-only fixture attempted forbidden discovery, I/O or execution")


@pytest.fixture(autouse=True)
def local_metadata_only(monkeypatch, tmp_path):
    monkeypatch.setenv("TRACEWEAVE_NPI_EXECUTION", "local")
    monkeypatch.setenv("TRACEWEAVE_CONNECTIVITY_ROUTE", "auto")
    monkeypatch.delenv("TRACEWEAVE_NPI_WORKER", raising=False)
    monkeypatch.setattr(config, "TRACEWEAVE_CACHE_ROOT", tmp_path / "private_cache")
    monkeypatch.setattr(config, "NPI_ALLOW_DEGRADED_KDB", True)
    monkeypatch.setattr(verdi_npi_backend, "_import_pynpi", _forbidden)
    monkeypatch.setattr(kdb_builder, "build_kdb", _forbidden)
    monkeypatch.setattr(kdb_builder, "_run_phase", _forbidden)
    monkeypatch.setattr(tb_hierarchy_builder, "build_hierarchy", _forbidden)
    monkeypatch.setattr(structural_scanner, "scan_structural_risks", _forbidden)
    monkeypatch.setattr(subprocess, "Popen", _forbidden)
    monkeypatch.setattr(subprocess, "run", _forbidden)


def _design(root, *, simulator="vcs", preference="auto", kdb=None, filelist=False):
    root.mkdir(parents=True, exist_ok=True)
    source = root / "top.sv"
    source.write_text("module top; wire q; endmodule\n")
    log = root / "compile.log"
    options = str(source)
    if filelist:
        (root / "design.f").write_text(f"+define+MODE=1\n{source}\n")
        options = f"-f {root / 'design.f'}"
    if simulator == "vcs":
        text = (f"Chronologic VCS simulator\nCommand: vcs -sverilog {options} -top top\n"
                f"Parsing design file '{source}'\nTop Level Modules:\n       top\n")
    elif simulator == "xcelium":
        text = f"cd {root} && xrun -sv {options} -top top\nfile: {source}\n  module worklib.top:v\n"
    else:
        text = f"make: Entering directory '{root}'\nverilator --cc {options} --top-module top\n"
    log.write_text(text)
    context = prepare_design_context(str(log), simulator=simulator, backend_preference=preference,
                                     kdb_path=None if kdb is None else str(kdb))
    return context, source, log


def _kdb(path, *, marker=False):
    path.mkdir(parents=True)
    (path / "_libFileMap").write_bytes(b"lib_A")
    (path / "_stnElabDB").write_bytes(b"elab_A")
    if marker:
        (path / ".hasElabcomError").write_bytes(b"")
    return path


def _explicit(root, **kwargs):
    candidate = _kdb(root / "chosen_kdb")
    context, source, log = _design(root / "design", kdb=candidate, **kwargs)
    return context, prepare_backend_binding(context), candidate, source, log


def _no_selection_io(monkeypatch):
    monkeypatch.setattr(binding_module, "probe_verdi_backend", _forbidden)
    monkeypatch.setattr(binding_module, "capture_kdb_anchor", _forbidden)


def _expired(store, handle):
    with pytest.raises(ContextUnavailable) as raised:
        with store.pin(handle):
            pytest.fail("expired handle remained available")
    assert raised.value.code == "handle_expired"


def test_first_auto_selection_is_immutable_and_does_not_reprobe_on_validation(tmp_path, monkeypatch):
    candidate = _kdb(tmp_path / "simv.daidir" / "kdb.elab++")
    context, _, _ = _design(tmp_path)
    original = binding_module.probe_verdi_backend
    calls = []
    def observed(*args, **kwargs):
        calls.append(1)
        return original(*args, **kwargs)
    monkeypatch.setattr(binding_module, "probe_verdi_backend", observed)
    binding = prepare_backend_binding(context)
    assert len(calls) == 1 and binding.npi_candidate
    assert binding.anchor.path == str(candidate)
    _no_selection_io(monkeypatch)
    binding.require_current()
    binding.require_current()
    assert len(calls) == 1
    receipt = binding.receipt()
    assert receipt["selection"] == "candidate"
    assert receipt["artifact"]["load_status"] == "not_checked"
    assert receipt["artifact"]["source_association"] == "unverified"
    assert "kdb_identity_bounded" in receipt["gaps"]
    with pytest.raises(FrozenInstanceError):
        binding.disposition = "loaded"
    status = binding.backend_status()
    status["kdb_path"] = "/caller/mutation"
    assert binding.backend_status()["kdb_path"] == str(candidate)


def test_absent_selection_stays_absent_if_candidate_appears_later(tmp_path, monkeypatch):
    context, _, _ = _design(tmp_path)
    binding = prepare_backend_binding(context)
    assert binding.anchor is None and not binding.npi_candidate
    assert binding.disposition == "unavailable"
    _kdb(tmp_path / "simv.daidir" / "kdb.elab++")
    _no_selection_io(monkeypatch)
    binding.require_current()
    assert binding.backend_status()["kdb_path"] is None
    assert binding.receipt()["artifact"] is None
    assert context.current()


@pytest.mark.parametrize("simulator", ["vcs", "xcelium", "verilator"])
def test_explicit_path_is_candidate_without_probe_or_source_association(tmp_path, monkeypatch, simulator):
    candidate = _kdb(tmp_path / "external_kdb")
    context, _, _ = _design(tmp_path / "design", simulator=simulator, kdb=candidate)
    monkeypatch.setattr(binding_module, "probe_verdi_backend", _forbidden)
    binding = prepare_backend_binding(context)
    assert binding.npi_candidate and binding.anchor.path == str(candidate)
    assert binding.backend_status()["kdb_flow"] == "explicit"
    assert binding.receipt()["artifact"]["source_association"] == "unverified"
    assert "kdb_source_association_unverified" in binding.gaps
    assert dict(binding.metrics)["discovery_file_count"] == 0
    assert dict(binding.metrics)["discovery_read_bytes"] == 0
    binding.require_current()


@pytest.mark.parametrize("policy", ["source", "route"])
def test_source_policy_has_zero_probe_anchor_or_native_work(tmp_path, monkeypatch, policy):
    candidate = _kdb(tmp_path / "not_to_be_opened")
    context, _, _ = _design(tmp_path / "design", preference="source" if policy == "source" else "auto", kdb=candidate)
    if policy == "route":
        monkeypatch.setenv("TRACEWEAVE_CONNECTIVITY_ROUTE", "source_graph")
    _no_selection_io(monkeypatch)
    binding = prepare_backend_binding(context)
    assert binding.disposition == "source_policy" and binding.anchor is None
    assert not binding.npi_candidate
    assert binding.backend_status()["_npi_selection_reason"] == "source_backend_requested"
    for metric in ("discovery_ms", "discovery_read_bytes", "discovery_file_count", "discovery_entry_count", "anchor_ms"):
        assert dict(binding.metrics)[metric] == 0
    binding.require_current()


@pytest.mark.parametrize("change", ["raw_bytes", "root_alias"])
def test_real_candidate_generation_or_alias_change_rejects_old_binding(tmp_path, change):
    first, second = _kdb(tmp_path / "first"), _kdb(tmp_path / "second")
    alias = tmp_path / "selected"
    alias.symlink_to(first, target_is_directory=True)
    context, _, _ = _design(tmp_path / "design", kdb=alias)
    binding = prepare_backend_binding(context)
    if change == "root_alias":
        alias.unlink()
        alias.symlink_to(second, target_is_directory=True)
    else:
        path = first / "_libFileMap"
        before = path.stat()
        path.write_bytes(b"lib_B")
        os.utime(path, ns=(before.st_atime_ns, before.st_mtime_ns))
    with pytest.raises(KdbBindingChanged):
        binding.require_current()
    assert context.current()  # Source validity is a different observation.


@pytest.mark.parametrize("change", ["execution", "route", "degraded", "VERDI_HOME", "NOVAS_HOME", "SNPSLMD_LICENSE_FILE", "LM_LICENSE_FILE"])
def test_execution_configuration_change_has_fixed_error(tmp_path, monkeypatch, change):
    _, binding, _, _, _ = _explicit(tmp_path)
    if change == "execution":
        monkeypatch.setenv("TRACEWEAVE_NPI_EXECUTION", "unsupported_policy")
    elif change == "route":
        monkeypatch.setenv("TRACEWEAVE_CONNECTIVITY_ROUTE", "source_graph")
    elif change == "degraded":
        monkeypatch.setattr(config, "NPI_ALLOW_DEGRADED_KDB", False)
    else:
        monkeypatch.setenv(change, "fixture_changed_private_value")
    with pytest.raises(ExecutionBindingChanged) as raised:
        binding.require_current()
    assert str(raised.value) == "design_context_execution_changed"
    assert "fixture_changed_private_value" not in str(raised.value)


def test_source_preference_does_not_depend_on_license_environment(tmp_path, monkeypatch):
    context, _, _ = _design(tmp_path, preference="source")
    _no_selection_io(monkeypatch)
    binding = prepare_backend_binding(context)
    monkeypatch.setenv("VERDI_HOME", "/different/vendor")
    monkeypatch.setenv("LM_LICENSE_FILE", "different_private_license")
    monkeypatch.setenv("TRACEWEAVE_NPI_EXECUTION", "unsupported_policy")
    binding.require_current()


@pytest.mark.parametrize("code", ["byte_limit", "file_limit", "reference_limit", "input_not_regular", "kdb_discovery_entry_limit", "kdb_setup_byte_limit"])
def test_probe_budget_abort_is_explicit_partial_with_released_capture(tmp_path, monkeypatch, code):
    context, source, _ = _design(tmp_path)
    captures = []
    def capture_factory(limits):
        capture = CompileInputCapture(limits)
        captures.append(capture)
        return capture
    def limited_probe(*args, **kwargs):
        # Control injection after one real captured body, not a semantic oracle.
        compile_input_capture.read_compile_text(source)
        raise CaptureAborted(code)
    monkeypatch.setattr(binding_module, "CompileInputCapture", capture_factory)
    monkeypatch.setattr(binding_module, "probe_verdi_backend", limited_probe)
    monkeypatch.setattr(binding_module, "capture_kdb_anchor", _forbidden)
    binding = prepare_backend_binding(context)
    assert binding.disposition == "probe_partial" and not binding.npi_candidate
    assert binding.anchor is None and f"kdb_probe_{code}" in binding.gaps
    assert binding.backend_status()["_npi_selection_reason"] == "npi_kdb_probe_incomplete"
    assert dict(binding.metrics)["discovery_file_count"] == 1
    assert dict(binding.metrics)["discovery_read_bytes"] == source.stat().st_size
    assert len(captures) == 1 and captures[0].body_count == captures[0].resident_bytes == 0


@pytest.mark.parametrize("dimension", ["bytes", "files", "references"])
def test_real_discovery_capture_budget_is_reported_partial(tmp_path, monkeypatch, dimension):
    (config.TRACEWEAVE_CACHE_ROOT / config.KDB_CACHE_SUBDIR).mkdir(parents=True)
    context, _, _ = _design(tmp_path, filelist=True)
    keyword = {"bytes": "max_bytes", "files": "max_files", "references": "max_references"}[dimension]
    code = {"bytes": "byte_limit", "files": "file_limit", "references": "reference_limit"}[dimension]
    captures = []
    def small_capture(limits):
        capture = CompileInputCapture(replace(limits, **{keyword: 1}))
        captures.append(capture)
        return capture
    monkeypatch.setattr(binding_module, "CompileInputCapture", small_capture)
    binding = prepare_backend_binding(context)
    assert binding.disposition == "probe_partial" and f"kdb_probe_{code}" in binding.gaps
    assert captures and all(c.body_count == c.resident_bytes == 0 for c in captures)


@pytest.mark.parametrize("stage", ["probe", "anchor"])
@pytest.mark.parametrize("failure", ["cancel", "timeout", "input_changed"])
def test_abort_propagates_without_publishing_a_binding(tmp_path, monkeypatch, stage, failure):
    context, _, _ = _design(tmp_path, kdb=tmp_path / "explicit" if stage == "anchor" else None)
    error = OperationCancelled() if failure == "cancel" else CaptureAborted(failure)
    def fail(*args, **kwargs):
        raise error
    monkeypatch.setattr(binding_module, "probe_verdi_backend" if stage == "probe" else "capture_kdb_anchor", fail)
    store = DesignContextStore()
    handle = store.register(context)
    with store.pin(handle) as entry:
        with pytest.raises(type(error)) as raised:
            prepare_backend_binding(context)
        assert raised.value is error and entry.binding is None


def test_probe_validation_change_is_fatal_and_total_deadline_covers_probe(tmp_path, monkeypatch):
    context, _, _ = _design(tmp_path)
    with monkeypatch.context() as patch:
        patch.setattr(binding_module.CompileInputCapture, "validate", lambda self: False)
        with pytest.raises(CaptureAborted) as raised:
            prepare_backend_binding(context)
        assert raised.value.code == "input_changed"
    now = [100.0]
    monkeypatch.setattr(binding_module.time, "monotonic", lambda: now[0])
    def slow_probe(*args, **kwargs):
        now[0] = 102.0
        return {"kdb_path": None}
    monkeypatch.setattr(binding_module, "probe_verdi_backend", slow_probe)
    with pytest.raises(CaptureAborted) as raised:
        prepare_backend_binding(context, timeout_sec=1)
    assert raised.value.code == "timeout"


def test_real_discovery_metrics_include_capture_and_validation_without_bodies(tmp_path, monkeypatch):
    (config.TRACEWEAVE_CACHE_ROOT / config.KDB_CACHE_SUBDIR).mkdir(parents=True)
    context, source, _ = _design(tmp_path, filelist=True)
    captures = []
    def capture_factory(limits):
        capture = CompileInputCapture(limits)
        captures.append(capture)
        return capture
    monkeypatch.setattr(binding_module, "CompileInputCapture", capture_factory)
    binding = prepare_backend_binding(context)
    metrics = dict(binding.metrics)
    raw_bytes = source.stat().st_size + (tmp_path / "design.f").stat().st_size
    assert metrics["discovery_file_count"] == 2
    assert metrics["discovery_read_bytes"] == 2 * raw_bytes
    assert metrics["discovery_entry_count"] == len(list(tmp_path.iterdir()))
    assert all(isinstance(value, (int, float)) and value >= 0 for value in metrics.values())
    assert metrics["anchor_ms"] == 0
    assert len(captures) == 1 and captures[0].body_count == captures[0].resident_bytes == 0
    assert not any(hasattr(binding, name) for name in ("source_text", "bodies", "capture"))
    with pytest.raises(FrozenInstanceError):
        binding.metrics = ()


def test_prepare_declared_candidate_changes_identity_without_probe_or_anchor_io(tmp_path, monkeypatch):
    _no_selection_io(monkeypatch)
    first, _, log = _design(tmp_path, kdb=tmp_path / "one")
    second = prepare_design_context(str(log), simulator="vcs", kdb_path=str(tmp_path / "two"))
    unrequested = prepare_design_context(str(log), simulator="vcs")
    assert first.requested_kdb_path == str(tmp_path / "one")
    assert len({first.identity_sha256, second.identity_sha256, unrequested.identity_sha256}) == 3
    assert first.snapshot == second.snapshot == unrequested.snapshot
    for invalid in ("", " ", "bad\x00path", True, 12):
        with pytest.raises(ValueError):
            prepare_design_context(str(log), simulator="vcs", kdb_path=invalid)


def test_pin_and_publish_binding_are_io_free_and_first_publication_is_idempotent(tmp_path, monkeypatch):
    context, binding, _, _, _ = _explicit(tmp_path)
    store = DesignContextStore(max_bytes=context.estimated_bytes + binding.estimated_bytes)
    handle = store.register(context)
    with pytest.raises(ContextUnavailable) as raised:
        store.publish_binding(handle, binding)
    assert raised.value.code == "handle_expired"
    with monkeypatch.context() as patch:
        patch.setattr(type(context), "current", _forbidden)
        patch.setattr(type(binding), "require_current", _forbidden)
        patch.setattr(builtins, "open", _forbidden)
        patch.setattr(os, "open", _forbidden)
        patch.setattr(os, "stat", _forbidden)
        patch.setattr(os, "scandir", _forbidden)
        with store.pin(handle) as entry:
            assert entry.context is context
            assert store.publish_binding(handle, binding) is binding
            assert store.publish_binding(handle, replace(binding)) is binding
            assert entry.binding is binding
    with store.lease(handle) as leased:
        assert leased is context
    assert store.remove(handle)
    # Removal releases binding accounting as well as context accounting.
    handle = store.register(context)
    with store.pin(handle):
        assert store.publish_binding(handle, binding) is binding


def test_binding_capacity_failure_is_atomic_and_pin_prevents_eviction(tmp_path):
    context, binding, _, _, _ = _explicit(tmp_path / "one")
    store = DesignContextStore(max_entries=1, max_bytes=context.estimated_bytes + binding.estimated_bytes - 1)
    handle = store.register(context)
    other, _, _ = _design(tmp_path / "other")
    with store.pin(handle) as entry:
        with pytest.raises(ContextUnavailable) as raised:
            store.publish_binding(handle, binding)
        assert raised.value.code == "context_capacity" and entry.binding is None
        with pytest.raises(ContextUnavailable) as raised:
            store.register(other)
        assert raised.value.code == "context_capacity"
        assert store.remove(handle) is False
    assert store.remove(handle)
    assert store.register(other).startswith("dc_")


def test_busy_context_survives_while_idle_context_is_evicted(tmp_path):
    contexts = [_design(tmp_path / str(i))[0] for i in range(3)]
    store = DesignContextStore(max_entries=2)
    first, second = (store.register(c) for c in contexts[:2])
    with store.pin(first):
        third = store.register(contexts[2])
        _expired(store, second)
        with store.pin(first) as entry:
            assert entry.context is contexts[0]
    with store.lease(third) as leased:
        assert leased is contexts[2]


def test_cross_context_and_source_policy_bindings_are_rejected(tmp_path):
    context, binding, _, _, log = _explicit(tmp_path / "one")
    other, _, _ = _design(tmp_path / "other")
    source = prepare_design_context(str(log), simulator="vcs", backend_preference="source", kdb_path=context.requested_kdb_path)
    assert source.identity_sha256 == context.identity_sha256
    store = DesignContextStore()
    for target in (other, source):
        handle = store.register(target)
        with store.pin(handle) as entry:
            with pytest.raises(ContextUnavailable) as raised:
                store.publish_binding(handle, binding)
            assert raised.value.code == "design_context_changed" and entry.binding is None


def test_conflicting_second_binding_cannot_replace_first_selection(tmp_path):
    context, first, candidate, _, _ = _explicit(tmp_path)
    (candidate / "_libFileMap").write_bytes(b"new_map_generation")
    second = prepare_backend_binding(context)
    assert first.anchor.identity_sha256 != second.anchor.identity_sha256
    store = DesignContextStore()
    handle = store.register(context)
    with store.pin(handle) as entry:
        assert store.publish_binding(handle, first) is first
        with pytest.raises(ContextUnavailable) as raised:
            store.publish_binding(handle, second)
        assert raised.value.code == "design_context_binding_conflict" and entry.binding is first


@pytest.mark.parametrize("reason", ["npi_load_failed", "npi_lsf_npi_unavailable"])
def test_only_availability_failures_are_remembered_while_pinned(tmp_path, reason):
    context, binding, _, _, _ = _explicit(tmp_path)
    store = DesignContextStore()
    handle = store.register(context)
    with store.pin(handle) as entry:
        store.note_npi_failure(handle, reason)
        assert entry.npi_failure is None  # No binding has been published yet.
        store.publish_binding(handle, binding)
        for ordinary in ("query_unsupported", "npi_dynamic_query_failed", "kdb_identity_changed", "private exception detail"):
            store.note_npi_failure(handle, ordinary)
            assert entry.npi_failure is None
        store.note_npi_failure(handle, reason)
        assert entry.npi_failure == reason
        status = binding.backend_status(npi_failure=entry.npi_failure)
        assert status["_npi_selection_reason"] == "npi_previous_load_failure"
        assert status["kdb_path"] == binding.backend_status()["kdb_path"]
        assert binding.receipt()["artifact"]["load_status"] == "not_checked"
    store.note_npi_failure(handle, "npi_load_failed" if reason != "npi_load_failed" else "npi_lsf_npi_unavailable")
    with store.pin(handle) as entry:
        assert entry.npi_failure == reason


def test_lease_still_rejects_changed_sources_and_pin_always_releases(tmp_path):
    context, binding, _, source, _ = _explicit(tmp_path)
    store = DesignContextStore(max_entries=1)
    handle = store.register(context)
    with store.pin(handle):
        store.publish_binding(handle, binding)
    source.write_text("module changed; endmodule\n")
    with pytest.raises(ContextUnavailable) as raised:
        with store.lease(handle):
            pytest.fail("changed source leased")
    assert raised.value.code == "design_context_changed"
    assert store.remove(handle)
