"""B3: trace_signal_path — backend-level tests.

We test the backend method (``VerdiNpiBackend.find_path`` and
``StaticConnectivityBackend.find_path``) rather than the MCP dispatch
layer; the dispatch wiring is the same shape as find_signal_loads and is
already covered by test_server.py patterns.
"""

import os
import sys

sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))

from src.connectivity_backend import StaticConnectivityBackend
from src.verdi_npi_backend import VerdiNpiBackend


# ---------------------------------------------------------------------------
# Mocks (mirror test_verdi_npi_backend.py style)
# ---------------------------------------------------------------------------


class _MockNetHdl:
    """Mimics a NetHdl with full_name(), scope_inst() and equality."""

    def __init__(self, name, scope_full_name=None,
                 inst_file=None, inst_line=None):
        self._name = name
        if scope_full_name is not None or inst_file is not None or inst_line is not None:
            self._scope = _MockInstHdl(
                full=scope_full_name or name.rsplit(".", 1)[0],
                file_val=inst_file,
                line_val=inst_line,
            )
        else:
            self._scope = None

    def full_name(self):
        return self._name

    def scope_inst(self):
        return self._scope

    def __eq__(self, other):
        return isinstance(other, _MockNetHdl) and self._name == other._name

    def __hash__(self):
        return hash(self._name)


class _MockInstHdl:
    def __init__(self, full, file_val=None, line_val=None):
        self._full = full
        self._file = file_val
        self._line = line_val

    def full_name(self):
        return self._full

    def file(self):
        return self._file

    def begin_line_no(self):
        return self._line


class _MockNetlist:
    def __init__(self, nets, conn_list=None, conn_raises=None):
        self._nets = nets
        self._conn = conn_list
        self._exc = conn_raises

    def get_net(self, name):
        return self._nets.get(name)

    def get_actual_net(self, name):
        return self._nets.get(name)

    def sig_to_sig_conn_list(self, from_hdl, to_hdl, assign_cell=False):
        if self._exc is not None:
            raise self._exc
        # ``assign_cell`` recorded so a test can assert it was passed through.
        self.last_assign_cell = assign_cell
        return list(self._conn or [])


class _MockNpisys:
    def __init__(self):
        self.init_calls = 0
        self.load_calls = []

    def init(self, argv):
        self.init_calls += 1
        return 1

    def load_design(self, argv):
        self.load_calls.append(list(argv))
        return 1


def _make_backend(monkeypatch, *, nets, conn_list=None, conn_raises=None):
    netlist_obj = _MockNetlist(nets, conn_list=conn_list, conn_raises=conn_raises)
    backend = VerdiNpiBackend()
    monkeypatch.setattr(
        "src.verdi_npi_backend._import_pynpi",
        lambda: (_MockNpisys(), netlist_obj),
    )
    return backend, netlist_obj


def _make_compile_log(tmp_path, top="top_tb"):
    log = tmp_path / "comp.log"
    log.write_text(f"Command: vcs -kdb top.sv\nTop Level Modules:\n       {top}\n")
    (tmp_path / "simv.daidir" / "kdb.elab++").mkdir(parents=True)
    return str(log)


# ---------------------------------------------------------------------------
# Static backend: explicit no-op
# ---------------------------------------------------------------------------


def test_static_find_path_returns_structured_unsupported():
    backend = StaticConnectivityBackend()
    r = backend.find_path("top.a", "top.b", compile_log="x")
    assert r["found"] is False
    assert r["path"] == []
    assert r["hops"] == 0
    assert r["unsupported_reason"] == "static_backend_no_path_api"
    assert r["expand_assigns"] is False
    assert r["from_signal"] == "top.a"
    assert r["to_signal"] == "top.b"


def test_static_find_path_propagates_expand_assigns():
    backend = StaticConnectivityBackend()
    r = backend.find_path("a", "b", compile_log="x", expand_assigns=True)
    assert r["expand_assigns"] is True


# ---------------------------------------------------------------------------
# NPI backend: degradation when no KDB
# ---------------------------------------------------------------------------


