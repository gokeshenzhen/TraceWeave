"""Daily entry contract controls and real isolated Slang positive facts.

Only the independent executor runs these; all logs/waves here are fixtures.
"""
import json
from pathlib import Path

import pytest

import server
from src import source_graph_production
from src.source_graph_contract import SemanticScopeSearch
from tests.test_prepared_source_connectivity import isolation, anyio_backend, public, prepare, project


def test_hierarchy_purpose_codec_and_capability():
    source = SemanticScopeSearch(roots=("daily_top",), depth=2, purpose="hierarchy")
    assert SemanticScopeSearch.from_dict(source.to_dict()) == source
    connection = SemanticScopeSearch(roots=("daily_top",), depth=2)
    assert source != connection and "purpose" not in connection.to_dict()
    from dataclasses import replace
    from tests.test_source_graph_runtime import _request
    from src.source_graph_contract import (SourceGraphArtifactScope,
        compare_source_graph_artifact_scopes, compute_source_graph_artifact_key)
    request = _request()
    scope = request.artifact_identity.scope
    root = scope.top
    scopes = [replace(scope, semantic_search=SemanticScopeSearch(roots=(root,), depth=2, purpose=p))
              for p in ("hierarchy", "connectivity")]
    assert SourceGraphArtifactScope.from_dict(scopes[0].to_dict()) == scopes[0]
    assert compute_source_graph_artifact_key(replace(request.artifact_identity, scope=scopes[0])).digest != compute_source_graph_artifact_key(replace(request.artifact_identity, scope=scopes[1])).digest
    assert compare_source_graph_artifact_scopes(*scopes).value == "unproven"
    assert compare_source_graph_artifact_scopes(*reversed(scopes)).value == "unproven"
    from src.slang_hierarchy_source import capability_gap
    assert capability_gap("find_tb_instance", {"path": "daily_top.g.u_leaf"}) is None
    for tool, args in (("find_tb_instance", {"module": "daily_leaf"}),
                       ("get_tb_subtree", {"depth": -1}),
                       ("get_tb_subtree", {"count_mode": "exact"}),
                       ("dump_tb_section", {"section": "files_full"})):
        assert capability_gap(tool, args) is None
    assert capability_gap("get_tb_class_hierarchy", {}) is None


@pytest.mark.anyio
@pytest.mark.parametrize("policy", ["source", "auto"])
async def test_u1_real_instances_and_bounded_subtree(tmp_path, policy):
    source, log = project(tmp_path, "daily", "daily_top")
    h = await prepare(tmp_path, log, policy)
    assert not source_graph_production.source_graph_runtime_created()
    async def call(name, **args):
        return await public(tmp_path, name, {"design_handle": h, "timeout_sec": 60, **args})
    for operation, args in (
        ("find_tb_instance", {"module": "daily_leaf"}),
        ("get_tb_subtree", {"depth": -1}),
        ("get_tb_subtree", {"count_mode": "exact"})):
        value = await call(operation, **args)
        assert value["facts_origin"] == "slang" and value["coverage_status"] == "complete", value
        assert (value["total"] if operation == "find_tb_instance" else value["total_descendants"]) == (
            3 if operation == "find_tb_instance" else 6), value
    assert source_graph_production.source_graph_runtime_created()
    found = await call("find_tb_instance", path="daily_top.g.u_leaf")
    assert found.get("facts_origin") == "slang", found
    row = found["hits"][0]
    assert row["path"] == "daily_top.g.u_leaf" and row["parent"] == "daily_top.g"
    assert row["module"] == "daily_leaf" and row["kind"] == "module"
    assert row["definition_file"] == str(source) and row["definition_line"] == 1
    assert row["source_file"] == str(source) and row["source_line"] == 10
    assert "16" in row["parameterization"]["W"]
    same = await call("find_tb_instance", path="daily_top.g.u_leaf")
    assert same["artifact_identity"] == found["artifact_identity"] and same["hits"] == found["hits"]
    assert same["backend_status"]["source_graph"]["metrics"]["actual_build_count"] == 0
    tree = await call("get_tb_subtree", root="daily_top", depth=2, max_nodes=20)
    assert tree["facts_origin"] == "slang", tree
    root = tree["node"]
    assert {x["path"] for x in root["children"]} == {"daily_top.a", "daily_top.b", "daily_top.g"}
    assert root["child_count"] == 3
    g = next(x for x in root["children"] if x["path"] == "daily_top.g")
    assert g["kind"] == "generate" and g["parent"] == "daily_top"
    assert g["children"][0]["path"] == "daily_top.g.u_leaf"
    for args in ({"max_nodes": 1}, {"depth": 0}):
        limited = await call("get_tb_subtree", root="daily_top", **args)
        assert limited["output_truncated"] and limited["total_descendants"] is None
        assert limited["count_status"] == "lower_bound" and limited["count_lower_bound"] > 0
    for path in ("daily_top.payload", "daily_top.payload.flag", "daily_top.absent"):
        miss = await call("find_tb_instance", path=path)
        assert miss["hits"] == [] and miss["total"] is None and miss["coverage_status"] == "partial"


