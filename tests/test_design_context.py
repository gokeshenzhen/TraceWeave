"""Independent capture/context/registry tests without an HDL frontend.

Fixtures specify their own source order, selected headers and raw-byte hashes.
Passing these tests establishes metadata and dependency-capture behavior, not
elaborated hierarchy, source replay completeness or semantic negative coverage.
"""
from __future__ import annotations

import builtins
from dataclasses import FrozenInstanceError, replace
import hashlib
import json
import os
import subprocess
import threading

import pytest

from src import compile_input_capture as capture_module
from src import compile_log_parser, sv_preprocessor, tb_hierarchy_builder
from src.cancellation import OperationCancelled, pop_cancel_event, push_cancel_event
from src.compile_input_capture import CaptureAborted, CaptureLimits, CompileInputCapture
from src.design_context_runtime import (
    ContextUnavailable,
    DesignContextStore,
    prepare_design_context,
)


def _write(path, text):
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(text)
    return path


def _vcs_log(path, sources, *, options="", tops=("top",)):
    command = f"vcs -sverilog {options} " + " ".join(map(str, sources))
    command += "".join(f" -top {top}" for top in tops)
    return _write(
        path,
        "Chronologic VCS simulator\nCommand: " + command + "\n"
        + "".join(f"Parsing design file '{source}'\n" for source in sources)
        + "Top Level Modules:\n" + "".join(f"       {top}\n" for top in tops),
    )


def _simple(root, *, text="module top; endmodule\n"):
    source = _write(root / "top.sv", text)
    log = _vcs_log(root / "compile.log", [source])
    return log, source


def _record_map(context):
    return {record.path: record for record in context.snapshot.records}


def _units(context):
    return [item["path"] for item in context.compile_result()["compile_evidence"]["ordered_compilation_units"]]


def _assert_expired(store, handle):
    with pytest.raises(ContextUnavailable) as raised:
        with store.lease(handle):
            pytest.fail("an expired handle yielded a context")
    assert raised.value.code == "handle_expired"


def test_prepare_captures_exact_input_bytes_without_a_tree(tmp_path):
    log, source = _simple(tmp_path)
    first = prepare_design_context(str(log))
    second = prepare_design_context(str(log))
    assert first.ordered_compile_logs == (str(log.resolve()),)
    assert first.selected_top == "top"
    assert len(first.identity_sha256) == 64
    assert first.identity_sha256 == second.identity_sha256
    assert first.current()
    assert first.snapshot.complete
    assert set(_record_map(first)) == {str(log.resolve()), str(source.resolve())}
    for path in (log, source):
        record = _record_map(first)[str(path.resolve())]
        assert record.sha256 == hashlib.sha256(path.read_bytes()).hexdigest()
    assert "component_tree" not in first.compile_result()
    assert "_scan_results" not in first.compile_result()


def test_prepare_many_sources_shares_option_index_and_captures_selected_headers(tmp_path, monkeypatch):
    """Preparing N sources must not reconstruct the N-source index N times."""
    header = _write(tmp_path / "headers" / "selected.svh", "localparam int WIDTH = 4;\n")
    sources = [
        _write(tmp_path / f"unit_{index}.sv", f'module unit_{index};\n`include "selected.svh"\nendmodule\n')
        for index in range(32)
    ]
    log = _vcs_log(tmp_path / "compile.log", sources,
                   options=f"+incdir+{header.parent}", tops=("unit_0",))
    constructions = []
    original = sv_preprocessor._CommandContextIndex.__init__

    def counted(self, compile_result, **kwargs):
        constructions.append(len(compile_result["compile_evidence"]["ordered_compilation_units"]))
        original(self, compile_result, **kwargs)

    monkeypatch.setattr(sv_preprocessor._CommandContextIndex, "__init__", counted)
    context = prepare_design_context(str(log))

    assert constructions == [len(sources)]
    assert _units(context) == list(map(str, sources))
    assert context.include_resolution_complete
    assert set(_record_map(context)) == {str(log), str(header), *map(str, sources)}
    assert context.current()
    header.write_text("localparam int WIDTH = 8;\n")
    assert context.current() is False


