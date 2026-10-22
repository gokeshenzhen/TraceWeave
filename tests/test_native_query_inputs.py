import os
from dataclasses import replace

import pytest

from src.compile_input_capture import CaptureAborted
from src.native_query import NativeInputChanged, RequestBudget, read_native_inputs
from src.native_query_codec import decode_native_inputs, encode_native_inputs
from tests.native_first_helpers import project


@pytest.mark.parametrize("change", ["content", "delete", "symlink"])
@pytest.mark.parametrize("simulator", ["vcs", "verilator"])
def test_metadata_observation_does_not_validate_declared_source(tmp_path, change, simulator):
    source, log, kdb = project(tmp_path)
    if simulator == "verilator":
        log.write_text(f"Command: verilator --cc {source} --top-module top\n")
    inputs = read_native_inputs({"compile_log": str(log)})
    assert {r.path for r in inputs.metadata_snapshot.records} == {str(log)}
    assert str(source) not in dict(inputs.aliases)
    if change == "content":
        source.write_text("changed source")
    elif change == "delete":
        source.unlink()
    else:
        source.unlink()
        source.symlink_to(tmp_path / "absent.sv")
    assert inputs.current()
    restored = decode_native_inputs(encode_native_inputs(inputs))
    assert restored == inputs and restored.current()


@pytest.mark.parametrize("flag", ["-f", "-F"])
@pytest.mark.parametrize("nested", [False, True])
def test_verilator_missing_filelist_appearance_invalidates_metadata(tmp_path, flag, nested):
    source, log, _ = project(tmp_path)
    missing = tmp_path / "missing.f"
    operand = missing
    if nested:
        operand = tmp_path / "outer.f"
        operand.write_text(f"{flag} {missing}\n")
    log.write_text(f"Command: verilator --cc {flag} {operand} --top-module top\n")
    inputs = read_native_inputs({"compile_log": str(log)})
    assert inputs.compile_result()["compile_evidence"]["ordered_compilation_units"] == []
    assert inputs.current()

    missing.write_text(f"{source}\n")
    assert not inputs.current()
    assert not decode_native_inputs(encode_native_inputs(inputs)).current()
    with pytest.raises(NativeInputChanged, match="compile_metadata_changed"):
        inputs.require_current()
    assert str(missing) in dict(inputs.unavailable)
    assert not inputs.identity_complete
    assert str(source) not in {r.path for r in inputs.metadata_snapshot.records}
    refreshed = read_native_inputs({"compile_log": str(log)})
    assert [unit["path"] for unit in refreshed.compile_result()["compile_evidence"][
        "ordered_compilation_units"]] == [str(source)]
    assert refreshed.compile_metadata_identity_sha256 != inputs.compile_metadata_identity_sha256


@pytest.mark.parametrize("change", ["log", "filelist", "env", "alias", "missing_filelist"])
def test_consumed_metadata_changes_are_terminal(tmp_path, monkeypatch, change):
    source, log, _ = project(tmp_path)
    filelist = tmp_path / "inputs.f"
    filelist.write_text(str(source))
    monkeypatch.setenv("TW_NATIVE_TEST_ROOT", str(tmp_path))
    log.write_text(f"Command: vcs -sverilog -f $TW_NATIVE_TEST_ROOT/inputs.f -top top\n"
                  f"Parsing design file '{source}'\nTop Level Modules:\n top\n")
    if change == "missing_filelist":
        filelist.unlink()
    if change == "alias":
        alias = tmp_path / "alias.log"
        alias.symlink_to(log)
        selected_log = alias
    else:
        selected_log = log
    inputs = read_native_inputs({"compile_log": str(selected_log)})
    assert inputs.current()
    if change == "log":
        before = log.stat()
        log.write_text(log.read_text().replace("-top top", "-top two"))
        os.utime(log, ns=(before.st_atime_ns, before.st_mtime_ns))
    elif change == "filelist":
        filelist.write_text("new.sv")
    elif change == "env":
        monkeypatch.setenv("TW_NATIVE_TEST_ROOT", "different")
    elif change == "alias":
        other = tmp_path / "other.log"
        other.write_text(log.read_text())
        selected_log.unlink()
        selected_log.symlink_to(other)
    else:
        filelist.write_text(str(source))
    assert not inputs.current()
    with pytest.raises(NativeInputChanged):
        inputs.require_current()


@pytest.mark.parametrize("arguments", [
    {}, {"kdb_path": "/kdb"}, {"top_hint": "top"}, {"compile_log": ""},
    {"compile_log": "/log", "retry_npi": 1}, {"compile_log": "/log", "simulator": "unknown"},
    {"compile_log": "/log", "supplementary_compile_logs": "bad"},
    {"compile_log": "/log", "backend_preference": "npi_only"},
])
def test_invalid_native_inputs_fail_before_parsing(arguments):
    with pytest.raises(ValueError):
        read_native_inputs(arguments)


@pytest.mark.parametrize("timeout", [True, 0, -1, 301, float("nan"), float("inf"), "30"])
def test_finite_total_budget(timeout):
    with pytest.raises(ValueError):
        RequestBudget(timeout)


def test_kdb_only_never_invents_compile_context():
    inputs = read_native_inputs({"kdb_path": "/existing/candidate", "top_hint": "top"})
    assert inputs.compile_result() is None and inputs.metadata_snapshot is None
    assert inputs.compile_metadata_identity_sha256 is None
    assert decode_native_inputs(encode_native_inputs(inputs)) == inputs


def test_duplicate_logs_and_wrong_top_rejected(tmp_path):
    _, log, _ = project(tmp_path)
    for args in ({"supplementary_compile_logs": [str(log)]}, {"top_hint": "wrong"}):
        with pytest.raises(ValueError):
            read_native_inputs({"compile_log": str(log), **args})


def test_source_symlink_replacement_does_not_change_metadata_identity(tmp_path):
    source, log, _ = project(tmp_path)
    first = read_native_inputs({"compile_log": str(log)})
    replacement = tmp_path / "replacement.sv"
    replacement.write_text("module changed; endmodule\n")
    source.unlink()
    source.symlink_to(replacement)
    second = read_native_inputs({"compile_log": str(log)})
    assert first.compile_metadata_identity_sha256 == second.compile_metadata_identity_sha256
    assert first.fallback_key == second.fallback_key
    assert first.current() and second.current()