@pytest.mark.anyio
@pytest.mark.parametrize("failure", ["ordinary", "typed"])
async def test_u1_controlled_npi_failure_uses_real_slang(tmp_path, monkeypatch, failure):
    _, log = project(tmp_path, "daily", "daily_top")
    h = await prepare(tmp_path, log, "auto")
    original = server._select_prepared_backend
    calls = []
    class ControlledBackend:
        name, uses_external_worker = "verdi_npi", False
        def query_source(self, plan):
            calls.append(plan.operation)
            if failure == "typed":
                from src.npi_hierarchy_source import NpiHierarchyUnavailable
                raise NpiHierarchyUnavailable("npi_load_failed")
            raise RuntimeError("controlled failure; no SDK checkout")
    def select(entry, fallback):
        _, status, _ = original(entry, fallback)
        return ControlledBackend(), status, None
    monkeypatch.setattr(server, "_select_prepared_backend", select)
    value = await public(tmp_path, "find_tb_instance", {"design_handle": h,
                         "path": "daily_top.g.u_leaf", "timeout_sec": 60})
    assert calls == ["find_tb_instance"] and value.get("facts_origin") == "slang", value
    assert value["hits"][0]["source_line"] == 10
    assert any(x["backend"] == "verdi_npi" and x["status"] == "failed"
               for x in value["backend_status"]["attempted_backends"])


@pytest.mark.anyio
async def test_u2_failure_same_source_and_log_only(tmp_path, monkeypatch):
    source, log = project(tmp_path, "daily", "daily_top")
    h = await prepare(tmp_path, log, "source")
    wave = tmp_path / "fixture.vcd"
    wave.write_text('$timescale 1ps $end\n$scope module daily_top $end\n$var wire 1 ! flag $end\n'
                    '$upscope $end\n$enddefinitions $end\n#0\n0!\n#2000\n1!\n')
    run = tmp_path / "fixture.log"
    run.write_text(f'"{source}", 2: daily_top.g.u_leaf: started at 1000ps failed at 2000ps\n')
    event = {"event_id": "fixture", "kind": "assertion", "time_ps": 2000, "line": 1,
             "instance_path": "daily_top.g.u_leaf", "source_file": str(source), "message": "fixture assertion failed"}
    found = await public(tmp_path, "find_tb_instance", {"design_handle": h, "path": event["instance_path"], "timeout_sec": 60})
    for name, field in (("analyze_failure_event", "likely_instances"),
                        ("recommend_failure_debug_next_steps", "recommended_instances")):
        args = {"design_handle": h, "simulator": "vcs", "log_path": str(run), "wave_path": str(wave), "timeout_sec": 60,
                **({"failure_event": event} if name == "analyze_failure_event" else {})}
        value = await public(tmp_path, name, args)
        correlation = value["source_correlation"]
        assert correlation["facts_origin"] == "slang" and correlation["full_hierarchy_fallback"] is False
        assert len(correlation["queries"]) == 1
        assert correlation["queries"][0]["artifact_identity"] == found["artifact_identity"]
        row = next(x for x in value[field] if x["instance_path"] == event["instance_path"])
        assert row["source_evidence"]["source_line"] == 10 and row["source_evidence"]["definition_line"] == 1
        assert row["src"] == str(source)
    monkeypatch.setenv("TRACEWEAVE_SOURCE_GRAPH", "0")
    value = await public(tmp_path, "analyze_failure_event", args | {"failure_event": event})
    assert value["source_correlation"]["facts_origin"] == "log_only"
    assert value["source_correlation"]["full_hierarchy_fallback"] is False


@pytest.mark.anyio
async def test_u2_source_execution_change_at_final_validation(tmp_path, monkeypatch):
    from dataclasses import replace
    _, log = project(tmp_path, "daily", "daily_top")
    h = await prepare(tmp_path, log, "source")
    config = server.get_source_graph_execution_config()
    original_query = server._query_prepared_hierarchy
    original_binding = server._validate_design_binding
    completed = False
    async def query(*args, **kwargs):
        nonlocal completed
        result = await original_query(*args, **kwargs)
        assert result.get("facts_origin") == "slang", result
        completed = True
        return result
    async def binding(*args, **kwargs):
        result = await original_binding(*args, **kwargs)
        if completed:
            monkeypatch.setattr(server, "get_source_graph_execution_config",
                                lambda: replace(config, timeout_sec=config.timeout_sec + 1))
        return result
    monkeypatch.setattr(server, "_query_prepared_hierarchy", query)
    monkeypatch.setattr(server, "_validate_design_binding", binding)
    value = await public(tmp_path, "find_tb_instance", {"design_handle": h,
                         "path": "daily_top.g.u_leaf", "timeout_sec": 60})
    assert value.get("error_code") == "design_context_execution_changed", value
    assert "hits" not in value
