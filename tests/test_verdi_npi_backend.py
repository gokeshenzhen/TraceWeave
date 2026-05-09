import os
import sys
import types

import pytest

sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))

from src.verdi_npi_backend import (
    VerdiNpiBackend,
    _classify_driver_kind,
    _classify_load_kind,
    _driver_summary,
    _line_from_synthesized,
    _scope_from_synthesized,
)


# ---------------------------------------------------------------------------
# Pure helpers
# ---------------------------------------------------------------------------


def test_scope_from_synthesized_strips_at_first_colon():
    assert _scope_from_synthesized(
        "top_tb.my_dut.dut:Always0#Always0:18:31:Mux.CH_invert"
    ) == "top_tb.my_dut.dut"


def test_scope_passes_through_non_synthesized():
    assert _scope_from_synthesized("top_tb.b_if.clk") == "top_tb.b_if.clk"


def test_line_from_synthesized_extracts_first_int():
    assert _line_from_synthesized(
        "top_tb.my_dut.dut:Always7#Always1:33:45:Mux.IH_invert"
    ) == 33


def test_line_from_synthesized_returns_none_for_non_synth():
    assert _line_from_synthesized("top_tb.b_if.clk") is None


def test_classify_load_kind_module_input_vs_rhs():
    assert _classify_load_kind("top_tb.b_if.clk", "npiNlInstPort") == "module_input"
    assert _classify_load_kind(
        "top_tb.my_dut.dut:Always0#Always0:18:31:Mux.CH_invert",
        "npiNlInstPort",
    ) == "rhs_expr"


# ---------------------------------------------------------------------------
# Mocked NPI backend
# ---------------------------------------------------------------------------


class _MockPin:
    def __init__(self, name, t="npiNlInstPort"):
        self._name = name
        self._t = t

    def full_name(self):
        return self._name

    def type(self):
        return self._t

    def src_info(self):
        return {}


class _MockNet:
    def __init__(self, loads=None, drivers=None):
        self._loads = loads or []
        self._drivers = drivers or []

    def load_list(self):
        return self._loads

    def driver_list(self):
        return self._drivers


class _MockNetlist:
    def __init__(self, net_map):
        self._net_map = net_map

    def get_net(self, name):
        return self._net_map.get(name)

    def get_actual_net(self, name):
        return self._net_map.get(name)


class _MockNpisys:
    def __init__(self, init_rc=1, load_rc=1):
        self.init_rc = init_rc
        self.load_rc = load_rc
        self.init_calls = 0
        self.load_calls: list[list[str]] = []

    def init(self, argv):
        self.init_calls += 1
        return self.init_rc

    def load_design(self, argv):
        self.load_calls.append(list(argv))
        return self.load_rc


def _make_backend_with_mock_npi(monkeypatch, *, npisys=None, netlist_obj=None):
    npisys = npisys or _MockNpisys()
    netlist_obj = netlist_obj or _MockNetlist({})
    backend = VerdiNpiBackend()

    def fake_import():
        return (npisys, netlist_obj)

    monkeypatch.setattr("src.verdi_npi_backend._import_pynpi", fake_import)
    return backend, npisys, netlist_obj


def _make_compile_log(tmp_path, top="top_tb"):
    log = tmp_path / "comp.log"
    log.write_text(f"Command: vcs -kdb top.sv\nTop Level Modules:\n       {top}\n")
    # KDB is detected by probe via simv.daidir/kdb.elab++
    (tmp_path / "simv.daidir" / "kdb.elab++").mkdir(parents=True)
    return str(log)


def test_backend_falls_back_when_pynpi_unavailable(monkeypatch, tmp_path):
    log = _make_compile_log(tmp_path)
    backend = VerdiNpiBackend()
    monkeypatch.setattr("src.verdi_npi_backend._import_pynpi", lambda: None)
    r = backend.find_loads(signal_path="top_tb.x", compile_log=log, simulator="vcs")
    # Static shape — completeness should be shallow_only.
    assert r["completeness"] == "shallow_only"


def test_backend_falls_back_when_load_design_fails(monkeypatch, tmp_path):
    log = _make_compile_log(tmp_path)
    npisys = _MockNpisys(load_rc=0)
    backend, _, _ = _make_backend_with_mock_npi(
        monkeypatch, npisys=npisys, netlist_obj=_MockNetlist({})
    )
    r = backend.find_loads(signal_path="top_tb.x", compile_log=log, simulator="vcs")
    assert r["completeness"] == "shallow_only"
    assert npisys.load_calls, "load_design should have been attempted"


