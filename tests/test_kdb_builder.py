"""Tests for src.kdb_builder.

Subprocess is mocked so these run in environments without a Verdi
license. The real-Verdi integration test is gated on the same
``requires_verdi`` heuristic used elsewhere.
"""
from __future__ import annotations

import json
import os
import sys
from pathlib import Path
from unittest.mock import patch

import pytest

sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))

from src import kdb_builder
from src.kdb_builder import (
    _extract_build_inputs,
    _extract_plus_args,
    _needs_uvm,
    _pick_top,
    build_kdb,
)


# ---------------------------------------------------------------------------
# Pure-function tests (input extraction, hashing)
# ---------------------------------------------------------------------------


def test_extract_plus_args_handles_packed_tokens():
    cmd = (
        "vcs -sverilog +define+VCS +define+UVM_OBJECT_MUST_HAVE_CONSTRUCTOR "
        "+incdir+/a/b +incdir+/c/d"
    )
    assert _extract_plus_args(cmd, "+define+") == [
        "VCS",
        "UVM_OBJECT_MUST_HAVE_CONSTRUCTOR",
    ]
    assert _extract_plus_args(cmd, "+incdir+") == ["/a/b", "/c/d"]


def test_extract_dash_incdir_xrun_syntax():
    """xrun emits include paths as ``-incdir <path>`` two-token pairs;
    the extractor must accept those alongside VCS's ``+incdir+<path>``."""
    from src.kdb_builder import _extract_dash_pair
    cmd = "xrun -sv -incdir /a/b -incdir /c/d top.sv"
    assert _extract_dash_pair(cmd, "-incdir") == ["/a/b", "/c/d"]


def test_extract_inputs_merges_plus_and_dash_incdirs(tmp_path):
    rtl = tmp_path / "top.sv"
    rtl.write_text("module top; endmodule")
    cr = {
        "top_modules": ["top"],
        "files": {"user": [{"path": str(rtl)}]},
        "compile_command": (
            f"xrun -sv +incdir+/from/plus -incdir /from/dash {rtl}"
        ),
    }
    out = _extract_build_inputs(cr, top_hint=None)
    assert "/from/plus" in out["incdirs"]
    assert "/from/dash" in out["incdirs"]


def test_extract_plus_args_handles_glued_defines():
    cmd = "tool +define+A+define+B=1+define+C foo.sv"
    assert _extract_plus_args(cmd, "+define+") == ["A", "B=1", "C"]


def test_needs_uvm_via_ntb_opts_flag():
    assert _needs_uvm("vcs -sv -ntb_opts uvm top.sv", ["/x/top.sv"]) is True


def test_needs_uvm_via_define():
    assert _needs_uvm("xrun +define+UVM_NO_DPI x.sv", ["/x.sv"]) is True


def test_needs_uvm_via_file_path():
    assert _needs_uvm("vcs top.sv", ["/proj/uvm_pkg.sv"]) is True


def test_needs_uvm_false_when_no_signals():
    assert _needs_uvm("vcs -sv top.sv", ["/proj/foo.sv"]) is False


def test_pick_top_prefers_non_recording_module():
    cr = {
        "top_modules": [
            "uvm_custom_install_recording",
            "uvm_custom_install_verdi_recording",
            "tb_top",
        ]
    }
    assert _pick_top(cr) == "tb_top"


def test_pick_top_falls_back_to_first_when_all_are_recorders():
    cr = {"top_modules": ["uvm_custom_install_recording"]}
    assert _pick_top(cr) == "uvm_custom_install_recording"


def test_pick_top_none_when_empty():
    assert _pick_top({"top_modules": []}) is None


def test_extract_inputs_minimal(tmp_path):
    rtl = tmp_path / "top.sv"
    rtl.write_text("module top; endmodule")
    cr = {
        "top_modules": ["top"],
        "files": {"user": [{"path": str(rtl), "type": "module"}]},
        "compile_command": "vcs -sv " + str(rtl),
    }
    out = _extract_build_inputs(cr, top_hint=None)
    assert out["top"] == "top"
    assert out["files"] == [str(rtl)]
    assert out["needs_uvm"] is False
    assert len(out["hash"]) == 16  # truncated sha256


def test_extract_inputs_dedups_files(tmp_path):
    rtl = tmp_path / "a.sv"
    rtl.write_text("")
    cr = {
        "top_modules": ["top"],
        "files": {"user": [{"path": str(rtl)}, {"path": str(rtl)}]},
        "compile_command": "",
    }
    out = _extract_build_inputs(cr, top_hint=None)
    assert out["files"] == [str(rtl)]


