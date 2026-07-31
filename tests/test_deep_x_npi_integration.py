"""Opt-in regression for the real deep-X FSDB + Verdi KDB route.

The default test suite must never consume a Verdi license.  Set
``TRACEWEAVE_RUN_EDA_INTEGRATION=1`` to turn this into a strict acceptance
test: once enabled, missing tools or fixture artifacts are failures, not skips.
"""

from __future__ import annotations

import os
from pathlib import Path

import pytest

import server
import src.connectivity_backend as connectivity_backend
from config import get_fsdb_runtime_info
from src.compile_log_parser import parse_compile_log
from src.verdi_backend import probe_verdi_backend


FIXTURE_DIR = Path(__file__).parent / "fixtures" / "deep_x_npi"
WORK_DIR = FIXTURE_DIR / "work"
COMPILE_LOG = WORK_DIR / "compile.log"
FSDB = WORK_DIR / "deep_x.fsdb"
KDB = WORK_DIR / "simv.daidir" / "kdb.elab++"
RTL = FIXTURE_DIR / "rtl" / "deep_uart_x.sv"
TB = FIXTURE_DIR / "tb" / "deep_x_tb.sv"
TOP = "uart_deep_x_tb"
SIGNAL = f"{TOP}.apb_prdata[7:0]"
X_TIME_PS = 66_000

_ENABLED = os.environ.get("TRACEWEAVE_RUN_EDA_INTEGRATION") == "1"
pytestmark = [
    pytest.mark.eda_integration,
    pytest.mark.skipif(
        not _ENABLED,
        reason="set TRACEWEAVE_RUN_EDA_INTEGRATION=1 to run real FSDB+KDB coverage",
    ),
]


def _require_file(path: Path, label: str) -> None:
    if not path.is_file() or path.stat().st_size == 0:
        pytest.fail(
            f"{label} is missing or empty: {path}. "
            f"Regenerate the fixture with {FIXTURE_DIR / 'run.sh'}.",
            pytrace=False,
        )


def _require_dir(path: Path, label: str) -> None:
    if not path.is_dir():
        pytest.fail(
            f"{label} is missing: {path}. "
            f"Regenerate the fixture with {FIXTURE_DIR / 'run.sh'}.",
            pytrace=False,
        )


def _result_dump(result) -> dict:
    return result.model_dump(exclude_none=False)


@pytest.fixture(autouse=True)
def _isolated_server_state():
    server.reset_session_state()
    yield

    cached = server._parser_cache.pop(str(FSDB), None)
    if cached is not None:
        server._dispose_cached_object(cached[1])
    server.reset_session_state()


@pytest.mark.anyio
async def test_public_trace_x_source_static_then_real_local_npi(monkeypatch):
    """The same FSDB input exposes Static's blind spot and NPI's deep driver."""

    verdi_home_raw = os.environ.get("VERDI_HOME")
    if not verdi_home_raw:
        pytest.fail(
            "TRACEWEAVE_RUN_EDA_INTEGRATION=1 requires VERDI_HOME; refusing to skip",
            pytrace=False,
        )
    verdi_home = Path(verdi_home_raw)
    _require_dir(verdi_home, "VERDI_HOME")
    _require_dir(verdi_home / "share" / "NPI" / "python", "Verdi pynpi tree")
    _require_file(COMPILE_LOG, "fixture compile log")
    _require_file(FSDB, "fixture FSDB")
    _require_dir(KDB, "fixture elaborated KDB")
    _require_file(RTL, "fixture RTL")
    _require_file(TB, "fixture testbench")

    runtime = get_fsdb_runtime_info()
    if not runtime.get("enabled"):
        pytest.fail(
            "FSDB runtime is unavailable while the EDA integration test is enabled: "
            f"{runtime.get('message')}",
            pytrace=False,
        )

    compile_result = parse_compile_log(str(COMPILE_LOG), "vcs")
    compiled_sources = {
        Path(entry["path"]).resolve()
        for entry in compile_result.get("files", {}).get("user", [])
    }
    assert compiled_sources == {RTL.resolve(), TB.resolve()}, compile_result
    assert compile_result.get("top_modules") == [TOP], compile_result

    probe = probe_verdi_backend(compile_result, str(COMPILE_LOG))
    assert probe.get("kdb_flow") == "vcs_two_step", probe
    assert Path(probe.get("kdb_path") or "").resolve() == KDB.resolve(), probe

    # This regression is deliberately local.  It must neither inherit an LSF
    # policy nor submit a scheduler job while checking the local NPI route.
    monkeypatch.setenv("TRACEWEAVE_NPI_EXECUTION", "local")

    trace_args = {
        "signal_path": SIGNAL,
        "wave_path": str(FSDB),
        "compile_log": str(COMPILE_LOG),
        "time_ps": X_TIME_PS,
        "top_hint": TOP,
        "max_depth": 20,
        "simulator": "vcs",
    }

    value = await server._dispatch(
        "get_signal_at_time",
        {
            "wave_path": str(FSDB),
            "signal_path": SIGNAL,
            "time_ps": X_TIME_PS,
        },
    )
    assert value.value["bin"] == "xxxxxxxx", _result_dump(value)

    # Build the real compile-log hierarchy and run the first trace with Static
    # forced at the one public selection seam.  This must not initialise NPI.
    with monkeypatch.context() as static_patch:
        static_patch.setattr(
            connectivity_backend,
            "select_backend",
            lambda _status: connectivity_backend.StaticConnectivityBackend(),
        )
        hierarchy = await server._dispatch(
            "build_tb_hierarchy",
            {"compile_log": str(COMPILE_LOG), "simulator": "vcs"},
        )
        assert hierarchy.project["top_module"] == TOP, _result_dump(hierarchy)
        static_result = await server._dispatch("trace_x_source", trace_args)

    static_dump = _result_dump(static_result)
    assert static_result.backend_status.backend == "static", static_dump
    assert static_result.backend_status.actual_backend == "static", static_dump
    assert static_result.trace_restarted is False, static_dump
    assert static_result.trace_status == "driver_unresolved", static_dump
    assert static_result.trace_depth == 0, static_dump
    assert static_result.propagation_chain[0].has_x is True, static_dump
    assert not any(
        node.source_line == 20 and node.driver_kind == "always_ff"
        for node in static_result.propagation_chain
    ), static_dump

    # Remove the override and exercise the production selection/routing path.
    # If license checkout or NPI loading fails, trace_x_source honestly restarts
    # with Static; that is a hard failure here and the receipt is printed.
    npi_result = await server._dispatch("trace_x_source", trace_args)
    npi_dump = _result_dump(npi_result)
    assert npi_result.backend_status.backend == "verdi_npi", npi_dump
    assert npi_result.backend_status.actual_backend == "verdi_npi", npi_dump
    assert npi_result.backend_status.execution_mode == "local", npi_dump
    assert npi_result.backend_status.fallback_reason is None, npi_dump
    assert npi_result.trace_restarted is False, npi_dump
    assert npi_result.trace_status == "traced_partial_chain", npi_dump

    deep_nodes = [
        node
        for node in npi_result.propagation_chain
        if node.driver_kind == "always_ff" and node.source_line == 20
    ]
    assert len(deep_nodes) == 1, npi_dump
    deep = deep_nodes[0]
    assert deep.source_file is not None and deep.source_file.endswith(
        "rtl/deep_uart_x.sv"
    ), npi_dump
    assert deep.driver_confidence == "exact", npi_dump
    assert deep.trace_stop_reason == "no_upstream_candidates", npi_dump
