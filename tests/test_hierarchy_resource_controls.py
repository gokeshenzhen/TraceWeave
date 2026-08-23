from __future__ import annotations

import asyncio
import threading
import time

import pytest

import server
from config import (
    CompileSourceIndexConfig,
    HierarchyNpiOverlayConfig,
    get_compile_source_index_config,
    get_hierarchy_npi_overlay_config,
)
from src import cancellation
from src.compile_source_runtime import CompileSourceIndexRuntime


@pytest.fixture(autouse=True)
def _clean_server_state():
    server.reset_session_state()
    yield
    server.reset_session_state()


@pytest.mark.parametrize(
    ("raw_mode", "expected"),
    [
        (None, HierarchyNpiOverlayConfig()),
        ("auto", HierarchyNpiOverlayConfig()),
        ("off", HierarchyNpiOverlayConfig(mode="off")),
        ("force", HierarchyNpiOverlayConfig(mode="force")),
        (
            "unexpected",
            HierarchyNpiOverlayConfig(
                mode="off",
                error_code="hierarchy_npi_overlay_config_invalid",
            ),
        ),
    ],
)
def test_hierarchy_npi_overlay_config_is_validated(
    monkeypatch,
    raw_mode,
    expected,
):
    if raw_mode is None:
        monkeypatch.delenv(
            "TRACEWEAVE_HIERARCHY_NPI_SOURCE_OVERLAY",
            raising=False,
        )
    else:
        monkeypatch.setenv(
            "TRACEWEAVE_HIERARCHY_NPI_SOURCE_OVERLAY",
            raw_mode,
        )
    assert get_hierarchy_npi_overlay_config() == expected


def test_compile_source_index_config_defaults_and_opt_out(monkeypatch):
    for name in (
        "TRACEWEAVE_COMPILE_SOURCE_INDEX",
        "TRACEWEAVE_COMPILE_SOURCE_INDEX_MAX_BYTES",
        "TRACEWEAVE_COMPILE_SOURCE_INDEX_MAX_FILES",
    ):
        monkeypatch.delenv(name, raising=False)

    assert get_compile_source_index_config() == CompileSourceIndexConfig()

    monkeypatch.setenv("TRACEWEAVE_COMPILE_SOURCE_INDEX", "off")
    assert get_compile_source_index_config() == CompileSourceIndexConfig(
        enabled=False
    )


@pytest.mark.parametrize(
    ("name", "value"),
    [
        ("TRACEWEAVE_COMPILE_SOURCE_INDEX_MAX_BYTES", "0"),
        ("TRACEWEAVE_COMPILE_SOURCE_INDEX_MAX_BYTES", "not-an-int"),
        ("TRACEWEAVE_COMPILE_SOURCE_INDEX_MAX_FILES", "0"),
        ("TRACEWEAVE_COMPILE_SOURCE_INDEX_MAX_FILES", "1000001"),
    ],
)
def test_compile_source_index_config_rejects_unsafe_limits(
    monkeypatch,
    name,
    value,
):
    monkeypatch.setenv(name, value)

    config = get_compile_source_index_config()

    assert config.enabled is False
    assert config.error_code == "compile_source_index_config_invalid"


@pytest.mark.anyio
async def test_hierarchy_internal_timeout_returns_structured_blocker(
    monkeypatch, tmp_path
):
    compile_log = tmp_path / "comp.log"
    compile_log.write_text(
        "Chronologic VCS simulator\n"
        "Top Level Modules:\n"
        "       top\n",
        encoding="utf-8",
    )
    monkeypatch.setenv("TRACEWEAVE_HIERARCHY_TIMEOUT", "0.02")

    def cancellable_slow_build(*_args, **_kwargs):
        while True:
            cancellation.check_cancelled()
            time.sleep(0.002)

    monkeypatch.setattr(server, "build_hierarchy", cancellable_slow_build)
    result = await server._dispatch(
        "build_tb_hierarchy",
        {"compile_log": str(compile_log), "simulator": "vcs"},
    )

    assert result.build_status == "blocked"
    assert result.hierarchy_handle == ""
    assert result.blocker == {
        "code": "hierarchy_timeout",
        "stage": "source_scan",
    }
    assert result.build_metrics["timeout_ms"] == 20.0
    assert server._session_state["build_tb_hierarchy"] is None
    assert server._compile_context_cache