def test_npi_find_path_no_kdb_falls_back_to_static_unsupported(monkeypatch, tmp_path):
    log = tmp_path / "comp.log"
    log.write_text("Command: vcs top.sv\nTop Level Modules:\n       top_tb\n")
    backend, _ = _make_backend(monkeypatch, nets={})
    r = backend.find_path("top_tb.a", "top_tb.b", compile_log=str(log), simulator="vcs")
    assert r["found"] is False
    assert r["unsupported_reason"] == "static_backend_no_path_api"
    # NPI-level fallback reason is tagged for the dispatch layer to surface.
    assert r["_npi_fallback_reason"] == "kdb_or_top_missing"


# ---------------------------------------------------------------------------
# NPI backend: success and taxonomy
# ---------------------------------------------------------------------------


def test_npi_find_path_from_not_found(monkeypatch, tmp_path):
    log = _make_compile_log(tmp_path)
    backend, _ = _make_backend(
        monkeypatch,
        nets={"top_tb.to": _MockNetHdl("top_tb.to")},
    )
    r = backend.find_path(
        "top_tb.missing", "top_tb.to", compile_log=log, simulator="vcs",
    )
    assert r["found"] is False
    assert r["unsupported_reason"] == "from_not_found"
    assert r["path"] == []


def test_npi_find_path_to_not_found(monkeypatch, tmp_path):
    log = _make_compile_log(tmp_path)
    backend, _ = _make_backend(
        monkeypatch,
        nets={"top_tb.from": _MockNetHdl("top_tb.from")},
    )
    r = backend.find_path(
        "top_tb.from", "top_tb.missing", compile_log=log, simulator="vcs",
    )
    assert r["found"] is False
    assert r["unsupported_reason"] == "to_not_found"


def test_npi_find_path_same_net_returns_one_hop_found(monkeypatch, tmp_path):
    log = _make_compile_log(tmp_path)
    # Both names resolve to the same NetHdl (e.g. via get_actual_net alias).
    shared = _MockNetHdl("top_tb.canonical", scope_full_name="top_tb",
                        inst_file="/p/m.sv", inst_line=5)
    backend, _ = _make_backend(
        monkeypatch,
        nets={"top_tb.alias_a": shared, "top_tb.alias_b": shared},
    )
    r = backend.find_path(
        "top_tb.alias_a", "top_tb.alias_b", compile_log=log, simulator="vcs",
    )
    assert r["found"] is True
    assert r["hops"] == 0
    assert r["unsupported_reason"] is None
    assert len(r["path"]) == 1
    hop = r["path"][0]
    assert hop["index"] == 0
    assert hop["is_endpoint"] is True
    assert hop["net_path"] == "top_tb.canonical"
    assert hop["source_file"] == "/p/m.sv"
    assert hop["source_line"] == 5


def test_npi_find_path_not_connected(monkeypatch, tmp_path):
    log = _make_compile_log(tmp_path)
    src = _MockNetHdl("top_tb.a", scope_full_name="top_tb")
    dst = _MockNetHdl("top_tb.b", scope_full_name="top_tb")
    backend, _ = _make_backend(
        monkeypatch,
        nets={"top_tb.a": src, "top_tb.b": dst},
        conn_list=[],  # NPI returned no path
    )
    r = backend.find_path("top_tb.a", "top_tb.b", compile_log=log, simulator="vcs")
    assert r["found"] is False
    assert r["unsupported_reason"] == "not_connected"