@pytest.mark.parametrize("simulator", ["vcs", "xcelium", "verilator"])
def test_prepare_uses_real_text_compile_parsers(tmp_path, simulator):
    source = _write(tmp_path / "top.sv", "module top; endmodule\n")
    log = tmp_path / "compile.log"
    if simulator == "vcs":
        _vcs_log(log, [source])
    elif simulator == "xcelium":
        _write(log, f"xrun\n  -top top\n  {source}\nfile: {source}\n  module worklib.top:v\n")
    else:
        _write(log, f"make: Entering directory '{tmp_path}'\nverilator --cc {source} --top-module top\n")
    context = prepare_design_context(str(log), simulator=simulator)
    assert context.compile_result()["simulator"] == simulator
    assert context.selected_top == "top"
    assert _units(context) == [str(source.resolve())]
    assert str(source.resolve()) in _record_map(context)
    assert context.current()


def test_filtered_tool_library_units_still_participate_in_snapshot(tmp_path, monkeypatch):
    library_root = tmp_path / "installed_tool_library"
    library = _write(library_root / "uvm_pkg.sv", "package uvm_pkg; endpackage\n")
    top = _write(tmp_path / "top.sv", "module top; endmodule\n")
    # Make a local fixture exercise the parser's actual library classification.
    monkeypatch.setattr(compile_log_parser, "EDA_LIB_PREFIXES", [str(library_root)])
    log = _vcs_log(tmp_path / "compile.log", [library, top])
    context = prepare_design_context(str(log))
    assert [item["path"] for item in context.compile_result()["files"]["user"]] == [str(top)]
    assert _units(context) == [str(library), str(top)]
    assert str(library) in _record_map(context)
    assert _record_map(context)[str(library)].sha256 == hashlib.sha256(library.read_bytes()).hexdigest()
    library.write_text("package uvm_pkg; parameter int W=2; endpackage\n")
    assert context.current() is False


def test_mixed_language_and_protected_inputs_remain_content_identified(tmp_path):
    top = _write(tmp_path / "top.sv", "module top; endmodule\n")
    vhdl = _write(tmp_path / "leaf.vhd", "entity leaf is end entity;\n")
    protected = _write(tmp_path / "opaque.svp", "vendor protected payload\n")
    log = _vcs_log(tmp_path / "compile.log", [vhdl, protected, top])
    context = prepare_design_context(str(log))
    assert _units(context) == list(map(str, (vhdl, protected, top)))
    for path in (vhdl, protected, top):
        assert _record_map(context)[str(path)].sha256 == hashlib.sha256(path.read_bytes()).hexdigest()
    assert "protected_region" in _record_map(context)[str(protected)].objective_exclusions
    vhdl.write_text("entity changed is end entity;\n")
    assert context.current() is False


def test_ordered_supplementary_logs_and_repeated_units_are_not_deduplicated(tmp_path):
    main, top = _simple(tmp_path)
    first = _write(tmp_path / "first.sv", "package first; endpackage\n")
    second = _write(tmp_path / "second.sv", "package second; endpackage\n")
    first_log = _vcs_log(tmp_path / "first.log", [first, first], tops=())
    second_log = _vcs_log(tmp_path / "second.log", [second], tops=())
    ordered = prepare_design_context(str(main), supplementary_compile_logs=(str(first_log), str(second_log)))
    reversed_ = prepare_design_context(str(main), supplementary_compile_logs=(str(second_log), str(first_log)))
    assert ordered.ordered_compile_logs == tuple(map(str, (main, first_log, second_log)))
    assert _units(ordered) == list(map(str, (top, first, first, second)))
    assert _units(reversed_) == list(map(str, (top, second, first, first)))
    assert ordered.identity_sha256 != reversed_.identity_sha256
    assert {str(p) for p in (main, first_log, second_log)} <= set(_record_map(ordered))


def test_supplement_content_change_invalidates_previous_context(tmp_path):
    main, _ = _simple(tmp_path)
    source = _write(tmp_path / "package.sv", "package p; endpackage\n")
    supplement = _vcs_log(tmp_path / "package.log", [source], tops=())
    before = prepare_design_context(str(main), supplementary_compile_logs=(str(supplement),))
    supplement.write_text(supplement.read_text() + "// new compile evidence\n")
    assert before.current() is False
    after = prepare_design_context(str(main), supplementary_compile_logs=(str(supplement),))
    assert before.identity_sha256 != after.identity_sha256


