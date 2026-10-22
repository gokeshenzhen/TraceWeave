"""Handwritten invalidating SDK oracle, plus the real shared query engines."""
from contextlib import nullcontext
from types import SimpleNamespace
import threading

import pytest

import config
import server
from src import design_backend_binding, design_context_runtime, npi_hierarchy_source, verdi_npi_backend
from src.design_context import DesignContext
from tests.native_first_helpers import project, public
from tests.test_npi_hierarchy_source import FakeSdk, install_sdk, assert_no_live_capsules
from tests.test_npi_hierarchy_files import FileSdk, A, B
from tests.test_npi_hierarchy_classes import install as install_class_sdk


def forbidden(*a, **kw):
    pytest.fail("native source query prepared source or discovered an unnecessary KDB")


@pytest.fixture
def anyio_backend():
    return "asyncio"


@pytest.fixture(autouse=True)
def isolate(monkeypatch, tmp_path):
    server.reset_session_state()
    monkeypatch.setenv("TRACEWEAVE_NPI_EXECUTION", "local")
    monkeypatch.setenv("TRACEWEAVE_CONNECTIVITY_ROUTE", "auto")
    monkeypatch.setattr(config, "TRACEWEAVE_CACHE_ROOT", tmp_path / "cache")
    monkeypatch.setattr(design_context_runtime, "prepare_design_context", forbidden)
    monkeypatch.setattr(DesignContext, "current", forbidden)
    monkeypatch.setattr(verdi_npi_backend, "_NPI_INITIALIZED_IDS", set())
    monkeypatch.setattr(verdi_npi_backend, "_NPI_ACTIVE_DESIGNS", {})
    monkeypatch.setattr(verdi_npi_backend, "_NPI_LOAD_GENERATIONS", {})
    monkeypatch.setattr(verdi_npi_backend, "_silence_native_stdio", nullcontext)
    monkeypatch.setattr(verdi_npi_backend, "_install_shutdown_banner_silencer", lambda: None)
    monkeypatch.setattr(npi_hierarchy_source, "_OWNER_LOCK", threading.Lock())
    yield
    server.reset_session_state()


def use_sdk(monkeypatch, sdk, *, classes=False):
    (install_class_sdk if classes else install_sdk)(monkeypatch, sdk)
    monkeypatch.setattr(verdi_npi_backend, "_import_pynpi", lambda: (sdk.system, SimpleNamespace()))


@pytest.mark.anyio
@pytest.mark.parametrize("kdb_only", [False, True])
async def test_generated_scope_and_subtree_keep_handwritten_parent_oracle(tmp_path, monkeypatch, kdb_only):
    source, log, kdb = project(tmp_path, top="test_top")
    sdk = FakeSdk()
    use_sdk(monkeypatch, sdk)
    selector = {"kdb_path": str(kdb), "top_hint": "test_top"} if kdb_only else {"compile_log": str(log)}
    source.unlink()
    exact = await public("find_tb_instance", path="test_top.g[0].u", **selector)
    assert exact.get("facts_origin") == "verdi_npi", exact
    assert exact["hits"][0]["parent"] == "test_top.g[0]"
    assert exact["hits"][0]["parameterization"] == {"W": "2"}
    assert "context_identity_sha256" not in exact and "design_context" not in exact["backend_status"]
    tree = await public("get_tb_subtree", root="test_top", depth=2, **selector)
    assert tree.get("facts_origin") == "verdi_npi", tree
    assert {n["path"] for n in tree["node"]["children"]} == {"test_top.u0", "test_top.g[0]", "test_top.arr[0]"}
    generated = next(n for n in tree["node"]["children"] if n["path"] == "test_top.g[0]")
    assert generated["children"][0]["path"] == "test_top.g[0].u"
    assert sdk.global_count == 2
    assert_no_live_capsules(sdk)


@pytest.mark.anyio
@pytest.mark.parametrize("section", ["compile_result", "include_tree", "filelist_tree"])
async def test_metadata_exports_never_discover_load_or_prepare(tmp_path, monkeypatch, section):
    source, log, _ = project(tmp_path)
    monkeypatch.setattr(design_backend_binding, "probe_verdi_backend", forbidden)
    monkeypatch.setattr(verdi_npi_backend, "_import_pynpi", forbidden)
    result = await public("dump_tb_section", compile_log=str(log), section=section)
    assert result.get("facts_origin") == "compile_metadata", result
    assert result["section"] == section and result["data"] is not None
    assert result["native_route"]["source_context_status"] == "not_required"
    assert result["native_route"]["kdb_identity_sha256"] is None