def test_npi_find_path_success_three_hop_chain(monkeypatch, tmp_path):
    log = _make_compile_log(tmp_path)
    src = _MockNetHdl("top.wa", scope_full_name="top",
                     inst_file="/p/top.sv", inst_line=10)
    mid_a = _MockNetHdl("top.m1.a", scope_full_name="top.m1",
                       inst_file="/p/m1.sv", inst_line=20)
    mid_b = _MockNetHdl("top.m1.GEN0_out", scope_full_name="top.m1",
                       inst_file="/p/m1.sv", inst_line=25)
    dst = _MockNetHdl("top.wout", scope_full_name="top",
                     inst_file="/p/top.sv", inst_line=40)
    chain = [src, mid_a, mid_b, dst]
    backend, netlist = _make_backend(
        monkeypatch,
        nets={"top.wa": src, "top.wout": dst},
        conn_list=chain,
    )
    r = backend.find_path("top.wa", "top.wout", compile_log=log, simulator="vcs")
    assert r["found"] is True
    assert r["hops"] == 3
    assert r["unsupported_reason"] is None
    assert [h["index"] for h in r["path"]] == [0, 1, 2, 3]
    assert [h["is_endpoint"] for h in r["path"]] == [True, False, False, True]
    assert r["path"][0]["net_path"] == "top.wa"
    assert r["path"][-1]["net_path"] == "top.wout"
    # Source info pulled from each net's scope_inst.
    assert r["path"][0]["source_file"] == "/p/top.sv"
    assert r["path"][1]["source_line"] == 20
    # assign_cell defaults False
    assert netlist.last_assign_cell is False


def test_npi_find_path_expand_assigns_threaded_through(monkeypatch, tmp_path):
    log = _make_compile_log(tmp_path)
    a = _MockNetHdl("top.a", scope_full_name="top")
    b = _MockNetHdl("top.b", scope_full_name="top")
    backend, netlist = _make_backend(
        monkeypatch, nets={"top.a": a, "top.b": b}, conn_list=[a, b],
    )
    r = backend.find_path(
        "top.a", "top.b", compile_log=log, simulator="vcs",
        expand_assigns=True,
    )
    assert r["expand_assigns"] is True
    assert netlist.last_assign_cell is True


def test_npi_find_path_call_failure_returns_taxonomy(monkeypatch, tmp_path):
    log = _make_compile_log(tmp_path)
    a = _MockNetHdl("top.a", scope_full_name="top")
    b = _MockNetHdl("top.b", scope_full_name="top")
    backend, _ = _make_backend(
        monkeypatch,
        nets={"top.a": a, "top.b": b},
        conn_raises=RuntimeError("npi blew up"),
    )
    r = backend.find_path("top.a", "top.b", compile_log=log, simulator="vcs")
    assert r["found"] is False
    assert r["unsupported_reason"] == "npi_call_failed"
    # The exception detail is preserved for the dispatch layer to strip
    # and surface through backend_status.fallback_reason if useful.
    assert "npi blew up" in r.get("_npi_call_error", "")


# ---------------------------------------------------------------------------
# Schema round-trip
# ---------------------------------------------------------------------------


def test_schema_validation_round_trip_success(monkeypatch, tmp_path):
    from src import schemas
    log = _make_compile_log(tmp_path)
    a = _MockNetHdl("top.a", scope_full_name="top")
    b = _MockNetHdl("top.b", scope_full_name="top")
    backend, _ = _make_backend(
        monkeypatch, nets={"top.a": a, "top.b": b}, conn_list=[a, b],
    )
    r = backend.find_path("top.a", "top.b", compile_log=log, simulator="vcs")
    r.pop("_npi_fallback_reason", None)
    r.pop("_npi_call_error", None)
    r["backend_status"] = {
        "simulator": "vcs",
        "backend": "verdi_npi",
        "actual_backend": "verdi_npi",
        "parser_match": "exact",
        "kdb_path": None,
        "kdb_flow": "vcs_two_step",
        "kdb_hint": None,
    }
    model = schemas.TraceSignalPathResult.model_validate(r)
    assert model.found is True
    assert model.hops == 1
    assert "Connectivity only" in model.direction_note


def test_schema_validation_round_trip_static_unsupported():
    from src import schemas
    backend = StaticConnectivityBackend()
    r = backend.find_path("a", "b", compile_log="x")
    r["backend_status"] = {
        "simulator": "unknown",
        "backend": "static",
        "actual_backend": "static",
        "parser_match": "approximate",
        "kdb_path": None,
        "kdb_flow": "none",
        "kdb_hint": None,
    }
    model = schemas.TraceSignalPathResult.model_validate(r)
    assert model.found is False
    assert model.unsupported_reason == "static_backend_no_path_api"
