"""The input-cost benchmark preserves facts and reports actual cache conditions."""
import json
from pathlib import Path
import subprocess
import sys

from scripts.benchmark_sweep_shared_clock import build_shared_clock_vcd


ROOT = Path(__file__).resolve().parents[1]


def test_fresh_process_benchmark_records_cold_reuse_and_new_input(tmp_path):
    wave, new_wave = tmp_path / "initial.vcd", tmp_path / "new.vcd"
    text = build_shared_clock_vcd(2, 8)
    wave.write_text(text)
    new_wave.write_text(text + "#85\n1!\n")
    log = tmp_path / "run.log"
    log.write_text("UVM_ERROR top_tb.sv(10) @ 10 ns: reporter [SCB] compare failed\n")
    output = tmp_path / "results"
    subprocess.run([
        sys.executable, str(ROOT / "scripts/benchmark_input_costs.py"),
        "--source-root", str(ROOT), "--wave", str(wave), "--next-wave", str(new_wave),
        "--log", str(log), "--output", str(output),
    ], cwd=ROOT, capture_output=True, text=True, check=True, timeout=30)
    manifest = json.loads((output / "manifest.json").read_text())
    rows = json.loads((output / "measurements.json").read_text())
    assert len(manifest["inputs"]["wave"]["sha256"]) == 64
    assert "excludes handler" in manifest["measurement_boundary"]
    assert [row["label"] for row in rows] == [
        "initial_wave", "same_wave_1", "new_wave", "log", "same_log_1",
    ]
    assert [row["metrics"].get("vcd_parse_calls", 0) for row in rows[:3]] == [1, 0, 1]
    assert rows[0]["fact_sha256"] == rows[1]["fact_sha256"]
    assert rows[0]["fact_sha256"] != rows[2]["fact_sha256"]
    assert all(row["metrics"]["log_parse_completed"] == 1 for row in rows[3:])
    assert rows[3]["fact_sha256"] == rows[4]["fact_sha256"]