def test_backend_returns_exact_loads_via_npi(monkeypatch, tmp_path):
    log = _make_compile_log(tmp_path)
    net = _MockNet([
        _MockPin("top_tb.b_if.clk"),
        _MockPin("top_tb.my_dut.dut:Always0#Always0:18:31:Mux.CH_invert"),
    ])
    netlist_obj = _MockNetlist({"top_tb.clk": net})
    backend, _, _ = _make_backend_with_mock_npi(
        monkeypatch, netlist_obj=netlist_obj
    )
    r = backend.find_loads(signal_path="top_tb.clk", compile_log=log, simulator="vcs")
    assert r["completeness"] == "exact"
    assert len(r["loads"]) == 2
    kinds = sorted(ld["kind"] for ld in r["loads"])
    assert kinds == ["module_input", "rhs_expr"]
    rhs = next(ld for ld in r["loads"] if ld["kind"] == "rhs_expr")
    assert rhs["load_path"] == "top_tb.my_dut.dut"
    assert rhs["source_line"] == 18
    assert rhs["backend"] == "verdi_npi"
    assert rhs["confidence"] == "exact"
    # Internal field stripped before return.
    assert "_npi_raw" not in rhs


def test_backend_dedup_keeps_distinct_synthesized_loads(monkeypatch, tmp_path):
    log = _make_compile_log(tmp_path)
    # Two muxes in the same module reading the same signal — same scope,
    # different cells. Static dedup that keyed only on (scope,kind) would
    # collapse these incorrectly.
    net = _MockNet([
        _MockPin("top_tb.dut:Always0#Always0:18:31:Mux.CH_a"),
        _MockPin("top_tb.dut:Always7#Always1:33:45:Mux.CH_a"),
    ])
    netlist_obj = _MockNetlist({"top_tb.dut.a": net})
    backend, _, _ = _make_backend_with_mock_npi(
        monkeypatch, netlist_obj=netlist_obj
    )
    r = backend.find_loads(signal_path="top_tb.dut.a", compile_log=log, simulator="vcs")
    assert len(r["loads"]) == 2


def test_backend_caches_loaded_kdb(monkeypatch, tmp_path):
    log = _make_compile_log(tmp_path)
    npisys = _MockNpisys()
    netlist_obj = _MockNetlist({"top_tb.x": _MockNet([_MockPin("top_tb.b.x")])})
    backend, _, _ = _make_backend_with_mock_npi(
        monkeypatch, npisys=npisys, netlist_obj=netlist_obj
    )
    backend.find_loads(signal_path="top_tb.x", compile_log=log, simulator="vcs")
    backend.find_loads(signal_path="top_tb.x", compile_log=log, simulator="vcs")
    assert len(npisys.load_calls) == 1, "second call should hit the cache"


def test_backend_no_kdb_falls_back(monkeypatch, tmp_path):
    log = tmp_path / "comp.log"
    log.write_text("Command: vcs top.sv\nTop Level Modules:\n       top_tb\n")
    backend = VerdiNpiBackend()
    # Even if pynpi is available, no KDB → fallback path.
    npisys = _MockNpisys()
    monkeypatch.setattr(
        "src.verdi_npi_backend._import_pynpi",
        lambda: (npisys, _MockNetlist({})),
    )
    r = backend.find_loads(
        signal_path="top_tb.x", compile_log=str(log), simulator="vcs"
    )
    assert r["completeness"] == "shallow_only"
    assert npisys.load_calls == []


def test_backend_get_net_none_falls_back(monkeypatch, tmp_path):
    log = _make_compile_log(tmp_path)
    netlist_obj = _MockNetlist({})  # signal not found
    backend, _, _ = _make_backend_with_mock_npi(
        monkeypatch, netlist_obj=netlist_obj
    )
    r = backend.find_loads(
        signal_path="top_tb.ghost", compile_log=log, simulator="vcs"
    )
    assert r["completeness"] == "exact"
    assert r["loads"] == []
    assert r["stopped_at"] == "signal_path_unresolved_in_npi"