def test_top_selection_is_part_of_context_identity(tmp_path):
    source = _write(tmp_path / "tops.sv", "module alpha; endmodule\nmodule beta; endmodule\n")
    log = _vcs_log(tmp_path / "compile.log", [source], tops=("alpha", "beta"))
    alpha = prepare_design_context(str(log), top_hint="alpha")
    beta = prepare_design_context(str(log), top_hint="beta")
    assert alpha.selected_top == "alpha" and beta.selected_top == "beta"
    assert alpha.identity_sha256 != beta.identity_sha256


def test_source_raw_content_change_with_restored_mtime_changes_identity(tmp_path):
    log, source = _simple(tmp_path, text="module top; wire a; endmodule\n")
    first = prepare_design_context(str(log))
    stat = source.stat()
    source.write_text("module top; wire b; endmodule\n")
    os.utime(source, ns=(stat.st_atime_ns, stat.st_mtime_ns))
    assert first.current() is False
    second = prepare_design_context(str(log))
    assert first.identity_sha256 != second.identity_sha256


def test_current_rehashes_when_all_stat_evidence_is_coarsened(tmp_path, monkeypatch):
    log, source = _simple(tmp_path, text="module top; wire a; endmodule\n")
    context = prepare_design_context(str(log))
    old = source.stat()
    source.write_text("module top; wire b; endmodule\n")
    original_stat, original_fstat = os.stat, os.fstat

    def coarsened(result):
        return old if (result.st_dev, result.st_ino) == (old.st_dev, old.st_ino) else result

    monkeypatch.setattr(capture_module.os, "stat", lambda name, *args, **kwargs: coarsened(original_stat(name, *args, **kwargs)))
    monkeypatch.setattr(capture_module.os, "fstat", lambda fd: coarsened(original_fstat(fd)))
    assert context.current() is False


def _include_case(root):
    early, late = root / "early", root / "late"
    early.mkdir(parents=True)
    late.mkdir()
    selected = _write(late / "selected.svh", '`include "nested.svh"\n')
    nested = _write(late / "nested.svh", "wire from_nested;\n")
    source = _write(
        root / "top.sv",
        'module top;\n`define HEADER "selected.svh"\n'
        '`ifdef SELECT\n`include `HEADER\n`else\n`include "inactive_missing.svh"\n`endif\nendmodule\n',
    )
    log = _vcs_log(root / "compile.log", [source], options=f"+define+SELECT +incdir+{early}+{late}")
    return log, source, selected, nested, early


def test_macro_conditional_and_nested_include_capture_matches_written_fixture(tmp_path):
    log, source, selected, nested, _ = _include_case(tmp_path)
    context = prepare_design_context(str(log))
    assert context.include_resolution_complete
    assert set(_record_map(context)) == {str(p) for p in (log, source, selected, nested)}
    assert not any("inactive_missing" in path for path in _record_map(context))
    assert context.current()


def test_recorded_include_is_captured_even_when_current_text_disagrees(tmp_path):
    log, source = _simple(tmp_path)
    header = _write(tmp_path / "recorded.svh", "wire compiled_header;\n")
    log.write_text(log.read_text().replace(
        "Top Level Modules:",
        f"Parsing included file '{header}'\nBack to file '{source}'\nTop Level Modules:",
    ))
    context = prepare_design_context(str(log))
    assert str(header) in _record_map(context)
    assert _record_map(context)[str(header)].sha256 == hashlib.sha256(header.read_bytes()).hexdigest()
    assert context.include_resolution_complete is False
    assert "include_evidence_mismatch" in context.gaps


def test_earlier_include_shadow_creation_invalidates_old_context(tmp_path):
    log, _, selected, _, early = _include_case(tmp_path)
    context = prepare_design_context(str(log))
    shadow = _write(early / "selected.svh", "wire earlier_choice;\n")
    assert context.current() is False
    refreshed = prepare_design_context(str(log))
    assert refreshed.identity_sha256 != context.identity_sha256
    assert str(shadow) in _record_map(refreshed)
    assert str(selected) not in _record_map(refreshed)


