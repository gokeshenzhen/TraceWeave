from __future__ import annotations

import time

import pytest

import server
from config import HierarchyNpiOverlayConfig, get_hierarchy_npi_overlay_config
from src import cancellation


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
