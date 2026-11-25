from concurrent.futures import ThreadPoolExecutor
import json
import os
from pathlib import Path
import re
import shlex
import subprocess
import sys

import anyio
from mcp import ClientSession, StdioServerParameters
from mcp.client.stdio import stdio_client
import pytest

from scripts import export_verdi_rc as exporter


SCRIPT = Path(exporter.__file__).resolve()
FIXTURES = Path(__file__).parent / "fixtures"
FLAG = "TRACEWEAVE_VERDI_RC_EXPORT"


@pytest.fixture(autouse=True)
def default_export_environment(monkeypatch):
    monkeypatch.delenv(FLAG, raising=False)
    monkeypatch.setattr(exporter, "VERDI_RC_EXPORT", True)


def test_concurrent_publication_preserves_every_view(tmp_path):
    directory = tmp_path / "debug"
    stem = "数据比较_20260101_123456"
    views = [f"view {n}\n" for n in range(8)]
    with ThreadPoolExecutor(max_workers=8) as pool:
        paths = list(pool.map(lambda view: exporter._publish(directory, stem, view), views))
    assert len(set(paths)) == len(views)
    assert [p.read_text() for p in paths] == views
    assert set(directory.iterdir()) == set(paths)
    assert directory / f"{stem}.rc" in paths


def test_issue_name_is_a_single_filesystem_component():
    assert exporter._issue_slug("../写入 / 数据 mismatch") == "写入_数据_mismatch"
    assert len(exporter._issue_slug("错" * 200).encode()) <= 160
    with pytest.raises(ValueError):
        exporter._issue_slug("../")


def test_changed_dump_does_not_publish_a_report(tmp_path, monkeypatch):
    monkeypatch.chdir(tmp_path)
    wave = tmp_path / "wave.fsdb"
    wave.touch()

    class ChangedDump:
        def __init__(self, path):
            assert path == str(wave)

        def get_header(self):
            return {"scale_fs_per_tick": 1000, "simulation_duration_ps": 100}

        def get_signal_declaration(self, path):
            assert path == "top.rx"
            wave.write_bytes(b"replaced")

        def close(self):
            pass

    monkeypatch.setattr(exporter, "FSDBParser", ChangedDump)
    with pytest.raises(ValueError, match="FSDB changed"):
        exporter.export_issue({"issue": "mismatch", "fsdb": "wave.fsdb",
                               "groups": {"Data": ["top.rx"]}, "error_time": 0})
    assert not (tmp_path / "debug").exists()


def test_cli_rejects_duplicate_group_without_writing(tmp_path):
    proc = subprocess.run([sys.executable, str(SCRIPT)], cwd=tmp_path, text=True,
                          input='{"groups":{"Data":["a"],"Data":["b"]}}', capture_output=True)
    assert proc.returncode != 0 and proc.stdout == ""
    assert "duplicate JSON key" in proc.stderr
    assert not (tmp_path / "debug").exists()


@pytest.mark.parametrize("value", ["0", "OFF", "false", "no"])
def test_disabled_cli_skips_before_reading_or_writing(tmp_path, value):
    result = subprocess.run([sys.executable, str(SCRIPT), "missing.json"], cwd=tmp_path,
                            env={**os.environ, FLAG: value}, capture_output=True, text=True, check=True)
    assert json.loads(result.stdout) == {"status": "disabled", "reason": FLAG + "=0"}
    assert list(tmp_path.iterdir()) == []


def test_disabled_export_skips_even_an_unavailable_fsdb(tmp_path, monkeypatch):
    monkeypatch.chdir(tmp_path)
    monkeypatch.setattr(exporter, "VERDI_RC_EXPORT", False)
    assert exporter.export_issue({"fsdb": "missing.fsdb"})["status"] == "disabled"
    assert not (tmp_path / "debug").exists()


@pytest.mark.anyio
@pytest.mark.parametrize("value", [None, "1", "0"])
async def test_stdio_client_receives_the_configured_export_policy(tmp_path, value):
    server = SCRIPT.parents[1] / "server.py"
    env = {**os.environ, "TRACEWEAVE_CACHE_DIR": str(tmp_path / "cache")}
    if value is not None:
        env[FLAG] = value
    params = StdioServerParameters(command=sys.executable, args=[str(server)], cwd=tmp_path, env=env)
    with anyio.fail_after(30):
        async with stdio_client(params) as (reader, writer):
            async with ClientSession(reader, writer) as session:
                instructions = (await session.initialize()).instructions
                tools = (await session.list_tools()).tools
    assert all("verdi_rc" not in tool.name for tool in tools)
    if value == "0":
        assert "export_verdi_rc.py" not in instructions
        assert FLAG + "=0" in instructions
        return
    command = next(line for line in instructions.splitlines() if "export_verdi_rc.py" in line)
    assert shlex.split(command) == [sys.executable, str(SCRIPT)]
    assert '"role":"evidence"' in instructions
    result = subprocess.run(shlex.split(command) + ["--help"], cwd=tmp_path, text=True,
                            capture_output=True, check=True)
    assert "first_deviation" in result.stdout
    assert '"role":"evidence"' in result.stdout


