"""End-to-end dispatch tests for hierarchy handle tools.

Covers the server-side wiring: HandleStore lookup, prerequisite gating,
HandleErrorResult wrapping, and that the dispatcher returns the right
schema-validated payload for each of the six tools.
"""

from __future__ import annotations

import pytest

import server


@pytest.fixture(autouse=True)
def _reset_session_state():
    server.reset_session_state()
    yield
    server.reset_session_state()


def _seed_session_with_handle(handle="tbh_test0001"):
    """Register a synthetic full_result under `handle` and flip both
    session-state flags so prerequisite gating passes."""
    server._session_state["get_sim_paths"] = {
        "verif_root": "/tmp", "case_dir": "/tmp/c", "simulator": "vcs",
        "compile_log": "/tmp/c/elab.log",
    }
    server._session_state["build_tb_hierarchy"] = {
        "compile_log": "/tmp/c/elab.log", "simulator": "vcs",
    }
    full = {
        "project": {"top_module": "top", "simulator": "vcs"},
        "component_tree": {
            "top": {
                "u_cpu": {
                    "type": "module", "class": "cpu",
                    "source_file": "/p/cpu.sv", "source_line": 10,
                    "children": {
                        "u_alu": {"type": "module", "class": "alu",
                                  "source_file": "/p/alu.sv", "source_line": 3},
                    },
                },
            },
        },
        "files": {},
        "class_hierarchy": [],
        "interfaces": [],
        "compile_result": {"simulator": "vcs", "files": {"user": []}},
        "_scan_results": [
            {"path": "/p/cpu.sv", "name": "cpu.sv", "type": "module",
             "source_text": "", "classes": [], "class_extends": {},
             "modules": ["cpu"], "interfaces": [], "creates": [],
             "module_instances": [], "virtual_interfaces": []},
            {"path": "/p/alu.sv", "name": "alu.sv", "type": "module",
             "source_text": "", "classes": [], "class_extends": {},
             "modules": ["alu"], "interfaces": [], "creates": [],
             "module_instances": [], "virtual_interfaces": []},
        ],
    }
    server._handle_store.register(handle, full)
    return handle


# ── prerequisite gating ─────────────────────────────────────────────


@pytest.mark.anyio
async def test_handle_tool_blocked_when_hierarchy_not_built():
    # No session prefilled — build_tb_hierarchy prerequisite fails.
    result = await server._dispatch("get_tb_subtree", {"handle": "tbh_x"})
    payload = result.model_dump()
    assert payload.get("missing_step") == "build_tb_hierarchy"


# ── handle resolution ───────────────────────────────────────────────


@pytest.mark.anyio
async def test_handle_tool_returns_expired_for_unknown_handle():
    _seed_session_with_handle()  # registers a different handle
    result = await server._dispatch("get_tb_subtree", {"handle": "tbh_zzzz"})
    payload = result.model_dump()
    assert payload["error"] == "handle_expired"


# ── per-tool happy paths via dispatcher ─────────────────────────────


@pytest.mark.anyio
async def test_get_tb_subtree_via_dispatch():
    h = _seed_session_with_handle()
    result = await server._dispatch("get_tb_subtree", {"handle": h, "root": "", "depth": 2})
    payload = result.model_dump()
    assert payload["handle"] == h
    assert payload["node"]["inst"] == "top"
    cpu = next(c for c in payload["node"]["children"] if c["inst"] == "u_cpu")
    assert cpu["children"][0]["inst"] == "u_alu"


@pytest.mark.anyio
async def test_lookup_tb_files_via_dispatch():
    h = _seed_session_with_handle()
    result = await server._dispatch("lookup_tb_files", {"handle": h, "basename": "alu.sv"})
    payload = result.model_dump()
    assert payload["total"] == 1
    assert payload["matches"][0]["path"] == "/p/alu.sv"


@pytest.mark.anyio
async def test_lookup_tb_files_requires_filter():
    h = _seed_session_with_handle()
    result = await server._dispatch("lookup_tb_files", {"handle": h})
    payload = result.model_dump()
    assert payload["error"] == "filter_required"


@pytest.mark.anyio
async def test_find_tb_instance_via_dispatch():
    h = _seed_session_with_handle()
    result = await server._dispatch("find_tb_instance", {"handle": h, "module": "alu"})
    payload = result.model_dump()
    assert payload["total"] == 1
    assert payload["hits"][0]["path"] == "top.u_cpu.u_alu"


@pytest.mark.anyio
async def test_get_tb_file_detail_via_dispatch():
    h = _seed_session_with_handle()
    result = await server._dispatch("get_tb_file_detail", {"handle": h, "path": "/p/cpu.sv"})
    payload = result.model_dump()
    assert payload["path"] == "/p/cpu.sv"
    assert payload["file_type"] == "module"


@pytest.mark.anyio
async def test_get_tb_file_detail_unknown_path_suggests():
    h = _seed_session_with_handle()
    result = await server._dispatch("get_tb_file_detail", {"handle": h, "path": "/other/cpu.sv"})
    payload = result.model_dump()
    assert payload["error"] == "file_not_in_compile_set"
    assert payload["did_you_mean"] == ["/p/cpu.sv"]


@pytest.mark.anyio
async def test_get_tb_class_hierarchy_via_dispatch():
    h = _seed_session_with_handle()
    result = await server._dispatch("get_tb_class_hierarchy", {"handle": h})
    payload = result.model_dump()
    assert payload["total"] == 0  # no classes in fixture


@pytest.mark.anyio
async def test_dump_tb_section_via_dispatch():
    h = _seed_session_with_handle()
    result = await server._dispatch("dump_tb_section",
                                     {"handle": h, "section": "compile_result"})
    payload = result.model_dump()
    assert payload["section"] == "compile_result"
    assert payload["data"]["simulator"] == "vcs"


@pytest.mark.anyio
async def test_dump_tb_section_unknown_section():
    h = _seed_session_with_handle()
    result = await server._dispatch("dump_tb_section",
                                     {"handle": h, "section": "nope"})
    payload = result.model_dump()
    assert payload["error"] == "unknown_section"


# ── tool registry exposes all six ───────────────────────────────────


@pytest.mark.anyio
async def test_handle_tools_listed():
    tools = await server.list_tools()
    names = {t.name for t in tools}
    for n in (
        "get_tb_subtree", "lookup_tb_files", "find_tb_instance",
        "get_tb_file_detail", "get_tb_class_hierarchy", "dump_tb_section",
    ):
        assert n in names