@pytest.mark.anyio
async def test_metadata_file_filter_is_not_source_capture(tmp_path, monkeypatch):
    source, log, _ = project(tmp_path)
    monkeypatch.setattr(design_backend_binding, "probe_verdi_backend", forbidden)
    monkeypatch.setattr(verdi_npi_backend, "_import_pynpi", forbidden)
    result = await public("lookup_tb_files", compile_log=str(log), basename="design.sv")
    assert result.get("matches"), result
    assert result["matches"][0]["path"] == str(source)
    assert result["matches"][0]["source_status"] == "not_captured"
    assert result["ownership_basis"] == "compile_metadata"
    assert result["ownership_identity"] == result["native_route"]["compile_metadata_identity_sha256"]
    assert "context_identity_sha256" not in result


@pytest.mark.anyio
@pytest.mark.parametrize("operation,query", [
    ("get_tb_file_detail", {}),
    ("lookup_tb_files", {"has_module": "top_def"}),
    ("get_tb_class_hierarchy", {"root_class": "p.C"}),
    ("dump_tb_section", {"section": "files_full"}),
    ("dump_tb_section", {"section": "interfaces"}),
    ("dump_tb_section", {"section": "component_tree_full"}),
    ("dump_tb_section", {"section": "class_hierarchy_full"}),
])
async def test_semantic_file_class_and_export_use_native_owner_without_source(tmp_path, monkeypatch, operation, query):
    source, log, _ = project(tmp_path)
    other = tmp_path / "other.sv"
    other.write_text("package p; class C; endclass endpackage\n")
    log.write_text(log.read_text().replace("-top top", f"{other} -top top") + f"Parsing design file '{other}'\n")
    sdk = FileSdk()
    for node in sdk.nodes.values():
        for key in ("file", "def_file"):
            node[key] = {A: str(source), B: str(other)}.get(node.get(key), node.get(key))
    use_sdk(monkeypatch, sdk, classes=True)
    if operation == "get_tb_file_detail":
        query = {"path": str(source)}
    result = await public(operation, compile_log=str(log), **query)
    assert result.get("facts_origin") == "verdi_npi", result
    assert result["native_route"]["source_context_status"] == "not_required"
    if operation == "get_tb_file_detail":
        assert "top_def" in result["modules"] and "bus_if" in result["interfaces"]
        assert result["source_status"] == "not_captured" and result["ownership_basis"] == "compile_metadata"
    if operation == "get_tb_class_hierarchy":
        assert result["roots"][0]["qualified_name"] == "p.C"
    assert sdk.global_count == 1
    assert all(not raw.valid for raw in sdk.raws)


@pytest.mark.anyio
@pytest.mark.parametrize("operation,query", [
    ("lookup_tb_files", {"basename": "design.sv"}), ("get_tb_file_detail", {"path": "/design.sv"}),
    ("dump_tb_section", {"section": "compile_result"}), ("dump_tb_section", {"section": "interfaces"}),
])
async def test_pure_kdb_cannot_claim_compile_ownership(tmp_path, monkeypatch, operation, query):
    _, _, kdb = project(tmp_path)
    monkeypatch.setattr(verdi_npi_backend, "_import_pynpi", forbidden)
    result = await public(operation, kdb_path=str(kdb), top_hint="top", **query)
    assert result["error_code"] == "compile_metadata_required", result


@pytest.mark.anyio
async def test_two_projects_same_names_never_share_ownership(tmp_path):
    left_source, left_log, _ = project(tmp_path / "left")
    right_source, right_log, _ = project(tmp_path / "right")
    identities = []
    for log, expected in ((left_log, left_source), (right_log, right_source), (left_log, left_source)):
        result = await public("lookup_tb_files", compile_log=str(log), basename="design.sv")
        assert [r["path"] for r in result["matches"]] == [str(expected)]
        identities.append(result["ownership_identity"])
    assert identities[0] == identities[2] != identities[1]