@pytest.mark.eda_integration
@pytest.mark.parametrize(("fixture", "deviation", "unit", "cursor", "marker"), [
    ("scale_100fs", "100100500fs", "1fs", 101000000, 100100500),
    ("scale_1ns", "100ns", "1ps", 101000, 100000),
])
def test_cli_and_nwave_restore_on_the_already_open_fsdb(
        tmp_path, require_fsdb_runtime, fixture, deviation, unit, cursor, marker):
    agent = tmp_path / "agent directory"
    viewer = tmp_path / "viewer"
    agent.mkdir()
    viewer.mkdir()
    wave = FIXTURES / f"{fixture}.fsdb"
    signal = f"{fixture}_tb.addr[31:0]"
    spec = {"issue": "地址 mismatch", "fsdb": str(wave),
            "groups": {"地址比较": [{"path": signal, "role": "evidence"}]},
            "changes": {"decoder.sv:21 address bits": [{"path": signal, "slice": [7, 4]}]},
            "error_time": "101ns", "first_deviation": deviation}
    cmd = [sys.executable, str(SCRIPT)]
    # NPI initialization can change the test process locale. The receipt is UTF-8.
    exported = subprocess.run(cmd, input=json.dumps(spec), cwd=agent, encoding="utf-8",
                              capture_output=True, check=True, timeout=30)
    receipt = json.loads(exported.stdout)
    rc = Path(receipt["rc_path"])
    assert receipt["fsdb"] == str(wave)
    assert rc.parent == agent / "debug"
    assert re.fullmatch(r"地址_mismatch_\d{8}_\d{6}\.rc", rc.name)

    # Switch the display unit before Restore to catch dependence on GUI preferences.
    (viewer / "rc_path.txt").write_text(str(rc), encoding="utf-8")
    script = viewer / "restore.tcl"
    script.write_text("""
set f [open restored.txt w]
if {[catch {
    set input [open rc_path.txt r]
    fconfigure $input -encoding utf-8
    set rc [read $input]
    close $input
    puts $f [wvGetActiveFileName]
    wvSetWindowTimeUnit 1 ns
    puts $f [wvRestoreSignal $rc]
    puts $f [wvGetActiveFileName]
    puts $f [wvGetWindowTimeUnit]
    puts $f [wvGetCursor]
    puts $f [wvGetMarker]
    puts $f [wvGetDisplaySignals -all]
    wvSaveSignalRC -layout -cursor -marker -usermarker
    wvSaveSignal roundtrip.rc
} err]} {puts $f $err}
close $f
wvExit
""")
    subprocess.run(["nWave", "-nologo", "-nogui", "-ssf", str(wave), "-play", str(script)],
                   cwd=viewer, capture_output=True, check=True, timeout=45)
    assert (viewer / "restored.txt").read_text(encoding="utf-8").splitlines() == [
        str(wave), "1", str(wave), unit, f"1 {cursor} x {unit}", f"1 {marker} x {unit}",
        f"/{fixture}_tb/addr[7:4] /{fixture}_tb/addr[31:0]",
    ]
    saved = (viewer / "roundtrip.rc").read_text(encoding="utf-8")
    assert 'addGroup "RTL change - decoder.sv:21 address bits"' in saved
    assert re.search(r"^addSignal .*ID_YELLOW5 .*addr\[7:4\]$", saved, re.M)
    assert re.search(r"^addSignal .*ID_CYAN5 .*addr\[31:0\]$", saved, re.M)
    assert 'addGroup "地址比较"' in saved and f'addSubGroup "{fixture}_tb"' in saved
    assert " reported_error ID_RED5 " in saved and " first_deviation ID_YELLOW8 " in saved

    spec["groups"] = {"Data": ["addr[31:0]"]}  # A real suffix, but no such exact dump path.
    manifest = agent / "bad.json"
    manifest.write_text(json.dumps(spec))
    failed = subprocess.run(cmd + [str(manifest)], cwd=agent, encoding="utf-8", capture_output=True)
    assert failed.returncode != 0 and failed.stdout == ""
    assert "Exact dump declaration not found" in failed.stderr
    assert list(rc.parent.iterdir()) == [rc]