def test_backend_load_list_exception_returns_stopped(monkeypatch, tmp_path):
    log = _make_compile_log(tmp_path)
    class _BoomNet:
        def load_list(self):
            raise RuntimeError("boom")
    netlist_obj = _MockNetlist({"top_tb.x": _BoomNet()})
    backend, _, _ = _make_backend_with_mock_npi(
        monkeypatch, netlist_obj=netlist_obj
    )
    r = backend.find_loads(
        signal_path="top_tb.x", compile_log=log, simulator="vcs"
    )
    assert r["stopped_at"] == "npi_load_list_failed"


def test_backend_kind_filter_passed_through(monkeypatch, tmp_path):
    log = _make_compile_log(tmp_path)
    net = _MockNet([
        _MockPin("top_tb.b_if.clk"),
        _MockPin("top_tb.my_dut.dut:Always0#Always0:18:31:Mux.CH_clk"),
    ])
    netlist_obj = _MockNetlist({"top_tb.clk": net})
    backend, _, _ = _make_backend_with_mock_npi(
        monkeypatch, netlist_obj=netlist_obj
    )
    r = backend.find_loads(
        signal_path="top_tb.clk",
        compile_log=log,
        simulator="vcs",
        kind_filter=["module_input"],
    )
    assert all(ld["kind"] == "module_input" for ld in r["loads"])
    assert len(r["loads"]) == 1


# ---------------------------------------------------------------------------
# Integration tests (real Verdi)
# ---------------------------------------------------------------------------


_CC20_LOG = "/home/robin/Projects/mcp_practise/uvm_demo_cc20/tb/comp.log"


def _verdi_available() -> bool:
    if not os.environ.get("VERDI_HOME"):
        return False
    if not os.path.exists(_CC20_LOG):
        return False
    try:
        from src.verdi_npi_backend import _import_pynpi
        return _import_pynpi() is not None
    except Exception:
        return False


requires_verdi = pytest.mark.skipif(
    not _verdi_available(),
    reason="VERDI_HOME unset, cc20 case missing, or pynpi unimportable",
)


@requires_verdi
def test_real_npi_returns_more_loads_than_static_for_clk():
    from src.connectivity_backend import (
        StaticConnectivityBackend,
        select_backend,
    )
    from src.verdi_backend import probe_verdi_backend
    from src.compile_log_parser import parse_compile_log

    cr = parse_compile_log(_CC20_LOG, "vcs")
    status = probe_verdi_backend(cr, _CC20_LOG)
    assert status["kdb_flow"] == "vcs_two_step"

    npi_backend = select_backend(status)
    assert npi_backend.name == "verdi_npi"
    npi_r = npi_backend.find_loads(
        signal_path="top_tb.clk", compile_log=_CC20_LOG, simulator="vcs",
    )
    assert npi_r["completeness"] == "exact"
    npi_paths = {ld["load_path"] for ld in npi_r["loads"]}

    static_r = StaticConnectivityBackend().find_loads(
        signal_path="top_tb.clk", compile_log=_CC20_LOG, simulator="vcs",
    )
    static_paths = {ld["load_path"] for ld in static_r["loads"]}

    # NPI must catch the interface-positional connections that static
    # cannot see (b_if/input_if/output_if).
    assert "top_tb.b_if.clk" in npi_paths
    assert "top_tb.input_if.clk" in npi_paths
    assert "top_tb.output_if.clk" in npi_paths
    assert len(npi_paths) > len(static_paths)


# ---------------------------------------------------------------------------
# Driver-side classifier helpers
# ---------------------------------------------------------------------------


def test_classify_driver_kind_initial():
    assert _classify_driver_kind(
        "top_tb.top_tb(@1):Init0#Init0:53:58:Init.OH_clk"
    ) == "initial"


def test_classify_driver_kind_always_ff():
    assert _classify_driver_kind(
        "top_tb.my_dut.dut:Always10#Always1:33:45:Reg.ROH_invert"
    ) == "always_ff"


def test_classify_driver_kind_combinational():
    assert _classify_driver_kind(
        "top_tb.dut:Always0#Always0:18:31:Mux.OH_y"
    ) == "always_comb"


def test_classify_driver_kind_assignment():
    assert _classify_driver_kind(
        "top_tb.dut:Cont0#Cont0:42:42:Assignment.OH_z"
    ) == "assign"


