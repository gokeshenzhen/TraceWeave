import os
import sys

sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))

from src.connectivity_backend import (
    ConnectivityBackend,
    DeferredConnectivityFallbackBackend,
    StaticConnectivityBackend,
    select_backend,
)


def _mock_compile(monkeypatch, files, top_module="top_tb"):
    def fake_parse_compile_log(log_path, simulator="auto"):
        return {
            "top_modules": [top_module],
            "files": {
                "user": [
                    {"path": str(path), "type": "module", "category": "rtl"}
                    for path in files
                ],
            },
        }

    monkeypatch.setattr("src.signal_driver.parse_compile_log", fake_parse_compile_log)


def test_static_backend_satisfies_protocol():
    backend = StaticConnectivityBackend()
    assert isinstance(backend, ConnectivityBackend)
    assert backend.name == "static"


def test_static_find_driver_routes_to_explain(monkeypatch, tmp_path):
    rtl = tmp_path / "m.sv"
    rtl.write_text(
        """\
module top_tb;
  m u0();
endmodule

module m;
  logic a, b;
  assign b = a;
endmodule
"""
    )
    _mock_compile(monkeypatch, [rtl])
    backend = StaticConnectivityBackend()
    r = backend.find_driver(
        signal_path="top_tb.u0.b",
        wave_path="dummy.fsdb",
        compile_log="x",
    )
    # explain_signal_driver returns dict with these keys.
    assert r["resolved_rtl_name"] == "b"
    assert r["driver_status"] in ("resolved", "partial")


def test_static_find_loads_routes_to_signal_load(monkeypatch, tmp_path):
    rtl = tmp_path / "m.sv"
    rtl.write_text(
        """\
module top_tb;
  m u0();
endmodule

module m;
  logic a, b;
  assign b = a;
endmodule
"""
    )
    _mock_compile(monkeypatch, [rtl])
    backend = StaticConnectivityBackend()
    r = backend.find_loads(signal_path="top_tb.u0.a", compile_log="x")
    assert r["completeness"] == "shallow_only"
    assert any(ld["load_path"] == "top_tb.u0.b" for ld in r["loads"])


def test_select_backend_returns_static_when_no_kdb():
    status = {"simulator": "vcs", "kdb_flow": "none", "kdb_path": None}
    assert select_backend(status).name == "static"


def test_select_backend_can_defer_static_for_source_graph_routing():
    status = {"simulator": "vcs", "kdb_flow": "none", "kdb_path": None}
    deferred = DeferredConnectivityFallbackBackend()

    assert select_backend(status, fallback=deferred) is deferred


def test_explicit_source_graph_route_skips_usable_kdb_before_npi_construction(
    monkeypatch,
):
    monkeypatch.setenv("TRACEWEAVE_CONNECTIVITY_ROUTE", "source_graph")
    status = {
        "simulator": "vcs",
        "kdb_flow": "vcs_two_step",
        "kdb_path": "/some/kdb.elab++",
    }
    deferred = DeferredConnectivityFallbackBackend()

    assert select_backend(status, fallback=deferred) is deferred
    assert status["connectivity_route"] == "source_graph"
    assert "connectivity_route_error" not in status


def test_invalid_connectivity_route_preserves_auto_selection_with_receipt(monkeypatch):
    monkeypatch.setenv("TRACEWEAVE_CONNECTIVITY_ROUTE", "npi-off")
    status = {"simulator": "vcs", "kdb_flow": "none", "kdb_path": None}
    deferred = DeferredConnectivityFallbackBackend()

    assert select_backend(status, fallback=deferred) is deferred
    assert status["connectivity_route"] == "auto"
    assert status["connectivity_route_error"] == ("connectivity_route_config_invalid")


def test_npi_backend_uses_injected_deferred_fallback(monkeypatch):
    status = {
        "simulator": "vcs",
        "kdb_flow": "vcs_two_step",
        "kdb_path": "/some/kdb.elab++",
    }
    deferred = DeferredConnectivityFallbackBackend()

    backend = select_backend(status, fallback=deferred)

    assert backend.name == "verdi_npi"
    assert backend._fallback is deferred


def test_select_backend_returns_npi_when_kdb_present():
    status = {
        "simulator": "vcs",
        "kdb_flow": "vcs_two_step",
        "kdb_path": "/some/kdb.elab++",
    }
    backend = select_backend(status)
    # NPI backend wraps Static internally; if pynpi is unimportable
    # at runtime the NPI backend transparently falls back. Either way
    # the dispatch layer just sees the same protocol.
    assert backend.name in ("verdi_npi", "static")
    if backend.name == "verdi_npi":
        # The NPI backend must hold a Static fallback so it can degrade
        # gracefully on any per-call NPI failure.
        assert isinstance(backend._fallback, StaticConnectivityBackend)


def test_select_backend_returns_lsf_backend_when_opted_in(monkeypatch, tmp_path):
    monkeypatch.setenv("TRACEWEAVE_NPI_EXECUTION", "lsf")
    monkeypatch.setenv("TRACEWEAVE_NPI_LSF_QUEUE", "licensed_q")
    monkeypatch.setenv("TRACEWEAVE_NPI_LSF_STAGING_DIR", str(tmp_path))
    status = {
        "simulator": "vcs",
        "kdb_flow": "vcs_two_step",
        "kdb_path": "/some/kdb.elab++",
    }

    backend = select_backend(status)

    assert backend.name == "verdi_npi"
    assert backend.execution_mode == "lsf"
    assert backend.uses_external_worker is True
    assert not hasattr(backend, "collect_instance_src_map")
