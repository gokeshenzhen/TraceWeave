"""Actual stdio calls, independently of the internal Python dispatch tests."""

import json
from pathlib import Path
import subprocess
import sys

import pytest

from src.readback_probe import PROBE_VCD


ROOT = Path(__file__).resolve().parents[1]


@pytest.mark.parametrize("custom_command", [False, True])
def test_stdio_probe_checks_values_and_keeps_client_exposure_unknown(tmp_path, custom_command):
    command = [sys.executable, str(ROOT / "scripts/check_waveform_readback.py"),
               "--work-dir", str(tmp_path)]
    if custom_command:
        command.extend(["--server-command", sys.executable, str(ROOT / "server.py")])
    completed = subprocess.run(
        command,
        cwd=tmp_path, capture_output=True, text=True, timeout=40,
    )
    report = json.loads(completed.stdout)
    assert completed.returncode == 0, (report.get("failure_reason"), report.get("exception_type"), completed.stderr)
    assert report["status"] == "passed"
    assert report["server_info"]["name"] == "traceweave"
    assert report["server_catalog"]["status"] == "observed"
    if custom_command:
        assert report["launch_source"] is None
        assert report["launch_receipt_unchanged"] is None
    else:
        assert report["launch_source"]["commit"]
        assert report["launch_receipt_unchanged"] is True
    assert report["ai_client"]["catalog_visibility"] == "unknown"
    assert report["ai_client"]["readback_calls"] == "unknown"
    assert report["model_adoption"] == "not_measured"
    assert len(report["calls"]) == 12
    assert report["calls"][-1]["is_error"] is True
    assert report["fixture_retained"] is True
    assert Path(report["fixture"]).read_text() == PROBE_VCD
    assert not (tmp_path / "cache/telemetry/usage.jsonl").exists()


def test_probe_does_not_overwrite_an_unrelated_fixture(tmp_path):
    wave = tmp_path / "readback_probe.vcd"
    wave.write_text("existing user content")
    completed = subprocess.run(
        [sys.executable, str(ROOT / "scripts/check_waveform_readback.py"),
         "--work-dir", str(tmp_path)],
        capture_output=True, text=True, timeout=10,
    )
    assert completed.returncode != 0
    assert wave.read_text() == "existing user content"


def test_transport_failure_is_not_misreported_as_hidden_tools(tmp_path):
    completed = subprocess.run(
        [sys.executable, str(ROOT / "scripts/check_waveform_readback.py"),
         "--work-dir", str(tmp_path), "--server-command", sys.executable, "-c", "raise SystemExit(1)"],
        capture_output=True, text=True, timeout=40,
    )
    assert completed.returncode != 0
    report = json.loads(completed.stdout)
    assert report["failure_reason"] == "probe_setup_or_transport_failed"
    assert report["server_catalog"]["status"] == "unknown"
    assert report["ai_client"]["catalog_visibility"] == "unknown"