def test_include_search_order_change_changes_identity_and_selected_header(tmp_path):
    log, source, _, _, early = _include_case(tmp_path)
    shadow = _write(early / "selected.svh", "wire early;\n")
    late = tmp_path / "late"
    first = prepare_design_context(str(log))
    _vcs_log(log, [source], options=f"+define+SELECT +incdir+{late}+{early}")
    second = prepare_design_context(str(log))
    assert first.identity_sha256 != second.identity_sha256
    assert str(shadow) in _record_map(first) and str(shadow) not in _record_map(second)


@pytest.mark.parametrize("alias_kind", ["source", "filelist", "include", "log"])
def test_retargeting_equal_content_alias_is_a_context_change(tmp_path, alias_kind):
    root = tmp_path / alias_kind
    root.mkdir()
    original = _write(root / "original.sv", "module top; endmodule\n")
    replacement = _write(root / "replacement.sv", original.read_text())
    source = root / "source.sv"
    if alias_kind == "source":
        source.symlink_to(original)
        alias, target = source, replacement
        log = _vcs_log(root / "compile.log", [source])
    elif alias_kind == "filelist":
        first_list = _write(root / "first.f", f"{original}\n")
        second_list = _write(root / "second.f", first_list.read_text())
        alias = root / "design.f"
        alias.symlink_to(first_list)
        target = second_list
        log = _write(root / "compile.log", f"Chronologic VCS\nCommand: vcs -sverilog -f {alias} -top top\nParsing design file '{original}'\n")
    elif alias_kind == "include":
        first_header = _write(root / "first.svh", "wire header;\n")
        second_header = _write(root / "second.svh", first_header.read_text())
        alias = root / "header.svh"
        alias.symlink_to(first_header)
        target = second_header
        _write(source, 'module top;\n`include "header.svh"\nendmodule\n')
        log = _vcs_log(root / "compile.log", [source])
    else:
        first_log = _vcs_log(root / "first.log", [original])
        second_log = _write(root / "second.log", first_log.read_text())
        alias = root / "compile.log"
        alias.symlink_to(first_log)
        target, log = second_log, alias
    context = prepare_design_context(str(log))
    alias.unlink()
    alias.symlink_to(target)
    assert context.current() is False


def test_filelist_contents_are_in_context_identity(tmp_path):
    _, source = _simple(tmp_path)
    filelist = _write(tmp_path / "design.f", f"+define+MODE=1\n{source}\n")
    log = _write(tmp_path / "compile.log", f"Chronologic VCS\nCommand: vcs -sverilog -f {filelist} -top top\nParsing design file '{source}'\n")
    first = prepare_design_context(str(log))
    assert str(filelist) in _record_map(first)
    filelist.write_text(filelist.read_text().replace("MODE=1", "MODE=2"))
    assert first.current() is False
    second = prepare_design_context(str(log))
    assert first.identity_sha256 != second.identity_sha256


def test_referenced_environment_change_invalidates_include_selection(tmp_path, monkeypatch):
    first_dir, second_dir = tmp_path / "first", tmp_path / "second"
    _write(first_dir / "header.svh", "wire first;\n")
    _write(second_dir / "header.svh", "wire second;\n")
    log, source = _simple(tmp_path, text='module top;\n`include "header.svh"\nendmodule\n')
    variable = "TRACEWEAVE_TEST_CONTEXT_HEADERS"
    monkeypatch.setenv(variable, str(first_dir))
    _vcs_log(log, [source], options=f"+incdir+${variable}")
    context = prepare_design_context(str(log))
    assert str(first_dir / "header.svh") in _record_map(context)
    assert context.current()
    monkeypatch.setenv(variable, str(second_dir))
    assert context.current() is False


def test_explicit_source_preference_survives_context_and_registry(tmp_path):
    log, _ = _simple(tmp_path)
    context = prepare_design_context(str(log), backend_preference="source")
    assert context.backend_preference == "source"
    store = DesignContextStore()
    handle = store.register(context)
    with store.lease(handle) as leased:
        assert leased.backend_preference == "source"