def test_extract_inputs_drops_non_source_paths(tmp_path):
    rtl = tmp_path / "a.sv"
    rtl.write_text("")
    cr = {
        "top_modules": ["top"],
        "files": {
            "user": [
                {"path": str(rtl)},
                {"path": str(tmp_path / "uvm_pkg.f")},
                {"path": str(tmp_path / "junk.txt")},
            ]
        },
        "compile_command": "",
    }
    out = _extract_build_inputs(cr, top_hint=None)
    assert out["files"] == [str(rtl)]


def test_extract_inputs_error_when_no_files(tmp_path):
    cr = {"top_modules": ["top"], "files": {"user": []}, "compile_command": ""}
    assert "error" in _extract_build_inputs(cr, top_hint=None)


def test_extract_inputs_error_when_no_top(tmp_path):
    rtl = tmp_path / "a.sv"
    rtl.write_text("")
    cr = {"top_modules": [], "files": {"user": [{"path": str(rtl)}]}, "compile_command": ""}
    assert "error" in _extract_build_inputs(cr, top_hint=None)


def test_extract_inputs_hash_changes_with_define(tmp_path):
    rtl = tmp_path / "a.sv"
    rtl.write_text("")
    cr1 = {
        "top_modules": ["t"],
        "files": {"user": [{"path": str(rtl)}]},
        "compile_command": "vcs +define+FOO " + str(rtl),
    }
    cr2 = dict(cr1, compile_command="vcs +define+BAR " + str(rtl))
    h1 = _extract_build_inputs(cr1, top_hint=None)["hash"]
    h2 = _extract_build_inputs(cr2, top_hint=None)["hash"]
    assert h1 != h2


# ---------------------------------------------------------------------------
# Subprocess-mocked build flow
# ---------------------------------------------------------------------------


def _make_cr(tmp_path: Path, *, top="tb_top", uvm=False):
    rtl = tmp_path / "rtl.sv"
    rtl.write_text("module tb_top; endmodule")
    cmd = "xrun -sv "
    if uvm:
        cmd += "-ntb_opts uvm "
    cmd += str(rtl)
    return {
        "simulator": "xcelium",
        "top_modules": [top],
        "files": {"user": [{"path": str(rtl), "type": "module"}]},
        "compile_command": cmd,
    }


class _FakeProcess:
    def __init__(self, returncode=0):
        self.returncode = returncode


def _fake_verdi_layout(tmp_root: Path) -> str:
    """Create $VERDI_HOME-shaped tree with executable stubs."""
    vh = tmp_root / "fake_verdi"
    bindir = vh / "bin"
    bindir.mkdir(parents=True)
    for name in ("vericom", "elabcom"):
        p = bindir / name
        p.write_text("#!/bin/sh\nexit 0\n")
        p.chmod(0o755)
    return str(vh)


def _install_fake_run(monkeypatch, *, kdb_after_elabcom: bool, fail_phase: str | None = None):
    """Patch subprocess.run to simulate vericom + elabcom."""
    calls: list[list[str]] = []

    def fake_run(cmd, cwd, stdout, stderr, timeout, check):
        calls.append(list(cmd))
        # Append a marker into the log so _tail returns something useful.
        stdout.write("fake subprocess output\n")
        stdout.flush()
        phase = "vericom" if cmd[0].endswith("vericom") else "elabcom"
        if fail_phase == phase:
            return _FakeProcess(returncode=2)
        if phase == "elabcom" and kdb_after_elabcom:
            (Path(cwd) / "kdb.elab++").mkdir(exist_ok=True)
        return _FakeProcess(returncode=0)

    monkeypatch.setattr(kdb_builder.subprocess, "run", fake_run)
    return calls


def test_build_kdb_happy_path(tmp_path, monkeypatch):
    verdi_home = _fake_verdi_layout(tmp_path)
    cr = _make_cr(tmp_path)
    calls = _install_fake_run(monkeypatch, kdb_after_elabcom=True)

    result = build_kdb(cr, cache_root=tmp_path / "cache", verdi_home=verdi_home)

    assert result["status"] == "rebuilt"
    assert result["rebuilt"] is True
    kdb_path = Path(result["kdb_path"])
    assert kdb_path.is_dir()
    build_sh = Path(result["build_script_path"])
    assert build_sh.is_file()
    # build.sh content reflects what we ran.
    script = build_sh.read_text()
    assert "vericom" in script
    assert "elabcom" in script
    assert "-top 'tb_top'" in script or "-top tb_top" in script
    # vericom got called before elabcom.
    phases = [Path(c[0]).name for c in calls]
    assert phases == ["vericom", "elabcom"]
    state = json.loads((Path(result["cache_dir"]) / "state.json").read_text())
    assert state["status"] == "ok"


