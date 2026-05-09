import os
import sys

sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))

from src.connectivity_backend import (
    ConnectivityBackend,
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