@pytest.mark.parametrize("kind", ["missing_source", "missing_include", "unexpanded_macro", "cross_unit_macro", "duplicate_phase"])
def test_dependency_uncertainty_is_not_reported_as_complete(tmp_path, kind):
    log, source = _simple(tmp_path)
    supplements = ()
    if kind == "missing_source":
        source.unlink()
    elif kind == "missing_include":
        source.write_text('module top;\n`include "absent.svh"\nendmodule\n')
    elif kind == "unexpanded_macro":
        source.write_text('`define EMIT `include "hidden.svh"\n`EMIT\nmodule top; endmodule\n')
        _write(tmp_path / "hidden.svh", "wire hidden;\n")
    elif kind == "cross_unit_macro":
        definitions = _write(tmp_path / "defines.sv", '`define HEADER "shared.svh"\n')
        source.write_text('`include `HEADER\nmodule top; endmodule\n')
        _write(tmp_path / "shared.svh", "wire shared;\n")
        _vcs_log(log, [definitions, source])
    else:
        source.write_text('`ifdef FIRST\n`include "first.svh"\n`else\n`include "second.svh"\n`endif\nmodule top; endmodule\n')
        _write(tmp_path / "first.svh", "wire first;\n")
        _write(tmp_path / "second.svh", "wire second;\n")
        _vcs_log(log, [source], options="+define+FIRST")
        supplement = _vcs_log(tmp_path / "second.log", [source], options="+define+SECOND", tops=())
        supplements = (str(supplement),)
    context = prepare_design_context(str(log), supplementary_compile_logs=supplements)
    assert context.gaps
    assert context.include_resolution_complete is False


def test_include_depth_limit_is_an_explicit_gap(tmp_path):
    log, _, _, _, _ = _include_case(tmp_path)
    context = prepare_design_context(str(log), max_include_depth=0)
    assert context.include_resolution_complete is False
    assert "include_depth_exceeded" in context.gaps


@pytest.mark.parametrize("budget", ["files", "bytes", "references"])
def test_source_capture_budget_returns_explicit_partial_context(tmp_path, budget):
    log, source = _simple(tmp_path)
    if budget == "files":
        limits = CaptureLimits(max_files=1)
    elif budget == "references":
        # Parsing observes the log and source spelling before normalizing.
        # The third reference is introduced by dependency collection.
        source.write_text('`include "header.svh"\nmodule top; endmodule\n')
        _write(tmp_path / "header.svh", "wire header;\n")
        limits = CaptureLimits(max_references=2)
    else:
        limits = CaptureLimits(max_bytes=log.stat().st_size + source.stat().st_size - 1)
    context = prepare_design_context(str(log), limits=limits)
    expected = {"files": "source_capture_file_limit", "bytes": "source_capture_byte_limit", "references": "source_capture_reference_limit"}[budget]
    assert expected in context.gaps
    assert context.include_resolution_complete is False
    assert str(log) in _record_map(context)
    if budget == "references":
        assert str(source) in _record_map(context)
        assert str(tmp_path / "header.svh") not in _record_map(context)
    else:
        assert str(source) not in _record_map(context)


def test_reference_budget_exhaustion_during_parse_aborts(tmp_path):
    log, _ = _simple(tmp_path)
    with pytest.raises(CaptureAborted) as raised:
        prepare_design_context(str(log), limits=CaptureLimits(max_references=1))
    assert raised.value.code == "reference_limit"


@pytest.mark.parametrize("kind", ["missing", "unparseable"])
def test_unusable_primary_log_does_not_return_a_context(tmp_path, kind):
    log = tmp_path / "compile.log"
    if kind == "unparseable":
        log.write_text("unrelated application output\n")
    with pytest.raises((OSError, ValueError, RuntimeError)):
        prepare_design_context(str(log))


@pytest.mark.parametrize("failure", ["cancel", "timeout", "changed"])
def test_failure_after_source_read_cannot_publish_context(tmp_path, monkeypatch, failure):
    log, source = _simple(tmp_path)
    original = CompileInputCapture.read_bytes
    event = threading.Event()
    now = [100.0]
    monkeypatch.setattr(capture_module.time, "monotonic", lambda: now[0])
    triggered = False

    def after_source(self, path):
        nonlocal triggered
        result = original(self, path)
        if os.path.realpath(path) == str(source) and not triggered:
            triggered = True
            if failure == "cancel":
                event.set()
            elif failure == "timeout":
                now[0] = 200.0
            else:
                source.write_text("module top; wire changed; endmodule\n")
        return result

    monkeypatch.setattr(CompileInputCapture, "read_bytes", after_source)
    token = push_cancel_event(event)
    try:
        expected = OperationCancelled if failure == "cancel" else CaptureAborted
        with pytest.raises(expected) as raised:
            prepare_design_context(str(log), limits=CaptureLimits(timeout_sec=1))
        assert triggered
        if failure != "cancel":
            assert raised.value.code == ("timeout" if failure == "timeout" else "input_changed")
    finally:
        pop_cancel_event(token)


