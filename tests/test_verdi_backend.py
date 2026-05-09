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


def test_xcelium_recommends_vericom(tmp_path):
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