def test_build_kdb_cache_hit(tmp_path, monkeypatch):
    verdi_home = _fake_verdi_layout(tmp_path)
    cr = _make_cr(tmp_path)
    cache = tmp_path / "cache"
    _install_fake_run(monkeypatch, kdb_after_elabcom=True)
    first = build_kdb(cr, cache_root=cache, verdi_home=verdi_home)
    assert first["status"] == "rebuilt"

    # Second call with identical inputs must hit cache (no subprocess).
    def explode(*a, **kw):  # would-be subprocess.run
        raise AssertionError("subprocess.run must not be called on cache hit")

    monkeypatch.setattr(kdb_builder.subprocess, "run", explode)
    second = build_kdb(cr, cache_root=cache, verdi_home=verdi_home)
    assert second["status"] == "cached"
    assert second["kdb_path"] == first["kdb_path"]


def test_build_kdb_force_rebuild(tmp_path, monkeypatch):
    verdi_home = _fake_verdi_layout(tmp_path)
    cr = _make_cr(tmp_path)
    cache = tmp_path / "cache"
    _install_fake_run(monkeypatch, kdb_after_elabcom=True)
    build_kdb(cr, cache_root=cache, verdi_home=verdi_home)

    calls = _install_fake_run(monkeypatch, kdb_after_elabcom=True)
    second = build_kdb(cr, cache_root=cache, verdi_home=verdi_home, force_rebuild=True)
    assert second["status"] == "rebuilt"
    assert len(calls) == 2  # vericom + elabcom both re-ran


def test_build_kdb_vericom_failure_keeps_existing_cache(tmp_path, monkeypatch):
    verdi_home = _fake_verdi_layout(tmp_path)
    cr = _make_cr(tmp_path)
    cache = tmp_path / "cache"
    _install_fake_run(monkeypatch, kdb_after_elabcom=True)
    ok = build_kdb(cr, cache_root=cache, verdi_home=verdi_home)
    assert ok["status"] == "rebuilt"
    cached_kdb = Path(ok["kdb_path"])

    # Now force a rebuild where vericom fails. Existing cache must stay.
    _install_fake_run(monkeypatch, kdb_after_elabcom=False, fail_phase="vericom")
    bad = build_kdb(cr, cache_root=cache, verdi_home=verdi_home, force_rebuild=True)
    assert bad["status"] == "failed"
    assert bad["phase"] == "vericom"
    # Original KDB still intact.
    assert cached_kdb.is_dir()


def test_build_kdb_no_verdi_home(tmp_path, monkeypatch):
    monkeypatch.delenv("VERDI_HOME", raising=False)
    cr = _make_cr(tmp_path)
    result = build_kdb(cr, cache_root=tmp_path / "cache", verdi_home=None)
    assert result["status"] == "failed"
    assert result["phase"] == "precheck"


def test_build_kdb_missing_tools(tmp_path, monkeypatch):
    vh = tmp_path / "no_bin"
    (vh / "bin").mkdir(parents=True)
    # No vericom / elabcom stubs created.
    cr = _make_cr(tmp_path)
    result = build_kdb(cr, cache_root=tmp_path / "cache", verdi_home=str(vh))
    assert result["status"] == "failed"
    assert "not found" in result["reason"]


def test_build_kdb_postcheck_when_kdb_missing(tmp_path, monkeypatch):
    """elabcom 'succeeded' but no kdb.elab++ produced — surface postcheck failure."""
    verdi_home = _fake_verdi_layout(tmp_path)
    cr = _make_cr(tmp_path)
    _install_fake_run(monkeypatch, kdb_after_elabcom=False)
    result = build_kdb(cr, cache_root=tmp_path / "cache", verdi_home=verdi_home)
    assert result["status"] == "failed"
    assert result["phase"] == "postcheck"


def test_build_kdb_inputs_with_uvm_emit_ntb_opts(tmp_path, monkeypatch):
    verdi_home = _fake_verdi_layout(tmp_path)
    cr = _make_cr(tmp_path, uvm=True)
    calls = _install_fake_run(monkeypatch, kdb_after_elabcom=True)
    result = build_kdb(cr, cache_root=tmp_path / "cache", verdi_home=verdi_home)
    assert result["status"] == "rebuilt"
    vericom_call = next(c for c in calls if c[0].endswith("vericom"))
    assert "-ntb_opts" in vericom_call
    elabcom_call = next(c for c in calls if c[0].endswith("elabcom"))
    # elabcom warns on -ntb_opts; we deliberately do not pass it.
    assert "-ntb_opts" not in elabcom_call