def test_context_and_compile_result_are_deeply_immutable(tmp_path):
    log, _ = _simple(tmp_path)
    context = prepare_design_context(str(log))
    original = context.compile_result()
    edited = context.compile_result()
    edited["files"]["user"][0]["path"] = "/unrelated/replacement.sv"
    edited["compile_evidence"]["ordered_compilation_units"].clear()
    edited["top_modules"].append("unrelated_top")
    assert context.compile_result() == original
    with pytest.raises(FrozenInstanceError):
        context.identity_sha256 = "0" * 64
    with pytest.raises(FrozenInstanceError):
        context.snapshot.records[0].sha256 = "0" * 64
    assert isinstance(context.metrics, tuple)
    metrics = dict(context.metrics)
    metrics["test_mutation"] = 1
    assert "test_mutation" not in dict(context.metrics)
    assert context.current()


def test_prepare_releases_all_capture_bodies_and_retains_only_facts(tmp_path, monkeypatch):
    sentinel = "PRIVATE_SOURCE_BODY_SENTINEL_7f08b9"
    log, _ = _simple(tmp_path, text=f"// {sentinel}\nmodule top; endmodule\n")
    captures = []
    original = CompileInputCapture.__init__

    def observe(self, *args, **kwargs):
        original(self, *args, **kwargs)
        captures.append(self)

    monkeypatch.setattr(CompileInputCapture, "__init__", observe)
    context = prepare_design_context(str(log))
    assert captures
    assert all(capture.body_count == 0 and capture.resident_bytes == 0 for capture in captures)
    assert sentinel not in repr(context)
    assert sentinel not in repr(context.compile_result())


def test_dependency_collector_returns_facts_without_expanding_hierarchy_macros(tmp_path, monkeypatch):
    source = _write(tmp_path / "top.sv", '`define INSTANCE leaf u0();\n`INSTANCE\n`include "header.svh"\n')
    header = _write(tmp_path / "header.svh", "wire declared;\n")
    result = {"compile_cwd": str(tmp_path), "compile_command": f"vcs {source}"}

    def forbidden(*args, **kwargs):
        pytest.fail("dependency collection invoked hierarchy macro expansion")

    monkeypatch.setattr(sv_preprocessor, "_expand_hierarchy_macro_line", forbidden)
    facts = sv_preprocessor.SystemVerilogPreprocessor(result).collect_dependencies(str(source))
    assert str(header) in facts.included_paths
    assert (str(source), str(header)) in facts.ordered_includes
    assert not hasattr(facts, "text") and not hasattr(facts, "root_text")
    assert not hasattr(facts, "trusted_hierarchy_text")
    with pytest.raises(FrozenInstanceError):
        facts.complete = True


def test_prepare_has_zero_hidden_hierarchy_scan_frontend_or_backend_calls(tmp_path, monkeypatch):
    log, _, _, _, _ = _include_case(tmp_path)

    def forbidden(*args, **kwargs):
        pytest.fail("capture-only prepare started hierarchy, semantic or licensed work")

    for name in ("build_hierarchy", "build_component_tree", "_scan_user_files", "scan_sv_file"):
        if hasattr(tb_hierarchy_builder, name):
            monkeypatch.setattr(tb_hierarchy_builder, name, forbidden)
    monkeypatch.setattr(sv_preprocessor.SystemVerilogPreprocessor, "preprocess", forbidden)
    monkeypatch.setattr(sv_preprocessor, "_expand_hierarchy_macro_line", forbidden)
    monkeypatch.setattr(subprocess, "Popen", forbidden)
    monkeypatch.setattr(subprocess, "run", forbidden)
    monkeypatch.setattr(os, "system", forbidden)
    original_import = builtins.__import__

    def guarded_import(name, *args, **kwargs):
        if name.split(".")[0] in {"pyslang", "pynpi"}:
            forbidden()
        return original_import(name, *args, **kwargs)

    monkeypatch.setattr(builtins, "__import__", guarded_import)
    context = prepare_design_context(str(log))
    assert context.include_resolution_complete
    assert context.current()


