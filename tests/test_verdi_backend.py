import os
import sys

import pytest

sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))

from src.verdi_backend import probe_verdi_backend


def _make_compile_result(simulator, *, compile_command=None, top="top_tb", files=None):
    return {
        "simulator": simulator,
        "top_modules": [top],
        "compile_command": compile_command,
        "files": {"user": files or [], "filtered_count": 0},
    }


def test_vcs_two_step_kdb_detected(tmp_path):
    case_dir = tmp_path
    kdb = case_dir / "simv.daidir" / "kdb.elab++"
    kdb.mkdir(parents=True)
    log = case_dir / "comp.log"
    log.write_text("Command: vcs -kdb ...\n")

    cr = _make_compile_result("vcs", compile_command="vcs -kdb foo.sv")
    status = probe_verdi_backend(cr, str(log))
    assert status["kdb_flow"] == "vcs_two_step"
    assert status["kdb_path"] == str(kdb)
    assert status["simulator"] == "vcs"
    assert status["kdb_validation_status"] == "usable"
    assert "available for an NPI attempt" in status["kdb_hint"]
    assert "active" not in status["kdb_hint"]
    assert "actual_backend and attempted_backends" in status["kdb_hint"]


def test_vcs_kdb_with_elaboration_error_marker_is_selected_degraded(tmp_path):
    case_dir = tmp_path
    kdb = case_dir / "simv.daidir" / "kdb.elab++"
    kdb.mkdir(parents=True)
    (kdb / ".hasElabcomError").write_text("elabcomLog/compiler.log\n")
    error_log = kdb / "elabcomLog" / "compiler.log"
    error_log.parent.mkdir()
    error_log.write_text("*Error* missing view\nTotal   8 error(s), 0 warning(s)\n")
    log = case_dir / "comp.log"
    log.write_text("Command: vcs -kdb top.sv\n")

    cr = _make_compile_result("vcs", compile_command="vcs -kdb top.sv")
    status = probe_verdi_backend(cr, str(log))

    assert status["kdb_flow"] == "vcs_two_step"
    assert status["kdb_path"] == str(kdb)
    assert status["kdb_validation_status"] == "elaboration_error"
    assert status["kdb_error_count"] == 8
    assert status["kdb_error_log"] == str(error_log)
    assert "degraded partial-netlist" in status["kdb_hint"]
    assert "if selected by routing policy" in status["kdb_hint"]
    assert "will attempt" not in status["kdb_hint"]


def test_vcs_degraded_kdb_escape_hatch_restores_rejection(
    tmp_path,
    monkeypatch,
):
    import config

    monkeypatch.setattr(config, "NPI_ALLOW_DEGRADED_KDB", False)
    kdb = tmp_path / "simv.daidir" / "kdb.elab++"
    kdb.mkdir(parents=True)
    (kdb / ".hasElabcomError").write_text("missing.log\n")
    log = tmp_path / "comp.log"
    log.write_text("Command: vcs -kdb top.sv\n")

    status = probe_verdi_backend(
        _make_compile_result("vcs", compile_command="vcs -kdb top.sv"),
        str(log),
    )

    assert status["kdb_path"] is None
    assert status["kdb_flow"] == "none"
    assert status["kdb_validation_status"] == "elaboration_error"
    assert status["_npi_selection_reason"] == "npi_degraded_kdb_disabled"
    assert "will use" not in status["kdb_hint"]


def test_vcs_clean_three_step_kdb_is_preferred_over_degraded_two_step(tmp_path):
    degraded = tmp_path / "simv.daidir" / "kdb.elab++"
    degraded.mkdir(parents=True)
    (degraded / ".hasElabcomError").write_text("missing.log\n")
    work = tmp_path / "AN.DB"
    work.mkdir()
    clean = work / "work.lib++"
    clean.mkdir()
    (tmp_path / "synopsys_sim.setup").write_text("WORK : ./AN.DB\n")
    log = tmp_path / "comp.log"
    log.write_text("Command: vcs -kdb top.sv\n")

    status = probe_verdi_backend(_make_compile_result("vcs"), str(log))

    assert status["kdb_path"] == str(clean)
    assert status["kdb_flow"] == "vcs_three_step"
    assert status["kdb_validation_status"] == "usable"


def test_vcs_three_step_kdb_via_synopsys_setup(tmp_path):
    case_dir = tmp_path
    log = case_dir / "comp.log"
    log.write_text("dummy\n")
    work = case_dir / "AN.DB"
    work.mkdir()
    (work / "work.lib++").mkdir()
    (case_dir / "synopsys_sim.setup").write_text(
        "WORK : ./AN.DB\n-- comment\n"
    )
    cr = _make_compile_result("vcs")
    status = probe_verdi_backend(cr, str(log))
    assert status["kdb_flow"] == "vcs_three_step"
    assert status["kdb_path"].endswith("work.lib++")


def test_vcs_kdb_missing_recommends_kdb_only(tmp_path):
    case_dir = tmp_path
    log = case_dir / "comp.log"
    log.write_text("dummy\n")
    cmd = "vcs +define+FOO -sverilog top.sv -o simv"
    cr = _make_compile_result("vcs", compile_command=cmd)
    status = probe_verdi_backend(cr, str(log))
    assert status["kdb_flow"] == "none"
    assert status["kdb_path"] is None
    assert "-kdb=only" in status["kdb_hint"]
    # The hint should embed the user's original command verbatim.
    assert "vcs +define+FOO -sverilog top.sv" in status["kdb_hint"]