def test_classify_driver_kind_unknown_falls_through():
    assert _classify_driver_kind(
        "top_tb.dut:Strange0#Weird0:1:1:Mystery.OH_x"
    ) == "unknown"


def test_classify_driver_kind_instance_port_when_no_synth():
    assert _classify_driver_kind("top_tb.b_if.clk") == "instance_port"


def test_driver_summary_includes_line():
    summary = _driver_summary(
        "top_tb.top_tb(@1):Init0#Init0:53:58:Init.OH_clk", "initial"
    )
    assert "line 53" in summary
    assert "initial driver" in summary


# ---------------------------------------------------------------------------
# Mocked NPI driver path
# ---------------------------------------------------------------------------


def test_driver_recursive_falls_through_to_static(monkeypatch, tmp_path):
    log = _make_compile_log(tmp_path)
    backend, _, _ = _make_backend_with_mock_npi(monkeypatch)
    # We do not need a useful net for this — recursive must skip NPI entirely.
    r = backend.find_driver(
        signal_path="top_tb.x",
        wave_path="dummy.fsdb",
        compile_log=log,
        recursive=True,
        simulator="vcs",
    )
    # Static recursive path uses ExplainDriverResult shape with recursive=True.
    assert r["recursive"] is True
    # backend defaulted to static via _strip_chain_hop / _strip_internal_fields.
    assert r.get("backend", "static") == "static"


def test_driver_single_hop_npi_resolves_initial_kind(monkeypatch, tmp_path):
    log = _make_compile_log(tmp_path)
    init_pin = _MockPin(
        "top_tb.top_tb(@1):Init0#Init0:53:58:Init.OH_clk"
    )
    net = _MockNet(drivers=[init_pin])
    netlist_obj = _MockNetlist({"top_tb.clk": net})
    backend, _, _ = _make_backend_with_mock_npi(
        monkeypatch, netlist_obj=netlist_obj
    )
    r = backend.find_driver(
        signal_path="top_tb.clk",
        wave_path="dummy.fsdb",
        compile_log=log,
        recursive=False,
        simulator="vcs",
    )
    assert r["backend"] == "verdi_npi"
    assert r["driver_status"] == "resolved"
    assert r["driver_kind"] == "initial"
    assert r["source_line"] == 53
    assert "initial driver" in r["expression_summary"]
    assert r["recursive"] is False
    assert r["driver_chain"] is None  # single driver — no chain


def test_driver_multi_driven_net_lists_chain(monkeypatch, tmp_path):
    log = _make_compile_log(tmp_path)
    pins = [
        _MockPin("top_tb.dut:Always0#Always0:18:31:Reg.ROH_v"),
        _MockPin("top_tb.dut:Always7#Always1:33:45:Reg.ROH_v"),
    ]
    net = _MockNet(drivers=pins)
    netlist_obj = _MockNetlist({"top_tb.dut.v": net})
    backend, _, _ = _make_backend_with_mock_npi(
        monkeypatch, netlist_obj=netlist_obj
    )
    r = backend.find_driver(
        signal_path="top_tb.dut.v",
        wave_path="x.fsdb",
        compile_log=log,
        simulator="vcs",
    )
    assert r["driver_chain"] is not None
    assert len(r["driver_chain"]) == 2
    assert all(h["backend"] == "verdi_npi" for h in r["driver_chain"])
    assert "multi-driven" in r["chain_summary"]


def test_driver_no_drivers_marks_stopped(monkeypatch, tmp_path):
    log = _make_compile_log(tmp_path)
    net = _MockNet(drivers=[])  # no drivers
    netlist_obj = _MockNetlist({"top_tb.float": net})
    backend, _, _ = _make_backend_with_mock_npi(
        monkeypatch, netlist_obj=netlist_obj
    )
    r = backend.find_driver(
        signal_path="top_tb.float",
        wave_path="x.fsdb",
        compile_log=log,
        simulator="vcs",
    )
    assert r["driver_status"] == "unsupported"
    assert r["stopped_at"] == "no_npi_drivers"
    assert r["backend"] == "verdi_npi"