def test_store_unknown_cross_store_and_removed_handles_expire(tmp_path):
    log, _ = _simple(tmp_path)
    context = prepare_design_context(str(log))
    store, fresh_store = DesignContextStore(), DesignContextStore()
    handle = store.register(context)
    assert handle.startswith("dc_")
    with store.lease(handle) as leased:
        assert leased.identity_sha256 == context.identity_sha256
    _assert_expired(store, "dc_unknown")
    _assert_expired(fresh_store, handle)
    fresh_handle = fresh_store.register(context)
    assert fresh_handle != handle
    _assert_expired(store, fresh_handle)
    _assert_expired(fresh_store, handle)
    store.remove(handle)
    _assert_expired(store, handle)


@pytest.mark.parametrize("operation", ["register", "lease"])
def test_store_rejects_changed_context_before_exposing_it(tmp_path, operation):
    log, source = _simple(tmp_path)
    context = prepare_design_context(str(log))
    store = DesignContextStore()
    handle = store.register(context) if operation == "lease" else None
    source.write_text("module top; wire later; endmodule\n")
    with pytest.raises(ContextUnavailable) as raised:
        if operation == "register":
            store.register(context)
        else:
            with store.lease(handle):
                pytest.fail("stale context was exposed")
    assert raised.value.code == "design_context_changed"


def test_store_full_busy_capacity_preserves_lease_then_allows_eviction(tmp_path):
    first_log, _ = _simple(tmp_path / "first")
    second_log, _ = _simple(tmp_path / "second")
    first = prepare_design_context(str(first_log))
    second = prepare_design_context(str(second_log))
    store = DesignContextStore(max_entries=1)
    first_handle = store.register(first)
    with store.lease(first_handle) as leased:
        with pytest.raises(ContextUnavailable) as raised:
            store.register(second)
        assert raised.value.code == "context_capacity"
        assert leased.identity_sha256 == first.identity_sha256
        with store.lease(first_handle) as still_present:
            assert still_present.identity_sha256 == first.identity_sha256
    second_handle = store.register(second)
    _assert_expired(store, first_handle)
    with store.lease(second_handle) as leased:
        assert leased.identity_sha256 == second.identity_sha256


@pytest.mark.parametrize("error", [LookupError, OperationCancelled])
def test_exceptional_lease_exit_releases_busy_capacity(tmp_path, error):
    first = prepare_design_context(str(_simple(tmp_path / "first")[0]))
    second = prepare_design_context(str(_simple(tmp_path / "second")[0]))
    store = DesignContextStore(max_entries=1)
    handle = store.register(first)
    with pytest.raises(error):
        with store.lease(handle):
            raise error("leave lease")
    replacement = store.register(second)
    _assert_expired(store, handle)
    with store.lease(replacement) as leased:
        assert leased.identity_sha256 == second.identity_sha256


def test_eviction_skips_busy_entry_even_when_it_is_older(tmp_path):
    contexts = [prepare_design_context(str(_simple(tmp_path / str(index))[0])) for index in range(3)]
    store = DesignContextStore(max_entries=2)
    first, second = (store.register(context) for context in contexts[:2])
    with store.lease(first):
        third = store.register(contexts[2])
        _assert_expired(store, second)
        with store.lease(first) as leased:
            assert leased.identity_sha256 == contexts[0].identity_sha256
        with store.lease(third) as leased:
            assert leased.identity_sha256 == contexts[2].identity_sha256


def test_store_byte_budget_cannot_be_bypassed_by_first_registration(tmp_path):
    log, _ = _simple(tmp_path)
    context = prepare_design_context(str(log))
    store = DesignContextStore(max_bytes=1)
    with pytest.raises(ContextUnavailable) as raised:
        store.register(context)
    assert raised.value.code == "context_capacity"