@pytest.mark.anyio
async def test_hierarchy_timeout_covers_compile_log_parse(
    monkeypatch, tmp_path
):
    compile_log = tmp_path / "comp.log"
    compile_log.write_text("Chronologic VCS simulator\n", encoding="utf-8")
    monkeypatch.setenv("TRACEWEAVE_HIERARCHY_TIMEOUT", "0.02")

    def cancellable_slow_parse(*_args, **_kwargs):
        while True:
            cancellation.check_cancelled()
            time.sleep(0.002)

    monkeypatch.setattr(server, "_parse_merged_compile_context", cancellable_slow_parse)
    result = await server._dispatch(
        "build_tb_hierarchy",
        {"compile_log": str(compile_log), "simulator": "vcs"},
    )

    assert result.build_status == "blocked"
    assert result.hierarchy_handle == ""
    assert result.blocker == {
        "code": "hierarchy_timeout",
        "stage": "compile_log_parse",
    }
    assert result.build_metrics["timeout_ms"] == 20.0


@pytest.mark.anyio
async def test_hierarchy_source_byte_limit_blocks_before_source_scan(
    monkeypatch, tmp_path
):
    source = tmp_path / "top.sv"
    source.write_text("module top; logic value; endmodule\n", encoding="utf-8")
    compile_log = tmp_path / "comp.log"
    compile_log.write_text(
        "Chronologic VCS simulator\n"
        f"Parsing design file '{source}'\n"
        "Top Level Modules:\n"
        "       top\n",
        encoding="utf-8",
    )
    monkeypatch.setenv("TRACEWEAVE_HIERARCHY_MAX_SOURCE_BYTES", "1")

    def forbidden_build(*_args, **_kwargs):
        raise AssertionError("source scan must not start after failed preflight")

    monkeypatch.setattr(server, "build_hierarchy", forbidden_build)
    result = await server._dispatch(
        "build_tb_hierarchy",
        {"compile_log": str(compile_log), "simulator": "vcs"},
    )

    assert result.build_status == "blocked"
    assert result.blocker["code"] == "hierarchy_source_byte_limit_exceeded"
    assert result.blocker["stage"] == "source_preflight"
    assert result.build_metrics["source_byte_limit"] == 1
    assert result.build_metrics["source_bytes_planned"] > 1


@pytest.mark.anyio
async def test_parallel_hierarchy_and_structural_scan_share_source_index(
    monkeypatch,
    tmp_path,
):
    source = tmp_path / "top.sv"
    source.write_text("module top; logic value; endmodule\n", encoding="utf-8")
    compile_log = tmp_path / "comp.log"
    compile_log.write_text(
        "Chronologic VCS simulator\n"
        f"Parsing design file '{source}'\n"
        "Top Level Modules:\n"
        "       top\n",
        encoding="utf-8",
    )
    runtime = CompileSourceIndexRuntime()
    monkeypatch.setattr(server, "_compile_source_index_runtime", runtime)
    monkeypatch.setattr(server, "apply_npi_source_overlay", lambda *_args: None)
    hierarchy_entered = threading.Event()
    scan_entered = threading.Event()
    real_build = server.build_hierarchy

    def coordinated_build(*args, **kwargs):
        hierarchy_entered.set()
        assert scan_entered.wait(timeout=5)
        return real_build(*args, **kwargs)

    def coordinated_scan(**kwargs):
        scan_entered.set()
        assert hierarchy_entered.wait(timeout=5)
        loader = kwargs["source_loader"]
        assert loader is not None
        assert "module top" in loader(str(source))
        return {
            "scan_scope": "scope1",
            "eligible_file_count": 1,
            "files_scanned": 1,
            "coverage_status": "complete",
            "coverage_warnings": [],
            "total_risks": 0,
            "risks": [],
            "categories_scanned": ["slice_overlap"],
            "skipped_files": [],
        }

    monkeypatch.setattr(server, "build_hierarchy", coordinated_build)
    monkeypatch.setattr(server, "scan_structural_risks", coordinated_scan)

    hierarchy, scan = await asyncio.gather(
        server._dispatch(
            "build_tb_hierarchy",
            {"compile_log": str(compile_log), "simulator": "vcs"},
        ),
        server._dispatch(
            "scan_structural_risks",
            {"compile_log": str(compile_log), "simulator": "vcs"},
        ),
    )

    dispositions = {
        hierarchy.build_metrics["compile_source_index_disposition"],
        scan.scan_metrics["compile_source_index_disposition"],
    }
    assert "miss_build" in dispositions
    assert dispositions <= {"miss_build", "coalesced", "hit_active_session"}
    assert len(dispositions) == 2
    assert runtime.metrics_snapshot()["compile_source_runtime_build_count"] == 1
    assert scan.scan_metrics["compile_source_index_physical_read_count"] == 1
    assert runtime.metrics_snapshot()[
        "compile_source_runtime_active_session_count"
    ] == 0