def test_driver_unresolved_signal_falls_through(monkeypatch, tmp_path):
    log = _make_compile_log(tmp_path)
    netlist_obj = _MockNetlist({})  # nothing
    backend, _, _ = _make_backend_with_mock_npi(
        monkeypatch, netlist_obj=netlist_obj
    )
    r = backend.find_driver(
        signal_path="top_tb.ghost",
        wave_path="x.fsdb",
        compile_log=log,
        simulator="vcs",
    )
    assert r["stopped_at"] == "signal_path_unresolved_in_npi"


def test_driver_list_exception_marks_failed(monkeypatch, tmp_path):
    log = _make_compile_log(tmp_path)
    class _BoomNet:
        def driver_list(self):
            raise RuntimeError("boom")
    netlist_obj = _MockNetlist({"top_tb.x": _BoomNet()})
    backend, _, _ = _make_backend_with_mock_npi(
        monkeypatch, netlist_obj=netlist_obj
    )
    r = backend.find_driver(
        signal_path="top_tb.x",
        wave_path="x.fsdb",
        compile_log=log,
        simulator="vcs",
    )
    assert r["stopped_at"] == "npi_driver_list_failed"


# ---------------------------------------------------------------------------
# Real-verdi driver integration
# ---------------------------------------------------------------------------


@requires_verdi
def test_real_npi_driver_resolves_clk_initial_block():
    from src.connectivity_backend import select_backend
    from src.verdi_backend import probe_verdi_backend
    from src.compile_log_parser import parse_compile_log

    cr = parse_compile_log(_CC20_LOG, "vcs")
    status = probe_verdi_backend(cr, _CC20_LOG)
    backend = select_backend(status)
    r = backend.find_driver(
        signal_path="top_tb.clk",
        wave_path="x.fsdb",
        compile_log=_CC20_LOG,
        simulator="vcs",
    )
    assert r["backend"] == "verdi_npi"
    assert r["driver_status"] == "resolved"
    assert r["driver_kind"] == "initial"
    assert r["source_line"] == 53


@requires_verdi
def test_real_npi_driver_invert_recognised_as_register():
    from src.connectivity_backend import select_backend
    from src.verdi_backend import probe_verdi_backend
    from src.compile_log_parser import parse_compile_log

    cr = parse_compile_log(_CC20_LOG, "vcs")
    status = probe_verdi_backend(cr, _CC20_LOG)
    backend = select_backend(status)
    r = backend.find_driver(
        signal_path="top_tb.my_dut.invert",
        wave_path="x.fsdb",
        compile_log=_CC20_LOG,
        simulator="vcs",
    )
    assert r["backend"] == "verdi_npi"
    assert r["driver_kind"] == "always_ff"
    assert r["source_line"] is not None


@requires_verdi
def test_real_npi_driver_recursive_falls_back_to_static():
    from src.connectivity_backend import select_backend
    from src.verdi_backend import probe_verdi_backend
    from src.compile_log_parser import parse_compile_log

    cr = parse_compile_log(_CC20_LOG, "vcs")
    status = probe_verdi_backend(cr, _CC20_LOG)
    backend = select_backend(status)
    r = backend.find_driver(
        signal_path="top_tb.my_dut.invert",
        wave_path="x.fsdb",
        compile_log=_CC20_LOG,
        simulator="vcs",
        recursive=True,
        max_depth=4,
    )
    # Recursive path goes through Static, which stamps backend=static.
    assert r["recursive"] is True
    assert r.get("backend") == "static"


@requires_verdi
def test_real_npi_normalizes_synthesized_paths_for_invert():
    from src.connectivity_backend import select_backend
    from src.verdi_backend import probe_verdi_backend
    from src.compile_log_parser import parse_compile_log

    cr = parse_compile_log(_CC20_LOG, "vcs")
    status = probe_verdi_backend(cr, _CC20_LOG)
    backend = select_backend(status)
    r = backend.find_loads(
        signal_path="top_tb.my_dut.invert",
        compile_log=_CC20_LOG,
        simulator="vcs",
    )
    assert r["completeness"] == "exact"
    assert r["loads"], "expected NPI to find load(s) for invert"
    for ld in r["loads"]:
        # Scope is FSDB-paste-able; raw stays in expr.
        assert ":" not in ld["load_path"]
        assert ld["expr"] is not None
        assert ld["backend"] == "verdi_npi"
        assert ld["confidence"] == "exact"
        # Synthesized loads should produce a parsed source_line.
        if ":" in ld["expr"]:
            assert ld["source_line"] is not None