def test_store_accepts_large_metadata_without_bypassing_explicit_byte_budget(tmp_path):
    context = prepare_design_context(str(_simple(tmp_path)[0]))
    metadata = context.compile_result()
    # A synthetic large diagnostic inventory exercises actual serialized
    # metadata accounting, without changing source observations or the getter.
    metadata["diagnostic_inventory"] = "x" * (5 * 1024 * 1024)
    large = replace(context, compile_result_json=json.dumps(metadata))
    assert 16 * 1024 * 1024 < large.estimated_bytes < 64 * 1024 * 1024
    small_store = DesignContextStore(max_bytes=16 * 1024 * 1024)
    original_handle = small_store.register(context)
    with pytest.raises(ContextUnavailable) as raised:
        small_store.register(large)
    assert raised.value.code == "context_capacity"
    with small_store.lease(original_handle) as original:
        assert original is context

    store = DesignContextStore()
    handle = store.register(large)
    with store.lease(handle) as leased:
        assert leased is large
        assert leased.current()

    metadata["diagnostic_inventory"] = "x" * (17 * 1024 * 1024)
    oversized = replace(context, compile_result_json=json.dumps(metadata))
    with pytest.raises(ContextUnavailable) as raised:
        store.register(oversized)
    assert raised.value.code == "context_capacity"
    with store.lease(handle) as retained:
        assert retained is large


def _log_anchored_filelist_context(tmp_path, monkeypatch, ambient, *, matching_order=True):
    """The log, rather than the parent's environment, fixes this build root."""
    root = tmp_path / "compiled_design"
    variable = "TW_CONTEXT_REPLAY_ROOT"
    top = _write(root / "rtl" / "top.sv", "module top; leaf u_leaf(); endmodule\n")
    leaf = _write(root / "rtl" / "leaf.sv", "module leaf; endmodule\n")
    outer = root / "lists" / "outer.f"
    nested = _write(root / "lists" / "nested.f", f"${variable}/rtl/leaf.sv\n")
    entries = [f"${variable}/rtl/top.sv\n", f"-f ${variable}/lists/nested.f\n"]
    _write(outer, "".join(entries if matching_order else reversed(entries)))
    log = _write(
        root / "tb" / "comp.log",
        "Chronologic VCS simulator\n"
        f"Command: vcs -sverilog -f ${variable}/lists/outer.f -top top\n"
        f"Parsing design file '{top}'\n"
        f"Parsing design file '{leaf}'\n"
        "Top Level Modules:\n       top\n",
    )
    if ambient == "unset":
        monkeypatch.delenv(variable, raising=False)
    else:
        monkeypatch.setenv(variable, str(tmp_path / "unrelated_previous_design"))
    return root, variable, log, outer, nested, top, leaf


@pytest.mark.parametrize("ambient", ["unset", "stale"])
def test_validated_replay_environment_captures_real_nested_filelists(tmp_path, monkeypatch, ambient):
    root, variable, log, outer, nested, top, leaf = _log_anchored_filelist_context(
        tmp_path, monkeypatch, ambient,
    )
    original_ambient = os.environ.get(variable)
    context = prepare_design_context(str(log))
    assert _units(context) == [str(top), str(leaf)]
    assert dict(context.replay_environment)[variable] == str(root)
    assert dict(context.environment)[variable] == original_ambient
    assert os.environ.get(variable) == original_ambient
    assert context.snapshot.complete
    assert context.include_resolution_complete
    assert not {"compile_input_unavailable", "compile_inputs_incomplete", "compile_options_incomplete"}.intersection(context.gaps)
    assert context.unavailable == ()
    assert set(_record_map(context)) == {str(path) for path in (log, outer, nested, top, leaf)}
    for path in (outer, nested):
        assert _record_map(context)[str(path)].sha256 == hashlib.sha256(path.read_bytes()).hexdigest()
    assert all("$" not in record.path for record in context.snapshot.records)
    assert context.current()
    nested.write_text(nested.read_text() + "// changed nested compile dependency\n")
    assert context.current() is False


@pytest.mark.parametrize("ambient", ["unset", "stale"])
def test_filelist_order_mismatch_cannot_authorize_replay_binding_or_clear_gaps(tmp_path, monkeypatch, ambient):
    _, variable, log, _, _, top, leaf = _log_anchored_filelist_context(
        tmp_path, monkeypatch, ambient, matching_order=False,
    )
    original_ambient = os.environ.get(variable)
    context = prepare_design_context(str(log))
    assert _units(context) == [str(top), str(leaf)]
    assert variable not in dict(context.replay_environment)
    assert {"compile_inputs_incomplete", "compile_options_incomplete"} <= set(context.gaps)
    assert context.include_resolution_complete is False
    assert os.environ.get(variable) == original_ambient