def test_xcelium_recommends_build_kdb_when_auto_enabled(tmp_path, monkeypatch):
    """When AUTO_KDB_BUILD is on (default), the Xcelium hint points at
    the build_kdb tool instead of dumping the raw vericom command."""
    monkeypatch.setattr("src.verdi_backend.os.environ", {**os.environ})
    import config
    monkeypatch.setattr(config, "AUTO_KDB_BUILD", True, raising=False)

    case_dir = tmp_path
    log = case_dir / "xrun.log"
    log.write_text("dummy\n")
    src = case_dir / "top.sv"
    src.write_text("module top; endmodule\n")
    cr = _make_compile_result(
        "xcelium",
        files=[{"path": str(src), "type": "module", "category": "rtl"}],
    )
    status = probe_verdi_backend(cr, str(log))
    assert status["kdb_flow"] == "none"
    assert status["simulator"] == "xcelium"
    assert "build_kdb" in status["kdb_hint"]


def test_xcelium_recommends_manual_vericom_when_auto_disabled(tmp_path, monkeypatch):
    """With AUTO_KDB_BUILD off, fall back to the explicit vericom command hint."""
    import config
    monkeypatch.setattr(config, "AUTO_KDB_BUILD", False, raising=False)

    case_dir = tmp_path
    log = case_dir / "xrun.log"
    log.write_text("dummy\n")
    src = case_dir / "top.sv"
    src.write_text("module top; endmodule\n")
    cr = _make_compile_result(
        "xcelium",
        files=[{"path": str(src), "type": "module", "category": "rtl"}],
    )
    status = probe_verdi_backend(cr, str(log))
    assert "vericom -kdb" in status["kdb_hint"]
    assert str(src) in status["kdb_hint"]
    assert "-top top_tb" in status["kdb_hint"]


def test_xcelium_picks_up_existing_vericom_libpp(tmp_path):
    case_dir = tmp_path
    log = case_dir / "xrun.log"
    log.write_text("dummy\n")
    (case_dir / "rtl.lib++").mkdir()
    cr = _make_compile_result("xcelium")
    status = probe_verdi_backend(cr, str(log))
    assert status["kdb_flow"] == "vericom_standalone"
    assert status["kdb_path"].endswith("rtl.lib++")


def test_unknown_simulator_falls_back_with_generic_hint(tmp_path):
    case_dir = tmp_path
    log = case_dir / "any.log"
    log.write_text("dummy\n")
    cr = _make_compile_result("unknown")
    status = probe_verdi_backend(cr, str(log))
    assert status["simulator"] == "unknown"
    assert status["kdb_flow"] == "none"
    assert "vericom" in status["kdb_hint"] or "kdb=only" in status["kdb_hint"]


def test_env_warnings_present_when_missing(tmp_path, monkeypatch):
    monkeypatch.delenv("VERDI_HOME", raising=False)
    monkeypatch.delenv("SNPSLMD_LICENSE_FILE", raising=False)
    monkeypatch.delenv("LM_LICENSE_FILE", raising=False)
    log = tmp_path / "comp.log"
    log.write_text("dummy\n")
    cr = _make_compile_result("vcs", compile_command="vcs top.sv")
    status = probe_verdi_backend(cr, str(log))
    assert "VERDI_HOME" in status["kdb_hint"]
    assert "LICENSE" in status["kdb_hint"]


def test_real_cc20_case_when_available():
    """When the cc20 fixture case is present locally, probe should succeed."""
    cc20_log = "/home/robin/Projects/mcp_practise/uvm_demo_cc20/tb/comp.log"
    if not os.path.exists(cc20_log):
        pytest.skip("cc20 fixture unavailable")
    from src.compile_log_parser import parse_compile_log

    cr = parse_compile_log(cc20_log, "vcs")
    status = probe_verdi_backend(cr, cc20_log)
    assert status["simulator"] == "vcs"
    # KDB may or may not exist; just sanity-check the shape.
    assert status["kdb_flow"] in ("vcs_two_step", "vcs_three_step", "none")


@pytest.mark.parametrize('actual,route,npi_status,reason,execution', [
    ('source_graph', 'source_graph', 'skipped', 'npi_skipped_by_policy', None),
    ('verdi_npi', 'auto', 'found', None, {'execution_mode': 'local'}),
    ('source_graph', 'auto', 'failed', 'npi_worker_failed', None),
    ('static', 'auto', 'failed', 'npi_worker_failed', None),
    ('source_graph', 'auto', 'failed', 'npi_scheduler_timeout',
     {'execution_mode': 'lsf', 'scheduler_status': 'timeout', 'worker_status': 'not_started'}),
])
def test_resource_hint_cannot_claim_execution(tmp_path, actual, route, npi_status, reason, execution):
    import server
    kdb = tmp_path / 'simv.daidir' / 'kdb.elab++'
    kdb.mkdir(parents=True)
    log = tmp_path / 'comp.log'
    log.write_text('vcs -kdb top.sv\n')
    probe = probe_verdi_backend(_make_compile_result('vcs'), str(log))
    probe['connectivity_route'] = route
    attempts = [dict(backend='verdi_npi', status=npi_status, reason=reason)]
    status = server._finalize_public_connectivity_status(
        backend_status=probe, selected_backend='verdi_npi', actual_backend=actual,
        attempts=attempts, fallback_reason=reason, npi_backend=None,
        npi_execution=execution, source_graph_receipt=None)
    assert status['kdb_hint'] == probe['kdb_hint']
    assert 'active' not in status['kdb_hint'] and 'will attempt' not in status['kdb_hint']
    assert status['actual_backend'] == actual
    assert status['attempted_backends'] == attempts
    if reason:
        assert status['fallback_reason'] == reason
    if execution:
        assert all(status[k] == v for k, v in execution.items())
